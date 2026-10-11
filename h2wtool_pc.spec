# PyInstaller 打包配置: pyinstaller h2wtool_pc.spec
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["run.py"],
    pathex=["."],
    datas=[("h2wtool_pc/schemas/*.json", "h2wtool_pc/schemas")],
    hiddenimports=["serial.tools.list_ports"],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="H2wToolPC", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="H2wToolPC")
