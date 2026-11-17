"""Bygg pensionsplanerare-distribution i ett steg.

Användning:
    python build.py

Gör:
1. Rensar build/ och dist/
2. Genererar Saved/demo_planer.json (via scripts/generate_demo.py)
3. Kör PyInstaller (build.spec, --onedir-läge)
4. Kopierar README.md, BERAKNINGAR.md, docs/screenshots/ och
   Saved/demo_planer.json in i dist/pension_planner/
5. Skapar dist/pensionsplanerare.zip

Resultatet i dist/pensionsplanerare.zip kan laddas upp direkt till en
GitHub Release.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / 'dist'
BUILD_DIR = ROOT / 'build'
APP_DIR = DIST_DIR / 'pension_planner'
ZIP_PATH = DIST_DIR / 'pensionsplanerare.zip'

DEMO_SCRIPT = ROOT / 'scripts' / 'generate_demo.py'
EXTRA_FILES = [
    ROOT / 'README.md',
    ROOT / 'BERAKNINGAR.md',
    ROOT / 'Saved' / 'demo_planer.json',
]
EXTRA_DIRS = [
    (ROOT / 'docs' / 'screenshots', APP_DIR / 'docs' / 'screenshots'),
]


def step(msg: str) -> None:
    print(f'\n== {msg}', flush=True)


def clean() -> None:
    step('Rensar build/ och dist/')
    for d in (BUILD_DIR, DIST_DIR):
        if not d.exists():
            continue
        # Radera innehållet men INTE själva mappen — Windows nekar
        # rmtree om ett annat program (t.ex. terminalen) har mappen öppen.
        for entry in d.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        print(f'   tömt {d.name}/')


def run_pyinstaller() -> None:
    step('Kör PyInstaller')
    cmd = [sys.executable, '-m', 'PyInstaller', 'build.spec', '--noconfirm']
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        sys.exit(f'PyInstaller misslyckades (exit {result.returncode})')
    if not (APP_DIR / 'pension_planner.exe').exists():
        sys.exit(f'Förväntad exe saknas: {APP_DIR / "pension_planner.exe"}')


def generate_demo() -> None:
    step('Genererar Saved/demo_planer.json')
    result = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT)], cwd=ROOT,
    )
    if result.returncode != 0:
        sys.exit(f'generate_demo.py misslyckades (exit {result.returncode})')


def copy_extras() -> None:
    step('Kopierar dokumentation och demo-plan')
    for src in EXTRA_FILES:
        if not src.exists():
            sys.exit(f'Saknar källfil: {src}')
        dst = APP_DIR / src.name
        shutil.copy2(src, dst)
        print(f'   {src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}')
    for src_dir, dst_dir in EXTRA_DIRS:
        if not src_dir.exists():
            sys.exit(f'Saknar källmapp: {src_dir}')
        if dst_dir.exists():
            shutil.rmtree(dst_dir)
        shutil.copytree(src_dir, dst_dir)
        print(f'   {src_dir.relative_to(ROOT)}/ -> {dst_dir.relative_to(ROOT)}/')


def make_zip() -> None:
    step('Skapar zip')
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(APP_DIR.rglob('*')):
            if path.is_file():
                arcname = path.relative_to(DIST_DIR)
                zf.write(path, arcname)
    size_mb = ZIP_PATH.stat().st_size / (1024 * 1024)
    print(f'   {ZIP_PATH.relative_to(ROOT)}  ({size_mb:.1f} MB)')


def main() -> None:
    os.chdir(ROOT)
    clean()
    generate_demo()
    run_pyinstaller()
    copy_extras()
    make_zip()
    print('\nKlar. Ladda upp dist/pensionsplanerare.zip till GitHub Releases.')


if __name__ == '__main__':
    main()
