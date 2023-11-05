#! /usr/bin/python3
#
# find_unblocked_orphans.py - A utility to find orphaned packages in pagure
#                             that are unblocked in koji and to show what
#                             may require those orphans
#
# Copyright (c) 2009-2013 Red Hat
# SPDX-License-Identifier:	GPL-2.0
#
# Authors:
#     Jesse Keating <jkeating@redhat.com>
#     Till Maas <opensource@till.name>

import argparse
import datetime
import email
import hashlib
import json
import os
import smtplib
import sys
import textwrap
import time
import traceback
from collections import defaultdict
from functools import lru_cache
from queue import Queue
from threading import Thread
from typing import Final, OrderedDict, Type

import dnf
import dogpile.cache
import koji
import requests
from dnf.query import Query
from requests import HTTPError

try:
    import texttable

    with_table = True
except ImportError:
    with_table = False

SUCCESS, FAILURE = 0, 1
ORPHAN_UID: Final = 'orphan'

HEADER = """The following packages are orphaned and will be retired when they
are orphaned for six weeks, unless someone adopts them. If you know for sure
that the package should be retired, please do so now with a proper reason:
https://fedoraproject.org/wiki/How_to_remove_a_package_at_end_of_life

Note: If you received this mail directly you (co)maintain one of the affected
packages or a package that depends on one. Please adopt the affected package or
retire your depending package to avoid broken dependencies, otherwise your
package will be retired when the affected package gets retired.
"""

WEEK_LIMIT: Final = 6

PAGURE_URL: Final = 'https://src.fedoraproject.org'
FEDORA_PROJECT_URL: Final = f'{PAGURE_URL}/api/0/projects'
PAGURE_MAX_ENTRIES_PER_PAGE: Final = 100

cache = dogpile.cache.make_region().configure(
    'dogpile.cache.dbm',
    expiration_time=86400,
    arguments=dict(
        filename=os.path.expanduser('~/.cache/dist-git-orphans-cache.dbm')),
)

LISTS_FEDORAPROJECT_ORG: Final = 'devel@lists.fedoraproject.org'
FEDORAPROJECT_ORG: Final = 'epel-announce@lists.fedoraproject.org'
KOJIHUB: Final = 'https://koji.fedoraproject.org/kojihub'

EPEL7_RELEASE = dict(
    repo='https://kojipkgs.fedoraproject.org/compose/updates/epel7/'
         'compose/Everything/x86_64/os/',
    source_repo='https://kojipkgs.fedoraproject.org/compose/updates/epel7/'
                'compose/Everything/source/tree/',
    koji_tag='epel7',
    koji_hub=KOJIHUB,
    pagure_branch='epel7',
    mailto=FEDORAPROJECT_ORG,
    bcc=[],
)
EPEL8_RELEASE = dict(
    repo='https://kojipkgs.fedoraproject.org/compose/updates/epel8/'
         'compose/Everything/x86_64/os/',
    source_repo='https://kojipkgs.fedoraproject.org/compose/updates/epel8/'
                'compose/Everything/source/tree/',
    koji_tag='epel8',
    koji_hub=KOJIHUB,
    pagure_branch='epel8',
    mailto=FEDORAPROJECT_ORG,
    bcc=[],
)
EPEL9_RELEASE = dict(
    repo='https://kojipkgs.fedoraproject.org/compose/updates/epel9/'
         'compose/Everything/x86_64/os/',
    source_repo='https://kojipkgs.fedoraproject.org/compose/updates/epel9/'
                'compose/Everything/source/tree/',
    koji_tag='epel9',
    koji_hub=KOJIHUB,
    pagure_branch='epel9',
    mailto=FEDORAPROJECT_ORG,
    bcc=[],
)
RAWHIDE_RELEASE = dict(
    repo='https://kojipkgs.fedoraproject.org/compose/rawhide/'
         'latest-Fedora-Rawhide/compose/Everything/x86_64/os',
    source_repo='https://kojipkgs.fedoraproject.org/compose/rawhide/'
                'latest-Fedora-Rawhide/compose/Everything/source/tree/',
    koji_tag='f40',
    koji_hub=KOJIHUB,
    pagure_branch='rawhide',
    mailto=LISTS_FEDORAPROJECT_ORG,
    bcc=[],
)
BRANCHED_RELEASE = dict(
    repo='https://kojipkgs.fedoraproject.org/compose/branched/'
         'latest-Fedora-39/compose/Everything/x86_64/os',
    source_repo='https://kojipkgs.fedoraproject.org/compose/branched/'
                'latest-Fedora-39/compose/Everything/source/tree/',
    koji_tag='f39',
    pagure_branch='f39',
    koji_hub=KOJIHUB,
    mailto=LISTS_FEDORAPROJECT_ORG,
    bcc=[],
)
RELEASES = {
    "rawhide": RAWHIDE_RELEASE,
    "branched": BRANCHED_RELEASE,
    "epel9": EPEL9_RELEASE,
    "epel8": EPEL8_RELEASE,
    "epel7": EPEL7_RELEASE,
}
FOOTER = """-- \nThe script creating this output is run and developed by Fedora
Release Engineering. Please report issues at its pagure instance:
https://pagure.io/releng/
The sources of this script can be found at:
https://pagure.io/releng/blob/main/f/scripts/find_unblocked_orphans.py
"""

