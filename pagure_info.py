import datetime
import sys
import traceback
from urllib.error import HTTPError

import requests

from releases import RAWHIDE_RELEASE, eprint, PAGURE_URL


class PagureInfo:
    def __init__(self, package:str, branch:dict=RAWHIDE_RELEASE["pagure_branch"], ns:str='rpms') -> None:
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
        the_date ="date_modified" if "date_modified" in self.pkginfo else "date_created"
        status_change = float(self.pkginfo[the_date])
        return datetime.datetime.fromtimestamp(status_change, tz=datetime.timezone.utc)

    def __getitem__(self, *args, **kwargs):
        return self.pkginfo.__getitem__(*args, **kwargs)
