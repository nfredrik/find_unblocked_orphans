import hashlib
import os
import sys
from collections import defaultdict, OrderedDict
from functools import lru_cache
from queue import Queue
from threading import Thread

import dnf

from pagure_info import PagureInfo
from releases import RAWHIDE_RELEASE, eprint


@lru_cache(maxsize=2048)
def srpm_nvr_object(query: str, name: str, version: str, release: str) -> str:
    try:
        return query.filter(name=name, version=version, release=release, arch='src').run()[0]
    except IndexError:
        eprint(
            f"Error: Cannot find a source rpm for {name}-{version}-{release}")
        sys.exit(1)


def setup_dnf(repo: str,
              source_repo: str) -> str:
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


class DepChecker:
    def __init__(self, release: str, branch) -> None:
        self._src_by_bin = self._bin_by_src = None

        self.dnfquery = self.release = release

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
    def by_src(self):
        if not self._bin_by_src:
            self.__create_mapping()
        return self._bin_by_src

    @property
    def by_bin(self):
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
        dependent_packages = {}

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
        for prov in provides:
            # check only base provide, ignore specific versions
            # "foo = 1.fc20" -> "foo"
            base_provide, *_ = prov.split()

            # print("FIXME: Workaround for:")
            # https://bugzilla.redhat.com/show_bug.cgi?id=1191178
            if base_provide[0] == "/":
                base_provide = base_provide.replace("[", "?")
                base_provide = base_provide.replace("]", "?")

            self.method_name23(base_provide, dependent_packages, ignore, prov, rpms, srpmname)
        return OrderedDict(sorted(dependent_packages.items()))

    def method_name23(self, base_provide, dependent_packages, ignore, prov, rpms, srpmname):
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

    def pagure_worker(self) -> None:
        while True:
            package = self.pagureinfo_queue.get()
            if package not in self.pagure_dict:
                pkginfo = PagureInfo(package, self.branch)
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

    def build_dep_map(self, incomplete, max_deps, packages, rpm_pkg_names) -> OrderedDict:
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

    def allow_check(self, allow_more, check_next, dep_map:dict, dependent_packages:dict, ignore:list, incomplete:list, max_deps,
                    name, seen, to_check) -> tuple[bool,list]:
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
    def srpm(self, package) -> str:
        """Given a package object, get a package object for the
        corresponding source rpm. Requires dnf still configured
        and a valid package object."""
        srpm, *_ = package.sourcerpm.split('.src.rpm')
        sname, sver, srel = srpm.rsplit('-', 2)
        return srpm_nvr_object(query=self.dnfquery, name=sname, version=sver, release=srel)