def eprint(*args, **kwargs) -> None:
    kwargs.setdefault('file', sys.stderr)
    kwargs.setdefault('flush', True)
    print(*args, **kwargs)

def send_mail(from_: str, to: str | list, subject: str, text: str, bcc: list[str] = None) -> None:
    to = [to] if isinstance(to, str) else to
    bcc = [] if bcc is None else bcc

    msg = email.mime.text.MIMEText(text)
    msg["Subject"] = subject
    msg["From"] = from_
    msg["To"] = to

    smtp = smtplib.SMTP('127.0.0.1')
    errors = smtp.sendmail(from_, to + bcc, msg.as_string())
    smtp.quit()
    eprint(f"mail errors: {repr(errors)}")


class PagureInfo:
    def __init__(self, package: str, branch: str, ns: str = 'rpms') -> None:
        try:
            response = requests.get(f'{PAGURE_URL}/api/0/{ns}/{package}')
            response.raise_for_status()
            self.pkginfo = response.json()
            if 'error' in self.pkginfo:
                # This is likely a "project not found" 404 error.
                raise ValueError(self.pkginfo['error'])
        except HTTPError as e:
            eprint(f'Error, failed to get from url...{e}')
        except Exception:
            eprint(f"Error getting pagure info for {ns}/{package} on {branch}")
            traceback.print_exc(file=sys.stderr)
            self.pkginfo = None

    def get_people(self) -> list:
        if self.pkginfo is None:
            return []
        people = set()
        for kind in ['access_users', 'access_groups']:
            for persons in self.pkginfo[kind].values():
                for person in persons:
                    people.add(person)
        return list(sorted(people))

    @property
    def age(self) -> datetime:
        return datetime.datetime.now(datetime.timezone.utc) - self.status_change

    @property
    def status_change(self) -> datetime:
        if self.pkginfo is None:
            return datetime.datetime.now(datetime.timezone.utc)
        # See https://pagure.io/pagure/issue/2412
        the_date = "date_modified" if "date_modified" in self.pkginfo else "date_created"
        status_change = float(self.pkginfo[the_date])
        return datetime.datetime.fromtimestamp(status_change, tz=datetime.timezone.utc)

    def __getitem__(self, *args, **kwargs):
        return self.pkginfo.__getitem__(*args, **kwargs)

def setup_dnf(repo: str,
              source_repo: str) -> Query:
    """ Setup dnf query with two repos
    """
    base = dnf.Base()
    # use digest to make repo id unique for each URL
    for baseurl, name in (repo, 'repo'), (source_repo, 'repo-source'):
        r = base.repos.add_new_repo(
            f'{name}-{hashlib.sha256(baseurl.encode()).hexdigest()}',
            base.conf,
            baseurl=[baseurl],
            skip_if_unavailable=False,
        )
        r.enable()
        r.load()

    base.fill_sack(load_system_repo=False, load_available_repos=True)
    return base.sack.query()



