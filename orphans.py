import os
import time
from typing import Final

import dogpile.cache
import koji
import requests

from releases import eprint, PAGURE_URL, ORPHAN_UID

FEDORA_PROJECT_URL:Final = f'{PAGURE_URL}/api/0/projects'
PAGURE_MAX_ENTRIES_PER_PAGE:Final = 100

cache = dogpile.cache.make_region().configure(
    'dogpile.cache.dbm',
    expiration_time=86400,
    arguments=dict(
        filename=os.path.expanduser('~/.cache/dist-git-orphans-cache.dbm')),
)


@cache.cache_on_arguments()
def __get_pagure_orphans(namespace:str, page:int=1) -> tuple[dict, int]:
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
def orphan_packages(namespace:str='rpms') -> dict:
    pkgs, pages = __get_pagure_orphans(namespace=namespace)
    eprint(f"({pages} pages)", end=" ")
    for page in range(2, pages + 1):
        eprint("." if page %10 else page, end="")
        new_pkgs, _ = __get_pagure_orphans(namespace, page)
        pkgs.update(new_pkgs)
    return pkgs



def unblocked_packages(packages:list, tag_id:dict, kojihub:dict) -> list:
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



