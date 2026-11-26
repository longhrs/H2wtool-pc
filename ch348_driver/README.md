# CH348 USB 转 8 串口驱动（Linux 4.19）

基于 usb-serial 框架的 WCH CH348（VID:PID `1a86:55d9`）驱动，8 个串口注册为 `/dev/ttyUSB0` ~ `/dev/ttyUSB7`。
协议参照 WCH 官方开源驱动 `ch9344ser_linux`（GPL-2.0+）。

已在 Linux 4.19.325 源码树上用 `W=1` 编译通过，无警告、无错误。

## 编译

需要目标内核的头文件或源码树，并启用 `CONFIG_USB_SERIAL`（y 或 m）。

```sh
make                                   # 对当前运行的内核编译
make KDIR=/path/to/linux-4.19          # 指定内核源码树（交叉编译时再加 ARCH= CROSS_COMPILE=）
```

ARM 板子交叉编译示例：

```sh
make KDIR=/path/to/linux-4.19 ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-
```

## 加载

```sh
sudo modprobe usbserial
sudo insmod ch348.ko
dmesg | tail          # 应看到 "CH348L/Q, version 0x.." 以及 ttyUSB0..7
```

如果系统里已装了官方驱动 `ch9344.ko`，请先 `rmmod ch9344`，否则两个驱动会抢同一个设备。

## 支持的功能

- 波特率 50 ~ 6000000，数据位 5/6/7/8，停止位 1/2，校验 无/奇/偶/Mark/Space
- 硬件流控 RTS/CTS（`CRTSCTS`）
- DTR/RTS 输出，CTS/DSR/RI/DCD 输入（`TIOCMGET`/`TIOCMSET`/`TIOCMIWAIT`）
- Break、`TIOCGICOUNT` 错误计数（overrun/parity/frame/break）
- CH348Q 的 4~7 号口没有 Modem 线，驱动会自动跳过对这几个口的 DTR/RTS 操作

## 不支持的功能

- GPIO（官方驱动通过 `/dev/ch9344_iodev*` 提供）
- 单个端口的流量节流（throttle）：8 个口共用一个 RX 端点，tty 缓冲满时多出来的数据会丢弃，计入 `buf_overrun`

## 测试

```sh
stty -F /dev/ttyUSB0 115200 cs8 -cstopb -parenb raw -echo
cat /dev/ttyUSB0 &
echo hello > /dev/ttyUSB0      # 把 TX/RX 短接做回环测试
```