@cache.cache_on_arguments()
def get_pagure_orphans(namespace: str, page: int = 1) -> tuple[dict, int]:
    params = dict(owner=ORPHAN_UID, namespace=namespace,
                  page=page,
                  per_page=PAGURE_MAX_ENTRIES_PER_PAGE)

    for i in range(20):
        try:
            response = requests.get(FEDORA_PROJECT_URL, params=params)
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            print('Error,__get_pagure_orphans failed, try again ...')
            time.sleep(i)
            continue

        break
    else:
        raise IOError(f'Error, we failed to fetch from url:{FEDORA_PROJECT_URL}')

    pkgs = response.json()['projects']
    pages = response.json()['pagination']['pages']
    return {p['name']: p for p in pkgs}, pages


@cache.cache_on_arguments()
def orphan_packages(namespace: str = 'rpms') -> dict:
    pkgs, pages = get_pagure_orphans(namespace=namespace)
    eprint(f"({pages} pages)", end=" ")
    for page in range(2, pages + 1):
        eprint("." if page % 10 else page, end="")
        new_pkgs, _ = get_pagure_orphans(namespace, page)
        pkgs.update(new_pkgs)
    return pkgs


def unblocked_packages(packages: list, tag_id: dict, kojihub: dict) -> list:
    unblocked = []
    kojisession = koji.ClientSession(kojihub)

    kojisession.multicall = True
    for p in packages:
        kojisession.listPackages(tagID=tag_id, pkgID=p, inherited=True)
    listings = kojisession.multiCall()

    eprint("Check the listings for unblocked packages.")

    for pkgname, result in zip(packages, listings):
        if isinstance(result, list):
            [pkg] = result
            if pkg:
                if not pkg[0]['blocked']:
                    package_name = pkg[0]['package_name']
                    unblocked.append(package_name)
            else:
                eprint('TODO: what state does this condition represent?')
        else:
            eprint(f"ERROR: {pkgname}: {result}")
    return unblocked


class DepCheckerError(Exception):
    pass

