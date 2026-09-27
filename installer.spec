# -*- mode: python ; coding: utf-8 -*-
# Сборка установщика: pyinstaller installer.spec  ->  dist/RussificatorSetup.exe (один файл).
# Его кладёт в каждый архив «для друзей» программа (resources/installer/RussificatorSetup.exe),
# поэтому он маленький: только ядро, движки Ren'Py / RPG Maker / Unity (без UnityPy) и окно на Tk.
from PyInstaller.utils.hooks import collect_submodules

hidden = []
for pkg in ("russificator.core", "russificator.engines", "russificator.fonts", "russificator.installer"):
    hidden += collect_submodules(pkg)
hidden += ["russificator.library", "russificator.branding", "russificator.paths",
           "russificator.translation.filters", "russificator.translation.markup"]

datas = [
    ("russificator/resources/fonts", "russificator/resources/fonts"),
    ("russificator/resources/rpgmaker", "russificator/resources/rpgmaker"),
    ("russificator/resources/installer/logo48.png", "russificator/resources/installer"),
    ("russificator/resources/installer/logo96.png", "russificator/resources/installer"),
    ("russificator/resources/icon.ico", "russificator/resources"),
]

a = Analysis(
    ["run_installer.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    excludes=["numpy", "PIL", "UnityPy", "webview", "ctranslate2", "sentencepiece", "clr", "pythonnet",
              "clr_loader", "TypeTreeGeneratorAPI", "pytest", "matplotlib", "scipy", "IPython",
              "russificator.ui", "russificator.translation.machine", "russificator.translation.local_llm",
              "russificator.translation.cloud", "russificator.translation.llm", "russificator.translation.service"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RussificatorSetup",
    icon="russificator/resources/icon.ico",
    console=False,
    upx=False,
    runtime_tmpdir=None,
)
