# -*- mode: python ; coding: utf-8 -*-
# Сборка: pyinstaller russificator.spec  ->  dist/Russificator/Russificator.exe (папка целиком — дистрибутив)
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("russificator/resources", "russificator/resources"), ("russificator/ui/web", "russificator/ui/web")]
binaries = []
hiddenimports = collect_submodules("russificator")
for pkg in ("webview", "ctranslate2", "sentencepiece", "UnityPy", "TypeTreeGeneratorAPI", "texture2ddecoder",
            "etcpak", "astc_encoder", "fmod_toolkit", "archspec", "clr_loader", "pythonnet", "tpk_ar", "onnxruntime"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ["run_gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["clr", "yaml", "ctranslate2.converters"],
    excludes=["tkinter", "matplotlib", "scipy", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Russificator",
    icon="russificator/resources/icon.ico",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Russificator")