class DepChecker:
    def __init__(self, query: Query, branch: str) -> None:
        self._src_by_bin = self._bin_by_src = None

        self.dnfquery = query

        self.branch = branch
        self.pagureinfo_queue = Queue()
        self.pagure_dict = {}
        self.not_in_repo = []

        self.dep_chain = defaultdict(set)

    def __create_mapping(self) -> None:
        src_by_bin = {}
        bin_by_src = {}
        for rpm_package in self.dnfquery:
            if rpm_package.arch == 'src':
                continue
            srpm = self.srpm(rpm_package)
            src_by_bin[rpm_package] = srpm
            bin_srpm_name = srpm.name
            bin_by_src.setdefault(bin_srpm_name, []).append(rpm_package)
        self._src_by_bin = src_by_bin
        self._bin_by_src = bin_by_src

    @property
    def by_src(self) -> dict:
        if not self._bin_by_src:
            self.__create_mapping()
        return self._bin_by_src

    @property
    def by_bin(self) -> dict:
        if not self._src_by_bin:
            self.__create_mapping()
        return self._src_by_bin

    def find_dependent_packages(self, srpmname: str, ignore: list[str]) -> OrderedDict:
        """ Return packages depending on packages built from SRPM ``srpmname``
            that are built from different SRPMS not specified in ``ignore``.

            :param ignore: list of binary package names that will not be
                returned as dependent packages or considered as alternate
                providers
            :type ignore: list() of str()

            :returns: OrderedDict dependent_package: list of requires only
                provided by package ``srpmname`` {dep_pkg: [prov, ...]}
        """
        # Some of this code was stolen from repoquery
        #dependent_packages = {}

        # Handle packags not found in the repo
        try:
            rpms = self.by_src[srpmname]
        except KeyError:
            # If we don't have a package in the repo, there is nothing to do
            eprint(f"Package {srpmname} not found in repo")
            self.not_in_repo.append(srpmname)
            rpms = []

        # provides of all packages built from ``srpmname``
        provides = []
        for pkg in rpms:
            # add all the provides from the package as strings
            string_provides = [str(prov) for prov in pkg.provides]
            provides.extend(string_provides)

            # add all files as provides
            # pkg.files is a list of paths
            # sometimes paths start with "//" instead of "/"
            # normalise "//" to "/":
            # os.path.normpath("//") == "//", but
            # os.path.normpath("///") == "/"
            file_provides = [os.path.normpath(f'//{fn}') for fn in pkg.files]
            provides.extend(file_provides)

        # Zip through the provides and find what's needed
        dependent_packages = {}
        for prov in provides:
            # check only base provide, ignore specific versions
            # "foo = 1.fc20" -> "foo"
            base_provide, *_ = prov.split()

            # print("FIXME: Workaround for:")
            # https://bugzilla.redhat.com/show_bug.cgi?id=1191178
            if base_provide[0] == "/":
                base_provide = base_provide.replace("[", "?")
                base_provide = base_provide.replace("]", "?")

            dependent_packages = self.elide(base_provide, dependent_packages, ignore, prov, rpms, srpmname)

        return OrderedDict(sorted(dependent_packages.items()))

    def old_elide(self, base_provide: str, dependent_packages: dict, ignore: list, prov: str, rpms: list,
              srpmname: str) -> dict:

        #olle = {}
        # Elide provide if also provided by another package
        for pkg in self.dnfquery.filter(provides=base_provide):
            # print("FIXME: might miss broken dependencies in case the other")
            # provider depends on a to-be-removed package as well
            if pkg.name in ignore:
                eprint(f"Ignoring provider package {pkg.name}")
            elif pkg not in rpms:
                break
        else:
            for dependent_pkg in self.dnfquery.filter(
                    requires=base_provide):
                # skip if the dependent rpm package belongs to the
                # to-be-removed Fedora package
                if dependent_pkg in self.by_src[srpmname]:
                    continue

                # use setdefault to either create an entry for the
                # dependent package or add the required prov
                dependent_packages.setdefault(dependent_pkg, set()).add(
                    prov)

    def elide(self, base_provide: str, dependent_packages: dict, ignore: list, prov: str, rpms: list,
              srpmname: str) -> dict:

        olle = {}
        # Elide provide if also provided by another package
        for pkg in self.dnfquery.filter(provides=base_provide):
            # print("FIXME: might miss broken dependencies in case the other")
            # provider depends on a to-be-removed package as well
            if pkg.name in ignore:
                eprint(f"Ignoring provider package {pkg.name}")
            elif pkg not in rpms:
                break
        else:
            for dependent_pkg in self.dnfquery.filter(
                    requires=base_provide):
                # skip if the dependent rpm package belongs to the
                # to-be-removed Fedora package
                if dependent_pkg in self.by_src[srpmname]:
                    continue

                # use setdefault to either create an entry for the
                # dependent package or add the required prov
                olle.setdefault(dependent_pkg, set()).add(
                    prov)
        return olle

    def pagure_worker(self) -> None:
        while True:
            package = self.pagureinfo_queue.get()
            if package not in self.pagure_dict:
                pkginfo = PagureInfo(package=package, branch=self.branch)
                qsize = self.pagureinfo_queue.qsize()
                eprint(f"Got info for {package} on {self.branch}, todo: {qsize}")
                self.pagure_dict[package] = pkginfo
            self.pagureinfo_queue.task_done()

    def recursive_deps(self, packages: list, max_deps: int = 20) -> tuple[OrderedDict, list]:
        incomplete = []
        # Start threads to get information about (co)maintainers for packages
        for _ in range(2):
            people_thread = Thread(target=self.pagure_worker)
            people_thread.daemon = True
            people_thread.start()
        # get a list of all rpm_pkgs that are to be removed
        rpm_pkg_names = []
        for name in packages:
            self.pagureinfo_queue.put(name)
            # Empty list if pkg is only for a different arch
            bin_pkgs = self.by_src.get(name, [])
            rpm_pkg_names.extend([p.name for p in bin_pkgs])

        # dict for all dependent packages for each to-be-removed package
        dep_map = self.build_dep_map(incomplete, max_deps, packages, rpm_pkg_names)

        eprint("Waiting for (co)maintainer information...", end=' ')
        self.pagureinfo_queue.join()
        eprint("done")
        return dep_map, incomplete

    def build_dep_map(self, incomplete: list, max_deps: int, packages: list, rpm_pkg_names: list) -> OrderedDict:
        dep_map = OrderedDict()
        for name in sorted(packages):
            self.dep_chain[name] = set()  # explicitly initialize the set for the orphaned
            eprint(f"Getting packages depending on: {name}")
            ignore = rpm_pkg_names
            dep_map[name] = OrderedDict()
            to_check = [name]
            allow_more = True
            seen = []
            while True:
                eprint(f"to_check ({len(to_check)}): {to_check}")
                check_next = to_check.pop(0)
                seen.append(check_next)
                if dependent_packages := self.find_dependent_packages(
                        check_next, ignore
                ):
                    allow_more, to_check = self.allow_check(allow_more, check_next, dep_map, dependent_packages,
                                                            ignore, incomplete, max_deps, name, seen, to_check)
                if not to_check:
                    break
            if not allow_more:
                eprint(f"More than {max_deps} broken deps for package "
                       f"'{name}', dependency check not completed")
        return dep_map

    def allow_check(self, allow_more: bool, check_next: str, dep_map: dict, dependent_packages: dict, ignore: list,
                    incomplete: list, max_deps: int,
                    name: str, seen: list, to_check: list) -> tuple[bool, list]:
        new_names = []
        new_srpm_names = set()
        for pkg, dependencies in dependent_packages.items():
            srpm_name = self.by_bin[pkg].name if pkg.arch != "src" else pkg.name
            if (srpm_name not in to_check and
                    srpm_name not in new_names and
                    srpm_name not in seen):
                new_names.append(srpm_name)
            new_srpm_names.add(srpm_name)

            for dep in dependencies:
                dep_map[name].setdefault(
                    srpm_name,
                    OrderedDict()
                ).setdefault(pkg, set()).add(dep)
        for new_srpm_name in new_srpm_names:
            self.dep_chain[new_srpm_name].add(check_next)
        for srpm_name in new_srpm_names:
            self.pagureinfo_queue.put(srpm_name)
        ignore.extend(new_names)
        if allow_more:
            to_check.extend(new_names)
            found_deps = dep_map[name].keys()
            dep_count = len(set(found_deps) | set(to_check))
            if dep_count > max_deps:
                todo_deps = max_deps - len(found_deps)
                todo_deps = max(todo_deps, 0)
                incomplete.append(name)
                eprint(f"Dep count is {dep_count}")
                eprint(f"incomplete is {incomplete}")

                allow_more = False
                to_check = to_check[:todo_deps]
        return allow_more, to_check

    # This function was stolen from pungi
    def srpm(self, package: dnf.package.Package) -> dnf.package.Package:
        """Given a package object, get a package object for the
        corresponding source rpm. Requires dnf still configured
        and a valid package object."""
        srpm, *_ = package.sourcerpm.split('.src.rpm')
        sname, sver, srel = srpm.rsplit('-', 2)
        return srpm_nvr_object(query=self.dnfquery, name=sname, version=sver, release=srel)

    @staticmethod
    @lru_cache(maxsize=2048)
    def srpm_nvr_object(query: Query, name: str, version: str, release: str) -> dnf.package.Package:
        try:
            return query.filter(name=name, version=version, release=release, arch='src').run()[0]
        except IndexError:
            DepCheckerError(
                f"Error: Cannot find a source rpm for {name}-{version}-{release}")





