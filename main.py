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
import json

from deep_checker import DepChecker, setup_dnf
from information import package_info, send_mail, HEADER
from releases import RELEASES, eprint
from orphans import orphan_packages, unblocked_packages

FOOTER = """-- \nThe script creating this output is run and developed by Fedora
Release Engineering. Please report issues at its pagure instance:
https://pagure.io/releng/
The sources of this script can be found at:
https://pagure.io/releng/blob/main/f/scripts/find_unblocked_orphans.py
"""

def prepare_for_mail(args, addresses, text, release) -> None:
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    subject = f"Orphaned Packages in {args.release} ({today})"
    mailto = args.mailto or release["mailto"]
    bcc = addresses + release["bcc"] if args.send else None
    send_mail(args.mailfrom, mailto, subject, text, bcc)

def save_to_json(json_filename:str, depchecker:DepChecker, orphans:list) -> None:
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


def main(args):

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
    #text = f"\nReport started at {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n\n"

    eprint('Getting builds from koji...', end=' ')
    allpkgs = sorted(list(set(list(orphans) + args.failed)))
    unblocked = unblocked_packages(allpkgs, tag_id=koji_tag, kojihub=koji_hub) if args.skipblocked else allpkgs
    eprint('done')

    text += HEADER.format(koji_tag.upper())
    eprint("Setting up dependency checker...", end=' ')

    dnf_base = setup_dnf(repo=repo, source_repo=source_repo)
    depchecker = DepChecker(dnf_base, branch)
    eprint("done")

    eprint('Calculating dependencies...', end=' ')
    # Create dnf object and depsolve out if requested.
    eprint("TODO: add app args to either depsolve or not")
    dep_map, incomplete = depchecker.recursive_deps(unblocked, args.max_deps)
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
        save_to_json(json_filename = args.json, depchecker = depchecker, orphans = orphans)

    if args.mailto or args.send:
        prepare_for_mail(args, addresses, text, release)
    eprint(f"Addresses ({len(addresses)}):", ", ".join(addresses))


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
    main(args)
