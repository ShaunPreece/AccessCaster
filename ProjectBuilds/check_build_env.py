"""Check that the Python running the build is 3.12 with every pin installed at its pinned version.

PyInstaller treats an import it cannot resolve as a warning, not an error: a package missing from
the build Python is left out of the bundle, the freeze succeeds, and the frozen app dies on its
first import of it. build_installer.ps1 runs this in its venv before the freeze and stops on any
package that is missing or on a different version from its pin.

Reads ProjectBuilds/requirements-build.txt, following its `-r` includes. Every requirement in
those files is an exact `name==version` pin; anything else is reported rather than guessed at.
Exit code 0 when everything matches, 1 otherwise, with one line per problem.
"""

import re
import sys
from importlib import metadata
from pathlib import Path

_PIN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)$')


def _pins(path, seen=None):
    """Yield (name, version, file) for every pin in `path` and the files it includes."""
    seen = set() if seen is None else seen
    path = path.resolve()
    if path in seen:
        return
    seen.add(path)
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if line.startswith('-r '):
            yield from _pins(path.parent / line[3:].strip(), seen)
            continue
        m = _PIN.match(line)
        if not m:
            raise SystemExit(f'{path.name}: not an exact pin, so it cannot be checked: {line!r}')
        yield m.group(1), m.group(2), path.name


def problems(requirements):
    """Return one message per pin the running Python does not satisfy."""
    found = []
    for name, wanted, source in _pins(requirements):
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            found.append(f'{name}=={wanted} ({source}) is not installed')
            continue
        if have != wanted:
            found.append(f'{name}=={wanted} ({source}) is installed as {have}')
    return found


def main():
    requirements = Path(__file__).with_name('requirements-build.txt')
    found = problems(requirements)
    if sys.version_info[:2] != (3, 12):
        found.insert(0, f'Python is {sys.version.split()[0]}, and the app is built on 3.12')
    if found:
        print(f'The build Python ({sys.executable}) does not match the pinned requirements:')
        for line in found:
            print(f'  {line}')
        print(r'Delete ProjectBuilds\.venv and run the build again: it reinstalls every pin.')
        return 1
    print('Build requirements: every pin is installed at its pinned version.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
