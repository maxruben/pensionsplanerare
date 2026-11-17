"""Uppdaterar FALLBACK_VERSION, FALLBACK_SHA och FALLBACK_DATE i pension_planner.py.

Kör detta INNAN du bygger en .exe så att den hårdkodade fallbacken matchar
nuvarande git-state. Vid .py-körning ignoreras fallbacken (git frågas direkt).

Versionen är v<VERSION_MAJOR>.<antal commits + VERSION_COUNT_OFFSET>.0.
Båda konstanterna läses ur
pension_planner.py så att exe och källkod räknar likadant.

Användning:
    python bump_version.py
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
TARGET = ROOT / 'pension_planner.py'


def _git(*args):
    return subprocess.check_output(
        ['git', *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
    ).strip()


def main():
    text = TARGET.read_text(encoding='utf-8')
    m_major = re.search(r"^VERSION_MAJOR = (\d+)$", text, re.MULTILINE)
    m_offset = re.search(
        r"^VERSION_COUNT_OFFSET = (\d+)$", text, re.MULTILINE,
    )
    if not m_major or not m_offset:
        print("Fel: hittade inte VERSION_MAJOR/VERSION_COUNT_OFFSET.",
              file=sys.stderr)
        sys.exit(2)
    try:
        count = (int(_git('rev-list', '--count', 'HEAD'))
                 + int(m_offset.group(1)))
        sha = _git('rev-parse', '--short', 'HEAD')
        commit_date = _git('show', '-s', '--format=%cs', 'HEAD')
    except subprocess.CalledProcessError as e:
        print(f"Fel: kunde inte läsa git-info: {e}", file=sys.stderr)
        sys.exit(1)

    version = f'v{m_major.group(1)}.{count}.0'
    text, n1 = re.subn(
        r"^FALLBACK_VERSION = '.+'$",
        f"FALLBACK_VERSION = '{version}'",
        text, count=1, flags=re.MULTILINE,
    )
    text, n2 = re.subn(
        r"^FALLBACK_SHA = '.+'$",
        f"FALLBACK_SHA = '{sha}'",
        text, count=1, flags=re.MULTILINE,
    )
    text, n3 = re.subn(
        r"^FALLBACK_DATE = '.+'$",
        f"FALLBACK_DATE = '{commit_date}'",
        text, count=1, flags=re.MULTILINE,
    )
    if n1 != 1 or n2 != 1 or n3 != 1:
        print("Fel: hittade inte FALLBACK_VERSION/SHA/DATE-konstanterna.", file=sys.stderr)
        sys.exit(2)
    TARGET.write_text(text, encoding='utf-8')
    print(
        f"Uppdaterat: FALLBACK_VERSION = '{version}', "
        f"FALLBACK_SHA = '{sha}', FALLBACK_DATE = '{commit_date}'"
    )


if __name__ == '__main__':
    main()
