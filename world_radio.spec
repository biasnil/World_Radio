# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for World Radio.

Build with:
    pyinstaller world_radio.spec

...or just run build_windows.bat, which sets up a clean environment and
does this for you. Produces dist\\World Radio\\World Radio.exe.

Requirements:
    - VLC media player installed on the machine that RUNS this build
      (not just the one that builds it) - python-vlc talks to the
      system VLC install; VLC itself isn't bundled by PyInstaller.
      https://www.videolan.org/vlc/
    - Everything in requirements.txt installed in the environment you
      run PyInstaller from - it inspects your active environment to
      decide what to bundle.

Why --onedir (this spec) instead of --onefile: a --onefile build
re-extracts itself to a temp folder on every launch (slower startup),
and - before common/paths.py was added to resolve a stable base
directory - was also the reason settings didn't persist between runs.
--onedir starts faster and keeps settings.json / app_settings.json /
countries.geo.json sitting right next to the .exe, visible and easy to
back up or delete. To switch to --onefile instead: set
exclude_binaries=False on the EXE(...) call below, pass a.binaries and
a.datas into EXE(...) directly, and drop the COLLECT(...) step -
everything else here still applies.
"""

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("matplotlib") + [
    ("assets/icon.png", "assets"),
    ("assets/icon.ico", "assets"),
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "matplotlib.backends.backend_tkagg",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="World Radio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # GUI app - no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",  # the .exe file's own icon (Explorer, taskbar) - separate from the in-app window icon
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="World Radio",
)
