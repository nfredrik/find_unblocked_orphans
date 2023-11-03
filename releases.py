import sys
from typing import Final

LISTS_FEDORAPROJECT_ORG:Final = 'devel@lists.fedoraproject.org'

FEDORAPROJECT_ORG:Final = 'epel-announce@lists.fedoraproject.org'

KOJIHUB = 'https://koji.fedoraproject.org/kojihub'

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


def eprint(*args, **kwargs) -> None:
    kwargs.setdefault('file', sys.stderr)
    kwargs.setdefault('flush', True)
    print(*args, **kwargs)


PAGURE_URL:Final = 'https://src.fedoraproject.org'
ORPHAN_UID:Final = 'orphan'
