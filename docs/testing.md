# Testing and contribution validation

Public automated tests use mocked Home Assistant entities and services. They do
not require access to a real installation or credentials. GitHub Actions uses
hosted runners with read-only permissions for pull requests.

The validated environments are:

| Home Assistant | Python | pytest | HA pytest plugin |
| --- | --- | --- | --- |
| 2025.8.0 | 3.13 | 8.4.1 | 0.13.269 |
| 2026.8.0 | 3.14 | 9.0.3 | 0.13.354 |

Install `requirements-ci/ha-current.txt` for Python 3.14 or
`requirements-ci/ha-minimum.txt` for Python 3.13, then run
`python -m pytest tests -q`. Python 3.14.2 or newer is required for the 2026.8 stack.
The CI check results provide the current public test count; private deployment
and old publishing-tool tests are intentionally outside this repository.

Before importing 0.4.0rc1, the complete development suite passed 1280 checks in each
of these environments. The candidate also passed 49 HA service-level scenarios on
HA 2026.3.1, covering control state, current delivery, hybrid/grid budgets, phases,
forecast/runtime planning and battery-cap restart/recovery behavior.

A read-only observation then recorded 1788 samples over 14 hours 53 minutes, including
local midnight, without unexpected availability errors. The user accepted the
shortened observation instead of waiting the planned 24 hours. This is separate
from accelerated day/night scenarios and does not certify physical inverter or EV
behavior. Installation-specific records and credentials remain private.

For a useful bug report, include versions, relevant configuration with secrets
removed, timestamps, the displayed decision reason, observed/commanded values and
a bounded log excerpt. Do not share access tokens or full installation backups.

Before committing, run `python scripts/check_public_tree.py`. The guard examines
both index blobs and tracked working files; sanitizing a working file without
restaging cannot hide the previously staged contents. CI additionally scans every
introduced commit, including files added and deleted within the same branch.

To build a manual-installation package, stage the intended source and run
`python scripts/package_release.py --output dist/pv-excess-control.zip`.
Component files and LICENSE must match the index. Untracked files are excluded;
production access and credentials are not needed.

New public documentation must be added to `scripts/public-docs.txt`. This reviewed
list and the allowed source-file layout prevent accidental publication of local
configuration or unrelated documents without relying on particular filenames.