def prepare_for_mail(args: argparse.Namespace, addresses: list[str], text: str, release: dict) -> None:
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    subject = f"Orphaned Packages in {args.release} ({today})"
    mailto = args.mailto or release["mailto"]
    bcc = addresses + release["bcc"] if args.send else None
    send_mail(args.mailfrom, mailto, subject, text, bcc)


def save_to_json(json_filename: str, depchecker: DepChecker, orphans: list) -> None:
    eprint(f'Saving {json_filename} with machine readable info')
    sc = {pkg: depchecker.pagure_dict[pkg].status_change.isoformat()
          for pkg in orphans if pkg in depchecker.pagure_dict}
    ap = {pkg: sorted(reasons) for pkg, reasons in depchecker.dep_chain.items()}
    json_data = {'status_change': sc, 'affected_packages': ap}
    try:
        with open(json_filename, 'w') as f:
            json.dump(json_data, f, indent=4, sort_keys=True)
    except OSError as e:
        eprint(f'Cannot save {json_filename}:', end=' ')
        eprint(f'{type(e).__name__}: e')


def wrap_and_format(label: str, pkgs: list) -> str:
    wrapper = textwrap.TextWrapper(
        break_long_words=False, subsequent_indent="    ",
        break_on_hyphens=False
    )
    count = len(pkgs)
    text = f"{label} ({count}): {' '.join(pkgs)}"
    return "\n" + wrapper.fill(text) + "\n\n"


