# H2wTool PC ⇄ Android 通信协议 v1

本文档描述 Android 端 (memtest / H2wFlashTool App) 如何与 PC 端工具通信。

## 1. 传输方式

所有传输方式使用**同一种帧格式**，Android 端只需实现一套消息收发逻辑。

| 方式 | Android 端怎么连 | PC 端识别为 |
| --- | --- | --- |
| 网络 (WiFi / 以太网 / USB 网卡) | TCP 连接 `PC_IP:9527` | `network` |
| USB (adb) | PC 自动执行 `adb reverse tcp:9527 tcp:9527`，Android 连接 `127.0.0.1:9527` | `usb-adb` |
| USB 串口 (CDC-ACM / gadget serial) | 打开串口设备直接收发 | `usb-serial` |

> 推荐 USB 场景使用 adb reverse：无需 root、无需网络、与网络方式代码完全一致。

### 1.1 局域网自动发现 (可选)

Android 向 UDP `255.255.255.255:9528` 广播字符串 `H2W_DISCOVER`，PC 回复 (单播回广播来源地址)：

```json
{"type": "discover_reply", "name": "H2wTool-PC", "tcp_port": 9527, "protocol": 1}
```

收到回复后用 **回复包的来源 IP** + `tcp_port` 建立 TCP 连接。

## 2. 帧格式 (JSON Lines)

- 每条消息是一个 **UTF-8 编码的 JSON 对象**，以 `\n` 结尾，一行一条。
- JSON 内部不能有未转义的换行 (标准 JSON 序列化本来就会转义)。
- 单条消息上限 16 MB。
- 所有消息都有公共字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | string | 消息类型，**必填** |
| `id` | string | 消息唯一 ID (建议 UUID 前 12 位)，对方 `ack.ref` 引用它 |
| `ts` | number | 发送时间，毫秒时间戳 |

未知字段会被忽略/保留，方便向前兼容。

## 3. 会话流程

```
Android                                   PC
   | ---- hello -------------------------->  |  记录设备信息
   | <--- welcome -------------------------  |
   | ---- heartbeat (每 5~10 秒) ----------> |  超过 30 秒无任何数据 PC 会断开
   |                                         |
   | <--- config_set (memtest 参数) -------  |  用户在 PC 上点 “下发”
   | ---- ack {ref, ok} ------------------>  |
   | <--- command {action:"start"} --------  |
   | ---- ack ---------------------------->  |
   | ---- status {state:"running",...} ---> |  周期性上报进度
   | ---- log ---------------------------->  |  可选
   | ---- result -------------------------> |  测试结束
   | <--- ack {ref: result.id} ------------  |  收到 ack 前应缓存结果，断线重连后重发
```

## 4. Android → PC 消息

### hello (连接后第一条)
```json
{"type":"hello","id":"a1b2c3d4e5f6","ts":1760000000000,
 "device_id":"R58M123ABC", "model":"SM-S9280", "android_version":"14",
 "app_version":"1.2.0", "tools":["memtest","h2wflashtool"]}
```
`device_id` 用于区分设备，建议用序列号 (`Build.getSerial()` 或 adb serialno)。其它任意字段 (电量、SoC、内存/存储容量…) 都会被保存在设备信息里。

### heartbeat
```json
{"type":"heartbeat","id":"...","ts":...}
```

### status (进度)
```json
{"type":"status","id":"...","ts":...,"tool":"memtest",
 "state":"running","progress":45,"message":"loop 2/3 walking_ones"}
```
`state`: `idle` | `running` | `done` | `stopped` | `error` | `paused`；`progress` 0–100。

