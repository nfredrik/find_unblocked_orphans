import email.mime
import smtplib
import textwrap
from collections import OrderedDict
from typing import Callable, Final

from deep_checker import DepChecker
from releases import eprint, ORPHAN_UID

WEEK_LIMIT:Final = 6

try:
    import texttable

    with_table = True
except ImportError:
    with_table = False

def wrap_and_format(label: str, pkgs: list) -> str:
    wrapper = textwrap.TextWrapper(
        break_long_words=False, subsequent_indent="    ",
        break_on_hyphens=False
    )
    count = len(pkgs)
    text = f"{label} ({count}): {' '.join(pkgs)}"
    return "\n" + wrapper.fill(text) + "\n\n"


def package_info(unblocked: list, dep_map: OrderedDict, deep_checker: DepChecker, incomplete: list, orphans: list = None,
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


def gather_failed(dep_map: dict, failed:list, release_text: str) -> str:
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


def gather_breaking(branch:str, breaking:set, dep_map:OrderedDict, orphans:list, orphans_breaking_deps_stale:list, release_text:str, week_limit:int,
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


def gather_orphans(branch:str, dep_map:OrderedDict, orphans:list, pagure_dict:dict, release_text:str, unblocked:list, week_limit:int) -> tuple[str,list, list]:
    info = ''
    if not orphans:
        return info,[],[]

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




HEADER = """The following packages are orphaned and will be retired when they
are orphaned for six weeks, unless someone adopts them. If you know for sure
that the package should be retired, please do so now with a proper reason:
https://fedoraproject.org/wiki/How_to_remove_a_package_at_end_of_life

Note: If you received this mail directly you (co)maintain one of the affected
packages or a package that depends on one. Please adopt the affected package or
retire your depending package to avoid broken dependencies, otherwise your
package will be retired when the affected package gets retired.
"""