def package_info(unblocked: list, dep_map: OrderedDict, deep_checker: DepChecker, incomplete: list,
                 orphans: list = None,
                 failed: list = None,
                 week_limit: int = WEEK_LIMIT, release: str = "") -> tuple[str, list[str]]:
    def maintainer_table(packages: list, pagure_dict: dict) -> tuple:
        affected_people = {}

        table = ""
        if with_table:
            table = texttable.Texttable(max_width=80)
            table.header(["Package", "(co)maintainers", "Status Change"])
            table.set_cols_align(["l", "l", "l"])
            table.set_deco(HEADER)

        for package_name in packages:
            pkginfo = pagure_dict[package_name]
            people = pkginfo.get_people()
            for p in people:
                affected_people.setdefault(p, set()).add(package_name)
            p = ', '.join(people)
            age = pkginfo.age
            agestr = f"{age.days // 7} weeks ago"

            if with_table:
                table.add_row([package_name, p, agestr])
            else:
                table += f"{package_name} {p} {agestr}\n"

        if with_table:
            table = table.draw()
        return table, affected_people

    def __maintainer_info(affected_people: dict) -> str:
        return "\n".join(
            [
                f"{person}: {', '.join(packages)}"
                for person, packages in sorted(affected_people.items())
                if person != ORPHAN_UID
            ]
        )

    def __dependency_info(dep_map: dict, affected_people: dict, pagure_dict: dict, incomplete: list) -> str:
        info = ""
        for package_name, subdict in dep_map.items():
            if subdict:
                pkginfo = pagure_dict[package_name]
                status_change = pkginfo.status_change.strftime("%Y-%m-%d")
                age = pkginfo.age.days // 7
                fmt = "Depending on: {} ({}), status change: {} ({} weeks ago)\n"
                info += fmt.format(package_name, len(subdict.keys()),
                                   status_change, age)
                for fedora_package, dependent_packages in subdict.items():
                    people = pagure_dict[fedora_package].get_people()
                    for p in people:
                        affected_people.setdefault(p, set()).add(package_name)
                    p = ", ".join(people)
                    info += f"\t{fedora_package} (maintained by: {p})\n"
                    for dep in dependent_packages:
                        provides = ", ".join(sorted(dependent_packages[dep]))
                        info += f"\t\t{dep} requires {provides}\n"
                    info += "\n"
            if package_name in incomplete:
                info += f"\tToo many dependencies for {package_name}, "
                info += "not all listed here\n\n"
        return info

    info = ""
    pagure_dict = deep_checker.pagure_dict
    not_in_repo = deep_checker.not_in_repo

    table, affected_people = maintainer_table(unblocked, pagure_dict)
    info += table
    info += "\n\nThe following packages require above mentioned packages:\n"
    info += __dependency_info(dep_map, affected_people, pagure_dict, incomplete)

    info += "Affected (co)maintainers\n"
    info += __maintainer_info(affected_people)

    release_text = f" ({release})" if release else ""

    tmp_info, orphans, orphans_breaking_deps_stale = gather_orphans(deep_checker.branch, dep_map, orphans,
                                                                    pagure_dict,
                                                                    release_text,
                                                                    unblocked, week_limit)

    info += tmp_info

    breaking = {dep for deps in dep_map.values() for dep in deps}

    info += gather_breaking(deep_checker.branch, breaking, dep_map, orphans, orphans_breaking_deps_stale,
                            release_text,
                            week_limit)

    info += gather_failed(dep_map, failed, release_text)

    if not_in_repo:
        info += wrap_and_format(f"Not found in repo{release_text}", sorted(not_in_repo))

    addresses = [f"{p}@fedoraproject.org"
                 for p in affected_people if p != ORPHAN_UID]
    return info, addresses