### result (测试结果，最重要)
```json
{"type":"result","id":"...","ts":...,
 "tool":"memtest",
 "test_id":"9f1c2a7d4b3e0011",
 "result":"FAIL",
 "start_time":1760000000000,
 "end_time":1760000360000,
 "duration_s":360,
 "summary":{"size_mb":2048,"loops":3,"errors":2},
 "details":{"failed_tests":["walking_ones"],"first_error_addr":"0x7f3a2c1000"},
 "config":{"size_mb":2048,"loops":3}}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `tool` | 是 | `memtest` / `h2wflashtool` |
| `result` | 是 | `PASS` / `FAIL` / `ABORT` / `ERROR` (也接受 `OK`/`NG`/`passed`/`failed` 等) |
| `test_id` | 建议 | 本次测试唯一 ID。同一设备 + 工具 + test_id 重复上报只保存一次，因此**可放心重发** |
| `start_time` / `end_time` | 否 | 毫秒时间戳或字符串 |
| `duration_s` | 否 | 秒 |
| `summary` | 否 | 扁平的关键指标，PC 列表中直接显示，也会导出到 CSV |
| `details` | 否 | 任意 JSON，PC 详情中查看 |
| `config` | 否 | 本次测试实际使用的参数 |

PC 保存后回复 `ack`；若格式错误回复 `{"type":"ack","ref":"...","ok":false,"error":"..."}`。

建议 H2wFlashTool 的 `summary`：`tested_mb`, `write_mbps`, `read_mbps`, `bad_mb`, `first_bad_offset`。

### log
```json
{"type":"log","id":"...","ts":...,"tool":"h2wflashtool","level":"error","text":"verify mismatch at 0x1F400000"}
```
`level`: `debug` | `info` | `warn` | `error`。PC 会按设备按天写入 `<数据目录>/logs/<device_id>/YYYYMMDD.log`。

### config_report (回复 config_get，或参数变化时主动上报)
```json
{"type":"config_report","id":"...","ts":...,"ref":"<config_get 的 id>","tool":"memtest","config":{...}}
```

### file (上传日志等文件，可选)
```json
{"type":"file","id":"...","ts":...,"tool":"memtest","name":"memtest_20261011.log","data_b64":"..."}
```
保存到 `<数据目录>/files/<device_id>/`，PC 回 ack。大文件请分多个文件发送 (单条消息 ≤16 MB)。

## 5. PC → Android 消息

### welcome
```json
{"type":"welcome","id":"...","ts":...,"ref":"<hello id>","server":"H2wTool-PC","server_version":"0.1.0",
 "protocol":1,"server_time":1760000000000,"heartbeat_interval_s":10}
