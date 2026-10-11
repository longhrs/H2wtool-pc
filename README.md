# H2wTool PC

PC 端测试管理工具：通过 **网卡 (WiFi/以太网)** 或 **USB (adb / 串口)** 接收 Android 设备上
memtest、H2wFlashTool 的测试结果，并可以在 PC 上设置这两个工具的测试参数、下发到设备、远程开始/停止测试。

## 功能

- **多种连接方式，同时生效**
  - 网络：TCP 服务 (默认端口 9527)，支持 UDP 广播自动发现 PC (端口 9528)
  - USB (adb)：自动检测 USB 连接的手机并执行 `adb reverse`，手机连 `127.0.0.1:9527` 即可
  - USB 串口：CDC-ACM / gadget serial / USB 转串口
  - 支持多台设备同时连接
- **测试结果**
  - 实时接收，保存到本地 SQLite 数据库；同一测试重复上报自动去重 (设备可放心重发)
  - 按工具 / 设备 / 结果 / 关键字过滤，PASS/FAIL 着色统计，查看完整原始数据
  - 导出 CSV (Excel 可直接打开)
  - 设备日志按设备按天落盘，设备上传的文件自动保存
- **参数设置**
  - MemTest：内存大小、线程数、循环次数、时长、测试项选择等
  - H2wFlashTool：测试目录、容量、文件大小、块大小、模式、IO 方式、数据模式等
  - 参数界面由 JSON schema 自动生成，字段与 Android 端不一致时只需改 JSON
  - 参数预设保存/加载，导入/导出 JSON
  - 下发到选中设备 / 全部设备，从设备读取当前参数，下发并开始测试，停止测试
- **无界面模式**：`--headless` 只做结果接收与保存，适合产线工位长期运行

## 安装与运行

需要 Python 3.10+。

```bash
pip install -r requirements.txt
python -m h2wtool_pc                 # 图形界面
python -m h2wtool_pc --headless      # 无界面模式
python -m h2wtool_pc --help
```

USB (adb) 方式需要安装 [Android platform-tools](https://developer.android.com/tools/releases/platform-tools)，
并让 `adb` 在 PATH 中 (或在“设置”里指定 adb 路径)，手机需开启 USB 调试。

### 打包成 Windows exe

```bash
pip install pyinstaller
pyinstaller h2wtool_pc.spec
# 输出: dist/H2wToolPC/H2wToolPC.exe
```

可以把 `adb.exe`、`AdbWinApi.dll`、`AdbWinUsbApi.dll` 放到 exe 同目录并在设置中指定 adb 路径。

## 使用流程

1. 启动工具，服务自动开始监听 (状态栏显示监听端口和本机 IP)。
2. 连接设备：
   - USB：插上手机，工具自动完成 `adb reverse`，手机 App 连接 `127.0.0.1:9527`；
   - 网络：手机 App 连接状态栏显示的 PC IP:9527，或用 UDP 发现；
   - 串口：工具栏“打开串口”。
3. 设备握手后出现在左侧“已连接设备”列表。
4. 在“MemTest 设置 / H2wFlashTool 设置”页调整参数，选中设备后点“下发到选中设备”或“下发并开始测试”。
5. 测试结束后结果出现在“测试结果”页，可过滤、查看详情、导出 CSV。

## 数据目录

| 系统 | 默认位置 |
| --- | --- |
| Windows | `%APPDATA%\H2wToolPC` |
| Linux / macOS | `~/.h2wtool_pc` |

可用 `--data-dir` 或环境变量 `H2WTOOL_DATA_DIR` 指定。目录内容：

```
results.db        测试结果数据库
settings.json     设置
presets/<tool>/   参数预设
schemas/          (可选) 覆盖内置参数定义
logs/<device>/    设备日志
files/<device>/   设备上传的文件
```

## Android 端对接

通信协议 (JSON Lines over TCP/串口)、消息格式、参数字段和 Kotlin 参考代码见
[docs/PROTOCOL.md](docs/PROTOCOL.md)。

没有真机时可以用模拟器联调：

```bash
python tools/device_simulator.py --auto memtest --count 3 --fail-rate 0.3
```

## 开发

```
h2wtool_pc/
  protocol.py            协议常量、编解码
  transport/             TCP 服务 / 串口 / UDP 发现
  core/hub.py            设备管理、消息处理、参数下发 (与界面无关)
  core/result_store.py   SQLite 结果存储、CSV 导出
  core/config_schema.py  参数 schema、校验、预设
  core/adb.py            adb 设备扫描与 reverse
  schemas/               memtest / h2wflashtool 参数定义
  gui/                   PySide6 界面
tools/device_simulator.py  Android 端模拟器
tests/                     pytest 测试
```

```bash
pip install -r requirements-dev.txt
pytest
```