def gather_failed(dep_map: dict, failed: list, release_text: str) -> str:
    if not failed:
        return ''

    tmp = ""
    ftbfs_label = f"FTBFS{release_text}"
    tmp += wrap_and_format(ftbfs_label, failed)

    ftbfs_breaking_deps = [o for o in failed if
                           o in dep_map and dep_map[o]]

    tmp += wrap_and_format(f"{ftbfs_label} (depended on)", ftbfs_breaking_deps)

    ftbfs_not_breaking_deps = [o for o in failed if
                               o not in dep_map or not dep_map[o]]

    tmp += wrap_and_format(f"{ftbfs_label} (not depended on)", ftbfs_not_breaking_deps)

    return tmp


def gather_breaking(branch: str, breaking: set, dep_map: OrderedDict, orphans: list, orphans_breaking_deps_stale: list,
                    release_text: str, week_limit: int,
                    ) -> str:
    tmp = ''
    if not breaking:
        return tmp

    tmp += wrap_and_format(f"Depending packages{release_text}", sorted(breaking))

    if orphans:
        reverse_deps = OrderedDict()
        stale_breaking = set()
        for package in orphans_breaking_deps_stale:
            for depender in dep_map[package]:
                reverse_deps.setdefault(depender, []).append(package)
            stale_breaking = stale_breaking.union(
                set(dep_map[package].keys()))
        for depender, providers in reverse_deps.items():
            eprint(f"fedretire --orphan-dependent {' '.join(providers)} "
                   f"--branch {branch} -- {depender}")
            for providingpkg in providers:
                eprint("fedretire --orphan --branch "
                       f"{branch} -- {providingpkg}")
        tmp += wrap_and_format(
            f"Packages depending on packages orphaned{release_text} "
            f"for more than {week_limit} weeks",
            sorted(stale_breaking))
    return tmp