```

### config_set (下发参数)
```json
{"type":"config_set","id":"...","ts":...,"tool":"memtest","config":{"size_mb":2048,"loops":3,"tests":["stuck_address","walking_ones"]}}
```
Android 应用参数后回复 `ack` (`ok:false` + `error` 表示拒绝)。PC 下发前已按 schema 校验，并补全了所有字段的默认值。

### config_get (读取参数)
```json
{"type":"config_get","id":"...","ts":...,"tool":"memtest"}
```
Android 回复 `ack` + `config_report` (`ref` 为本消息 id)。

### command
```json
{"type":"command","id":"...","ts":...,"tool":"memtest","action":"start"}
```
`action`: `start` | `stop`。PC 的“下发并开始测试”按钮会先发 `config_set` 再发 `command start`。

### ack
```json
{"type":"ack","id":"...","ts":...,"ref":"<被确认消息 id>","ok":true}
```

## 6. 参数定义 (schema)

两个工具的参数字段定义在 `h2wtool_pc/schemas/*.json`，PC 界面根据它自动生成。

**memtest** (`h2wtool_pc/schemas/memtest.json`)

| key | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| size_mb | int | 512 | 测试内存大小 MB，0 = 按 free_percent |
| free_percent | int | 80 | 可用内存比例 |
| threads | int | 0 | 线程数，0 = CPU 核数 |
| loops | int | 1 | 循环次数，0 = 无限 |
| duration_min | int | 0 | 最长时间，0 = 不限 |
| stop_on_error | bool | true | |
| keep_screen_on | bool | true | |
| report_interval_s | int | 5 | 进度上报间隔 |
| tests | string[] | 全部 | stuck_address, random_value, compare_xor, compare_sub, compare_mul, compare_div, compare_or, compare_and, seq_increment, solid_bits, block_seq, checkerboard, bit_spread, bit_flip, walking_ones, walking_zeros, write_8bit, write_16bit |

**h2wflashtool** (`h2wtool_pc/schemas/h2wflashtool.json`)

| key | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| target_path | string | /sdcard/h2w_test | 测试目录 |
| test_size_mb | int | 0 | 测试容量，0 = 全部剩余空间 |
| reserve_mb | int | 512 | 保留空间 |
| file_size_mb | int | 1024 | 单个测试文件大小 |
| mode | enum | write_verify | write_verify / write_only / verify_only |
| block_size_kb | enum(int) | 1024 | 4 / 64 / 512 / 1024 / 4096 |
| io_mode | enum | fsync | buffered / fsync / direct |
| pattern | enum | h2testw | h2testw / random / zeros / ones |
| loops | int | 1 | 0 = 无限 |
| delete_after | bool | true | |
| stop_on_error | bool | true | |
| record_speed | bool | true | |
| report_interval_s | int | 5 | |

字段与 Android 实际实现不一致时，**直接修改 JSON 即可，无需改 PC 代码**：
可以改仓库里的内置文件，或者把同名文件放到 `<数据目录>/schemas/` 下覆盖。
支持的类型：`int`, `float`, `bool`, `string`, `enum`, `multi`。

## 7. Android 端参考实现 (Kotlin)

```kotlin
class H2wPcLink(private val host: String = "127.0.0.1", private val port: Int = 9527) {
    private lateinit var socket: Socket
    private lateinit var writer: BufferedWriter
    private val lock = Any()

    fun connect(onMessage: (JSONObject) -> Unit) {
        socket = Socket(host, port)
        writer = socket.getOutputStream().bufferedWriter(Charsets.UTF_8)
        send("hello", JSONObject()
            .put("device_id", Build.SERIAL_OR_YOUR_ID)
            .put("model", Build.MODEL)
            .put("android_version", Build.VERSION.RELEASE)
            .put("app_version", BuildConfig.VERSION_NAME)
            .put("tools", JSONArray(listOf("memtest", "h2wflashtool"))))
        thread {
            socket.getInputStream().bufferedReader(Charsets.UTF_8).forEachLine { line ->
                if (line.isNotBlank()) onMessage(JSONObject(line))
            }
        }
        thread { while (!socket.isClosed) { Thread.sleep(5000); send("heartbeat", JSONObject()) } }
    }

    fun send(type: String, body: JSONObject): String {
        val id = UUID.randomUUID().toString().replace("-", "").take(12)
        body.put("type", type).put("id", id).put("ts", System.currentTimeMillis())
        synchronized(lock) { writer.write(body.toString()); writer.write("\n"); writer.flush() }
        return id
    }

    fun ack(ref: String, ok: Boolean = true, error: String? = null) =
        send("ack", JSONObject().put("ref", ref).put("ok", ok).apply { error?.let { put("error", it) } })
}

// 处理 PC 下发的消息
link.connect { msg ->
    when (msg.getString("type")) {
        "config_set" -> { applyConfig(msg.getString("tool"), msg.getJSONObject("config")); link.ack(msg.getString("id")) }
        "config_get" -> {
            link.ack(msg.getString("id"))
            link.send("config_report", JSONObject().put("ref", msg.getString("id"))
                .put("tool", msg.getString("tool")).put("config", currentConfig(msg.getString("tool"))))
        }
        "command" -> { link.ack(msg.getString("id")); if (msg.getString("action") == "start") startTest(msg.getString("tool")) else stopTest(msg.getString("tool")) }
    }
}
```

网络方式把 `host` 换成 PC 的 IP (或用 UDP 发现得到的地址)；串口方式把 Socket 换成串口流，其余不变。
Android 9+ 连接明文 TCP 无需额外配置 (只有 HTTP 受 cleartext 限制)，但需要 `INTERNET` 权限。

## 8. 联调

仓库自带一个 Android 端模拟器，完全按本协议实现：

```bash
python tools/device_simulator.py --host 127.0.0.1 --port 9527              # 等待 PC 下发参数/命令
python tools/device_simulator.py --auto memtest --count 5 --fail-rate 0.3  # 自动跑 5 次并上报
python tools/device_simulator.py --discover                                # UDP 发现 PC
```
