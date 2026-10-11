# PyInstaller 打包配置: pyinstaller h2wtool_pc.spec
# 输出 dist/H2wToolPC/ 目录，包含:
#   H2wToolPC.exe           图形界面
#   H2wToolPC-cli.exe       命令行 / 无界面模式 (--headless)
#   H2wDeviceSimulator.exe  Android 端模拟器，用于没有真机时验证
# -*- mode: python ; coding: utf-8 -*-

datas = [("h2wtool_pc/schemas/*.json", "h2wtool_pc/schemas")]

a = Analysis(
    ["run.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=["serial.tools.list_ports"],
    excludes=["tkinter"],
)
sim = Analysis(
    ["tools/device_simulator.py"],
    pathex=["."],
    datas=datas,
    excludes=["tkinter", "PySide6"],
)

pyz = PYZ(a.pure)
sim_pyz = PYZ(sim.pure)

gui_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="H2wToolPC", console=False)
cli_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="H2wToolPC-cli", console=True)
sim_exe = EXE(sim_pyz, sim.scripts, [], exclude_binaries=True, name="H2wDeviceSimulator", console=True)

coll = COLLECT(
    gui_exe, cli_exe, sim_exe,
    a.binaries, a.datas, sim.binaries, sim.datas,
    name="H2wToolPC",
)