def gather_orphans(branch: str, dep_map: OrderedDict, orphans: list, pagure_dict: dict, release_text: str,
                   unblocked: list, week_limit: int) -> tuple[str, list, list]:
    info = ''
    if not orphans:
        return info, [], []

    orphans = [o for o in orphans if o in unblocked]
    info += wrap_and_format("Orphans", orphans)

    orphans_breaking_deps = [o for o in orphans if dep_map.get(o)]
    info += wrap_and_format("Orphans (dependend on)",
                            orphans_breaking_deps)

    orphans_breaking_deps_stale = [
        o for o in orphans_breaking_deps if
        (pagure_dict[o].age.days // 7) >= week_limit]

    info += wrap_and_format(
        f"Orphans{release_text} for at least {week_limit} "
        "weeks (dependend on)",
        orphans_breaking_deps_stale)

    orphans_not_breaking_deps = [o for o in orphans if not dep_map.get(o)]

    info += wrap_and_format(f"Orphans{release_text} (not depended on)",
                            orphans_not_breaking_deps)

    orphans_not_breaking_deps_stale = [
        o for o in orphans_not_breaking_deps if
        (pagure_dict[o].age.days // 7) >= week_limit]

    if orphans_not_breaking_deps_stale:
        eprint(f"fedretire --orphan --branch {branch} -- " +
               " ".join(orphans_not_breaking_deps_stale))

    info += wrap_and_format(
        f"Orphans{release_text} for at least {week_limit} "
        "weeks (not dependend on)",
        orphans_not_breaking_deps_stale)
    return info, orphans, orphans_breaking_deps_stale




@lru_cache(maxsize=2048)
def srpm_nvr_object(query: Query, name: str, version: str, release: str) -> dnf.package.Package:
    try:
        return query.filter(name=name, version=version, release=release, arch='src').run()[0]
    except IndexError:
        eprint(
            f"Error: Cannot find a source rpm for {name}-{version}-{release}")
        sys.exit(1)




def main(args: argparse.Namespace):
    release = RELEASES[args.release]
    if args.source_repo:
        release["source_repo"] = args.source_repo

    if args.repo:
        release["repo"] = args.repo

    repo = release["repo"]
    source_repo = release["source_repo"]
    branch = release["pagure_branch"]
    koji_tag = release["koji_tag"]
    koji_hub = release["koji_hub"]

    eprint('Contacting pagure for list of orphans...', end=' ')
    orphans = {} if args.skip_orphans else sorted(orphan_packages())
    eprint('done')

    text = f"\nReport started at {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"

    eprint('Getting builds from koji...', end=' ')
    allpkgs = sorted(list(set(list(orphans) + args.failed)))
    unblocked = unblocked_packages(allpkgs, tag_id=koji_tag, kojihub=koji_hub) if args.skipblocked else allpkgs
    eprint('done')

    text += HEADER.format(koji_tag.upper())
    eprint("Setting up dependency checker...", end=' ')

    dnf_base = setup_dnf(repo=repo, source_repo=source_repo)
    depchecker = DepChecker(query=dnf_base, branch=branch)
    eprint("done")

    eprint('Calculating dependencies...', end=' ')
    # Create dnf object and depsolve out if requested.
    eprint("TODO: add app args to either depsolve or not")
    try:
     dep_map, incomplete = depchecker.recursive_deps(unblocked, args.max_deps)
    except DepCheckerError:
        return FAILURE

    eprint('done')
    info, addresses = package_info(
        unblocked=unblocked, dep_map=dep_map, deep_checker=depchecker, orphans=orphans, failed=args.failed,
        release=args.release, incomplete=incomplete)
    text += "\n"
    text += info
    text += FOOTER
    text += f"\nReport finished at {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    print(text)

    if args.json is not None:
        save_to_json(json_filename=args.json, depchecker=depchecker, orphans=orphans)

    if args.mailto or args.send:
        prepare_for_mail(args, addresses, text, release)
    eprint(f"Addresses ({len(addresses)}):", ", ".join(addresses))
    return SUCCESS


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-orphans", dest="skip_orphans",
                        help="Do not look for orphans",
                        default=False, action="store_true")
    parser.add_argument("--max_deps", dest="max_deps", type=int,
                        help="set max_deps on recursive find deps",
                        default=20)
    parser.add_argument("--release", choices=RELEASES.keys(),
                        default="rawhide")
    parser.add_argument("--mailto", default=None,
                        help="Send mail to this address (for testing)")
    parser.add_argument(
        "--send", default=False, action="store_true",
        help="Actually send mail including Bcc addresses to mailing list"
    )
    parser.add_argument("--source-repo", default=None,
                        help="Source repo URL to use for depcheck")
    parser.add_argument("--repo", default=None,
                        help="Repo URL to use for depcheck")
    parser.add_argument("--json", default=None,
                        help="Export info about orphaned "
                             "packages to a specified JSON file")
    parser.add_argument("--no-skip-blocked", default=True,
                        dest="skipblocked", action="store_false",
                        help="Do not skip blocked pkgs")
    parser.add_argument("--mailfrom", default="nobody@fedoraproject.org")
    parser.add_argument("failed", nargs="*",
                        help="Additional packages, e.g. FTBFS packages")
    args = parser.parse_args()
    sys.exit(main(args))
