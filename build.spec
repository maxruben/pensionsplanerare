# PyInstaller-spec för pension_planner.exe
# Bygg: pyinstaller build.spec
# Resultat: dist/pension_planner.exe

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Selenium lazy-importerar underliggande drivrutiner via strängar (ex.
# selenium.webdriver.chrome.webdriver) som PyInstaller inte hittar via
# statisk analys. collect_submodules('selenium') tar med alla dess
# submoduler så minpension-wizarden fungerar i .exe:n.
_selenium_submodules = collect_submodules('selenium')

a = Analysis(
    ['pension_planner.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'parsing',
        # Lazy-importerade från knappar/menyer — PyInstaller ser dem inte annars
        'minpension_wizard',
        'insurance_income_window',
        'monthly_detail_window',
        'expense_window',
    ] + _selenium_submodules,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Onödiga backend-moduler i matplotlib (vi använder TkAgg)
        'matplotlib.backends.backend_qt5agg',
        'matplotlib.backends.backend_qt5',
        'matplotlib.backends.backend_qtagg',
        'matplotlib.backends.backend_qt',
        'matplotlib.backends.backend_gtk3agg',
        'matplotlib.backends.backend_gtk3',
        'matplotlib.backends.backend_gtk4agg',
        'matplotlib.backends.backend_wxagg',
        'matplotlib.backends.backend_wx',
        'matplotlib.backends.backend_cairo',
        'matplotlib.backends.backend_webagg',
        'matplotlib.backends.backend_nbagg',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'PIL.ImageQt',
        # Utveckling / testning / notebook
        # (unittest kan INTE excludeas: pyparsing.testing importerar den
        # och matplotlib importerar pyparsing vid start)
        'IPython', 'jupyter', 'notebook', 'jedi', 'parso',
        'pytest',
        'setuptools._distutils',
        'lib2to3',
        'pydoc_data',
        # Onödiga numpy sub-moduler
        'numpy.tests',
        # Onödiga matplotlib sample-data
        'matplotlib.tests', 'matplotlib.sample_data',
    ],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
# --onedir-läge: liten laddar-exe + support-mapp med DLL:er och Python-runtime.
# --onefile skapar en självpackande exe som matchar malware-heuristik och
# konsekvent flaggas av Windows Defender även utan UPX. --onedir har
# betydligt lägre false-positive-frekvens.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='pension_planner',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='pension_planner',
)
