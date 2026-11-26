// SPDX-License-Identifier: GPL-2.0+
/*
 * USB serial driver for the WCH CH348 USB to octal UART converter.
 *
 * Target kernel: Linux 4.19.x (usb-serial framework, /dev/ttyUSBn).
 *
 * The protocol implemented here follows the vendor driver published by
 * Nanjing Qinheng Microelectronics (WCH), "ch9344ser_linux" (GPL-2.0+).
 *
 * Device layout
 * -------------
 * One vendor-specific interface with two pairs of bulk endpoints:
 *
 *   pair 0 (data):   IN  - RX data of all 8 UARTs, multiplexed in 32-byte
 *                          chunks:  [port][len][data 0..30]
 *                    OUT - TX data: [port][len_lo][len_hi][data ...]
 *   pair 1 (cmd):    OUT - register writes / configuration commands
 *                    IN  - asynchronous status (LSR, MSR, TX-empty, ...)
 *
 * After a TX packet has been sent for a port, the next packet for that port
 * is only sent once the chip reports "TX empty" for it on the status pipe
 * (with a baud-rate based timeout as a safety net, as the vendor driver does).
 *
 * All 8 ports share the single RX URB set of port 0, so the driver does not
 * implement per-port throttling; data that does not fit into the tty flip
 * buffer is dropped and accounted as buf_overrun.
 */

#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/slab.h>
#include <linux/tty.h>
#include <linux/tty_flip.h>
#include <linux/serial.h>
#include <linux/usb.h>
#include <linux/usb/serial.h>
#include <linux/kfifo.h>
#include <linux/timer.h>
#include <linux/random.h>
#include <linux/bitops.h>

#define DRIVER_DESC	"WCH CH348 USB to 8 x UART driver"

#define CH348_VID		0x1a86
#define CH348_PID		0x55d9

#define CH348_NUM_PORTS		8
#define CH348_CMD_TIMEOUT	2000	/* ms */
#define CH348_FIFO_SIZE		4096	/* per-port software TX fifo */
#define CH348_TX_HDR		3
#define CH348_RX_CHUNK		32
#define CH348_RX_MAX_DATA	30
#define CH348_TX_TIMEOUT_MIN	1000	/* ms, safety net for TX-empty */
#define CH348_DEFAULT_BAUD	115200
#define CH348_MIN_BAUD		50
#define CH348_MAX_BAUD		6000000

/* command opcodes (first byte on the cmd endpoint) */
#define CMD_W_R		0xC0	/* write full register         */
#define CMD_W_BR	0x80	/* write single register bit    */
#define CMD_WB_E	0x90	/* extended / global command    */

/* vendor control requests */
#define CMD_VER		0x96	/* read chip version (4 bytes)  */

/* extended command registers (second byte) */
#define VEN_R		0x85
#define R_MOD		0x97
#define R_TM_O		0x9C
#define R_UP_O		0x9D
#define R_INIT		0xA1
#define R_IO_CE		0xA3
#define R_IO_CI		0xA7
#define R_EE_CFG	0xA9

/* 16550-like UART registers (relative to the port's register base) */
#define R_C1		0x01	/* IER */
#define R_C2		0x02	/* FCR */
#define R_C3		0x03	/* LCR */
#define R_C4		0x04	/* MCR */
#define R_C6		0x06	/* MSR */

/* status IIR codes (low nibble) */
#define R_II_B1		0x06	/* line status   */
#define R_II_B2		0x02	/* TX empty      */
#define R_II_B3		0x00	/* modem status  */

/* LSR bits reported on the status pipe */
#define CH348_LSR_OE	0x02
#define CH348_LSR_PE	0x04
#define CH348_LSR_FE	0x08
#define CH348_LSR_BI	0x10

/* bit-write values for MCR (CMD_W_BR): (bit << 4) | value */
#define MCR_DTR_OFF	0x00
#define MCR_DTR_ON	0x01
#define MCR_RTS_OFF	0x10
#define MCR_RTS_ON	0x11
#define MCR_AFE_OFF	0x50	/* hardware flow control off */
#define MCR_AFE_ON	0x51	/* hardware flow control on  */
/* bit-write values for LCR (CMD_W_BR) */
#define LCR_BREAK_OFF	0x60
#define LCR_BREAK_ON	0x61

/* uart modes */
#define M_NOR		0x00
#define M_HF		0x03

struct ch348;

struct ch348_port {
	struct ch348 *ch348;
	unsigned int portnum;

	spinlock_t lock;	/* protects the TX state and msr below */
	struct kfifo fifo;
	struct urb *write_urb;
	u8 *write_buf;
	bool opened;
	bool urb_busy;		/* write URB in flight                */
	bool tx_empty;		/* chip reported its TX FIFO is empty */
	unsigned int tx_bytes;	/* bytes of the last packet sent      */
	struct timer_list tx_timer;

	unsigned int baud;
	unsigned int msr;	/* TIOCM_CTS/DSR/RI/CD */

	struct mutex mctrl_lock;	/* serializes DTR/RTS updates */
	unsigned int mctrl;	/* TIOCM_DTR/RTS */
	u8 uartmode;
};

struct ch348 {
	struct usb_serial *serial;
	struct usb_device *udev;

	unsigned int data_out_pipe;
	unsigned int cmd_pipe;
	size_t write_size;		/* max packet size of data OUT */

	struct urb *status_urb;
	u8 *status_buf;
	size_t status_size;

	bool chip_q;			/* CH348Q: ports 4..7 have no modem lines */
	u8 chipver;

	struct ch348_port ports[CH348_NUM_PORTS];
};

static inline u8 ch348_reg_base(unsigned int portnum)
{
	if (portnum < 4)
		return 0x10 * portnum;
	return 0x10 * (portnum - 4) + 0x08;
}

static inline bool ch348_has_modem(struct ch348 *ch348, unsigned int portnum)
{
	return !(ch348->chip_q && portnum > 3);
}

/* ------------------------------------------------------------------------ */
/* command pipe                                                              */
/* ------------------------------------------------------------------------ */

static int ch348_cmd(struct ch348 *ch348, const u8 *data, int len)
{
	u8 *buf;
	int actual = 0;
	int ret;

	buf = kmemdup(data, len, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;

	ret = usb_bulk_msg(ch348->udev, ch348->cmd_pipe, buf, len, &actual,
			   CH348_CMD_TIMEOUT);
	kfree(buf);

	if (ret) {
		dev_err(&ch348->serial->interface->dev,
			"command 0x%02x/0x%02x failed: %d\n",
			data[0], len > 1 ? data[1] : 0, ret);
		return ret;
	}
	if (actual != len)
		return -EIO;

	return 0;
}

static int ch348_write_reg(struct ch348 *ch348, unsigned int portnum,
			   u8 op, u8 reg, u8 val)
{
	u8 buf[3];

	buf[0] = op;
	buf[1] = ch348_reg_base(portnum) + reg;
	buf[2] = val;

	return ch348_cmd(ch348, buf, sizeof(buf));
}

static int ch348_set_mcr_bit(struct ch348 *ch348, unsigned int portnum, u8 v)
{
	return ch348_write_reg(ch348, portnum, CMD_W_BR, R_C4, v);
}

static int ch348_request_msr(struct ch348 *ch348, unsigned int portnum)
{
	u8 buf[3];

	buf[0] = CMD_WB_E;
	buf[1] = VEN_R;
	buf[2] = ch348_reg_base(portnum) | R_C6;

	return ch348_cmd(ch348, buf, sizeof(buf));
}

static int ch348_set_uartmode(struct ch348 *ch348, unsigned int portnum,
			      u8 mode)
{
	struct ch348_port *p = &ch348->ports[portnum];
	int ret;

	if (p->uartmode == mode)
		return 0;

	ret = ch348_set_mcr_bit(ch348, portnum,
				mode == M_HF ? MCR_AFE_ON : MCR_AFE_OFF);
	if (!ret)
		p->uartmode = mode;

	return ret;
}

static int ch348_update_mctrl(struct ch348 *ch348, unsigned int portnum,
			      unsigned int set, unsigned int clear)
{
	struct ch348_port *p = &ch348->ports[portnum];
	unsigned int newctrl, diff;
	int ret = 0;

	if (!ch348_has_modem(ch348, portnum))
		return 0;

	mutex_lock(&p->mctrl_lock);
	newctrl = (p->mctrl & ~clear) | set;
	diff = newctrl ^ p->mctrl;

	if (diff & TIOCM_DTR) {
		ret = ch348_set_mcr_bit(ch348, portnum,
				(newctrl & TIOCM_DTR) ? MCR_DTR_ON : MCR_DTR_OFF);
		if (ret)
			goto out;
	}
	if (diff & TIOCM_RTS) {
		ret = ch348_set_mcr_bit(ch348, portnum,
				(newctrl & TIOCM_RTS) ? MCR_RTS_ON : MCR_RTS_OFF);
		if (ret)
			goto out;
	}
	p->mctrl = newctrl;
out:
	mutex_unlock(&p->mctrl_lock);
	return ret;
}

/* ------------------------------------------------------------------------ */
/* TX path                                                                   */
/* ------------------------------------------------------------------------ */

static unsigned long ch348_tx_timeout(unsigned int baud, unsigned int bytes)
{
	unsigned int ms;

	if (!baud)
		baud = 9600;
	/* 12 bits per char upper bound, doubled, plus a fixed margin */
	ms = DIV_ROUND_UP(bytes * 12 * 1000, baud) * 2 + CH348_TX_TIMEOUT_MIN;

	return msecs_to_jiffies(ms);
}

/* Send the next packet for a port if allowed. Callable from any context. */
static void ch348_kick(struct ch348 *ch348, unsigned int portnum)
{
	struct ch348_port *p = &ch348->ports[portnum];
	struct usb_serial_port *port = ch348->serial->port[portnum];
	unsigned long flags;
	unsigned int count;
	int ret;

	spin_lock_irqsave(&p->lock, flags);
	if (!p->opened || p->urb_busy || !p->tx_empty)
		goto out;

	count = kfifo_out(&p->fifo, p->write_buf + CH348_TX_HDR,
			  ch348->write_size - CH348_TX_HDR);
	if (!count)
		goto out;

	p->write_buf[0] = portnum;
	p->write_buf[1] = count & 0xff;
	p->write_buf[2] = (count >> 8) & 0xff;
	p->write_urb->transfer_buffer_length = count + CH348_TX_HDR;

	p->urb_busy = true;
	p->tx_empty = false;
	p->tx_bytes = count;

	ret = usb_submit_urb(p->write_urb, GFP_ATOMIC);
	if (ret) {
		p->urb_busy = false;
		p->tx_empty = true;
		p->tx_bytes = 0;
		spin_unlock_irqrestore(&p->lock, flags);
		if (ret != -EPERM && ret != -ENODEV)
			dev_err_ratelimited(&port->dev,
					    "failed to submit write urb: %d\n",
					    ret);
		return;
	}

	mod_timer(&p->tx_timer, jiffies + ch348_tx_timeout(p->baud, count));
	port->icount.tx += count;
	spin_unlock_irqrestore(&p->lock, flags);

	/* fifo space was freed, let the line discipline write more */
	usb_serial_port_softint(port);
	return;
out:
	spin_unlock_irqrestore(&p->lock, flags);
}

static void ch348_write_bulk_callback(struct urb *urb)
{
	struct usb_serial_port *port = urb->context;
	struct ch348 *ch348 = usb_get_serial_data(port->serial);
	struct ch348_port *p = &ch348->ports[port->port_number];
	int status = urb->status;
	unsigned long flags;

	spin_lock_irqsave(&p->lock, flags);
	p->urb_busy = false;
	if (status) {
		/* the chip never got the data, no TX-empty will follow */
		p->tx_empty = true;
		p->tx_bytes = 0;
	}
	spin_unlock_irqrestore(&p->lock, flags);

	switch (status) {
	case 0:
		break;
	case -ENOENT:
	case -ECONNRESET:
	case -ESHUTDOWN:
		return;
	default:
		dev_err_ratelimited(&port->dev, "write urb failed: %d\n",
				    status);
		break;
	}

	ch348_kick(ch348, port->port_number);
	usb_serial_port_softint(port);
}

static void ch348_tx_done(struct ch348 *ch348, unsigned int portnum)
{
	struct ch348_port *p = &ch348->ports[portnum];
	unsigned long flags;

	spin_lock_irqsave(&p->lock, flags);
	p->tx_empty = true;
	p->tx_bytes = 0;
	spin_unlock_irqrestore(&p->lock, flags);

	ch348_kick(ch348, portnum);
	usb_serial_port_softint(ch348->serial->port[portnum]);
}

static void ch348_tx_timer(struct timer_list *t)
{
	struct ch348_port *p = from_timer(p, t, tx_timer);
	unsigned long flags;
	bool expired = false;

	spin_lock_irqsave(&p->lock, flags);
	if (p->opened && !p->tx_empty) {
		if (p->urb_busy) {
			/* URB still in flight, check again later */
			mod_timer(&p->tx_timer, jiffies +
				  msecs_to_jiffies(CH348_TX_TIMEOUT_MIN));
		} else {
			p->tx_empty = true;
			p->tx_bytes = 0;
			expired = true;
		}
	}
	spin_unlock_irqrestore(&p->lock, flags);

	if (expired) {
		dev_dbg(&p->ch348->serial->port[p->portnum]->dev,
			"no TX-empty report, continuing\n");
		ch348_kick(p->ch348, p->portnum);
	}
}

static int ch348_write(struct tty_struct *tty, struct usb_serial_port *port,
		       const unsigned char *buf, int count)
{
	struct ch348 *ch348 = usb_get_serial_data(port->serial);
	struct ch348_port *p = &ch348->ports[port->port_number];

	if (!count)
		return 0;

	count = kfifo_in_locked(&p->fifo, buf, count, &p->lock);
	ch348_kick(ch348, port->port_number);

	return count;
}

static int ch348_write_room(struct tty_struct *tty)
{
	struct usb_serial_port *port = tty->driver_data;
	struct ch348 *ch348 = usb_get_serial_data(port->serial);
	struct ch348_port *p = &ch348->ports[port->port_number];
	unsigned long flags;
	int room;

	spin_lock_irqsave(&p->lock, flags);
	room = kfifo_avail(&p->fifo);
	spin_unlock_irqrestore(&p->lock, flags);

	return room;
}

static int ch348_chars_in_buffer(struct tty_struct *tty)
{
	struct usb_serial_port *port = tty->driver_data;
	struct ch348 *ch348 = usb_get_serial_data(port->serial);
	struct ch348_port *p = &ch348->ports[port->port_number];
	unsigned long flags;
	int chars;

	spin_lock_irqsave(&p->lock, flags);
	chars = kfifo_len(&p->fifo) + (p->tx_empty ? 0 : p->tx_bytes);
	spin_unlock_irqrestore(&p->lock, flags);

	return chars;
}

static bool ch348_tx_empty(struct usb_serial_port *port)
{
	struct ch348 *ch348 = usb_get_serial_data(port->serial);
	struct ch348_port *p = &ch348->ports[port->port_number];
	unsigned long flags;
	bool empty;

	spin_lock_irqsave(&p->lock, flags);
	empty = kfifo_is_empty(&p->fifo) && !p->urb_busy && p->tx_empty;
	spin_unlock_irqrestore(&p->lock, flags);

	return empty;
}

/* ------------------------------------------------------------------------ */
/* RX path (data IN endpoint, owned by port 0)                               */
/* ------------------------------------------------------------------------ */

static void ch348_process_read_urb(struct urb *urb)
{
	struct usb_serial_port *port0 = urb->context;
	struct usb_serial *serial = port0->serial;
	struct ch348 *ch348 = usb_get_serial_data(serial);
	const u8 *buf = urb->transfer_buffer;
	int len = urb->actual_length;
	unsigned long pushed = 0;
	unsigned int portnum, dlen, done;
	struct usb_serial_port *port;
	int i;

	for (i = 0; i + 2 <= len; i += CH348_RX_CHUNK) {
		portnum = buf[i];
		dlen = buf[i + 1];

		if (portnum >= CH348_NUM_PORTS) {
			dev_dbg(&port0->dev, "bad rx port %u\n", portnum);
			break;
		}
		if (dlen > CH348_RX_MAX_DATA || i + 2 + dlen > len) {
			dev_dbg(&port0->dev, "bad rx length %u\n", dlen);
			break;
		}
		if (!dlen || !READ_ONCE(ch348->ports[portnum].opened))
			continue;

		port = serial->port[portnum];
		done = tty_insert_flip_string(&port->port, buf + i + 2, dlen);
		port->icount.rx += done;
		if (done < dlen)
			port->icount.buf_overrun += dlen - done;
		__set_bit(portnum, &pushed);
	}

	for_each_set_bit(i, &pushed, CH348_NUM_PORTS)
		tty_flip_buffer_push(&serial->port[i]->port);
}

/* ------------------------------------------------------------------------ */
/* status pipe                                                               */
/* ------------------------------------------------------------------------ */

/*
 * @val: low nibble = which lines are valid/changed (CTS, DSR, RI, DCD),
 *       high nibble = their current level (16550 MSR layout).
 */
static void ch348_update_msr(struct ch348 *ch348, unsigned int portnum, u8 val)
{
	static const struct {
		u8 mask;
		unsigned int tiocm;
	} map[] = {
		{ 0x01, TIOCM_CTS },
		{ 0x02, TIOCM_DSR },
		{ 0x04, TIOCM_RI },
		{ 0x08, TIOCM_CD },
	};
	struct ch348_port *p = &ch348->ports[portnum];
	struct usb_serial_port *port = ch348->serial->port[portnum];
	unsigned int oldmsr, newmsr, diff;
	struct tty_struct *tty;
	unsigned long flags;
	int i;

	spin_lock_irqsave(&p->lock, flags);
	oldmsr = p->msr;
	newmsr = oldmsr;
	for (i = 0; i < ARRAY_SIZE(map); i++) {
		if (!(val & map[i].mask))
			continue;
		if (val & (map[i].mask << 4))
			newmsr |= map[i].tiocm;
		else
			newmsr &= ~map[i].tiocm;
	}
	p->msr = newmsr;
	spin_unlock_irqrestore(&p->lock, flags);

	diff = oldmsr ^ newmsr;
	if (!diff)
		return;

	spin_lock_irqsave(&port->lock, flags);
	if (diff & TIOCM_CTS)
		port->icount.cts++;
	if (diff & TIOCM_DSR)
		port->icount.dsr++;
	if (diff & TIOCM_RI)
		port->icount.rng++;
	if (diff & TIOCM_CD)
		port->icount.dcd++;
	spin_unlock_irqrestore(&port->lock, flags);

	if ((diff & TIOCM_CD) && READ_ONCE(p->opened)) {
		tty = tty_port_tty_get(&port->port);
		if (tty) {
			usb_serial_handle_dcd_change(port, tty,
						     newmsr & TIOCM_CD);
			tty_kref_put(tty);
		}
	}

	wake_up_interruptible(&port->port.delta_msr_wait);
}

static void ch348_update_lsr(struct ch348 *ch348, unsigned int portnum, u8 lsr)
{
	struct usb_serial_port *port = ch348->serial->port[portnum];
	unsigned long flags;

	spin_lock_irqsave(&port->lock, flags);
	if (lsr & CH348_LSR_OE)
		port->icount.overrun++;
	if (lsr & CH348_LSR_PE)
		port->icount.parity++;
	if (lsr & CH348_LSR_FE)
		port->icount.frame++;
	if (lsr & CH348_LSR_BI)
		port->icount.brk++;
	spin_unlock_irqrestore(&port->lock, flags);
}

static void ch348_process_status(struct ch348 *ch348, const u8 *d, int len)
{
	struct device *dev = &ch348->serial->interface->dev;
	unsigned int portnum;
	u8 iir;
	int i = 0;

	while (i + 1 < len) {
		portnum = d[i] & 0x0f;
		iir = d[i + 1];

		if (iir == R_INIT) {
			/* echo of the init command */
			i += 12;
		} else if (iir >= R_MOD && iir <= R_TM_O) {
			/* mode / gpio replies, unused here */
			i += 4;
		} else if (iir >= R_IO_CE && iir <= R_IO_CI) {
			i += (iir == R_IO_CI) ? 10 : 3;
		} else if ((iir & 0x0f) == R_II_B2) {
			if (portnum >= CH348_NUM_PORTS)
				break;
			ch348_tx_done(ch348, portnum);
			i += 3;
		} else if ((iir & 0x0f) == R_II_B3 ||
			   (iir == VEN_R && i + 4 <= len &&
			    d[i + 2] == (ch348_reg_base(portnum) | R_C6))) {
			if (portnum >= CH348_NUM_PORTS)
				break;
			if (iir == VEN_R) {
				/* reply to MSR read: all lines valid */
				ch348_update_msr(ch348, portnum,
						 d[i + 3] | 0x0f);
				i += 4;
			} else {
				if (i + 3 > len)
					break;
				ch348_update_msr(ch348, portnum, d[i + 2]);
				i += 3;
			}
		} else if ((iir & 0x0f) == R_II_B1) {
			if (portnum >= CH348_NUM_PORTS || i + 3 > len)
				break;
			ch348_update_lsr(ch348, portnum, d[i + 2]);
			i += 3;
		} else if (iir == R_EE_CFG || iir == R_UP_O) {
			i += 4;
		} else {
			dev_dbg(dev, "unknown status 0x%02x 0x%02x\n",
				d[i], iir);
			break;
		}
	}
}

static void ch348_status_callback(struct urb *urb)
{
	struct ch348 *ch348 = urb->context;
	struct device *dev = &ch348->serial->interface->dev;
	int status = urb->status;
	int ret;

	switch (status) {
	case 0:
		ch348_process_status(ch348, urb->transfer_buffer,
				     urb->actual_length);
		break;
	case -ENOENT:
	case -ECONNRESET:
	case -ESHUTDOWN:
		return;
	case -EPIPE:
		dev_err(dev, "status endpoint stalled\n");
		return;
	default:
		dev_dbg(dev, "status urb error: %d\n", status);
		break;
	}

	ret = usb_submit_urb(urb, GFP_ATOMIC);
	if (ret && ret != -EPERM && ret != -ENODEV)
		dev_err(dev, "failed to resubmit status urb: %d\n", ret);
}

/* ------------------------------------------------------------------------ */
/* line settings                                                             */
/* ------------------------------------------------------------------------ */

static u8 ch348_rx_timeout(unsigned int baud)
{
	if (!baud || baud >= 921600)
		return 5;
	return (1000000 * 15 / baud) / 100 + 1;
}

/* obfuscation used by the chip for the init command (vendor algorithm) */
static void ch348_encode(u8 *b, u8 rol, u8 xor)
{
	u8 first;
	int i, j;

	for (i = 0; i < rol; i++) {
		first = b[0];
		for (j = 0; j < 7; j++)
			b[j] = (b[j] << 1) | ((b[j + 1] & 0x80) ? 1 : 0);
		b[7] = (b[7] << 1) | ((first & 0x80) ? 1 : 0);
	}
	for (i = 0; i < 8; i++)
		b[i] ^= xor;
}

static void ch348_set_termios(struct tty_struct *tty,
			      struct usb_serial_port *port,
			      struct ktermios *old)
{
	struct ch348 *ch348 = usb_get_serial_data(port->serial);
	unsigned int portnum = port->port_number;
	struct ch348_port *p = &ch348->ports[portnum];
	unsigned int baud, set = 0, clear = 0;
	unsigned long flags;
	u8 buf[12];
	u8 rnd[2], rol, xor;
	u8 parity, databits, stopbits;
	int ret;

	if (old && !tty_termios_hw_change(&tty->termios, old))
		return;

	baud = tty_get_baud_rate(tty);
	if (C_BAUD(tty) == B0) {
		baud = p->baud ? p->baud : CH348_DEFAULT_BAUD;
		clear = TIOCM_DTR | TIOCM_RTS;
	} else if (old && (old->c_cflag & CBAUD) == B0) {
		set = TIOCM_DTR | TIOCM_RTS;
	}
	baud = clamp_t(unsigned int, baud, CH348_MIN_BAUD, CH348_MAX_BAUD);

	if (C_PARENB(tty)) {
		if (C_CMSPAR(tty))
			parity = C_PARODD(tty) ? 3 : 4;	/* mark : space */
		else
			parity = C_PARODD(tty) ? 1 : 2;	/* odd : even   */
	} else {
		parity = 0;
	}

	switch (C_CSIZE(tty)) {
	case CS5:
		databits = 5;
		break;
	case CS6:
		databits = 6;
		break;
	case CS7:
		databits = 7;
		break;
	default:
		databits = 8;
		break;
	}

	stopbits = C_CSTOPB(tty) ? 0x02 : 0x00;

	get_random_bytes(rnd, sizeof(rnd));
	rol = rnd[0] & 0x0f;
	xor = rnd[1];

	buf[0] = CMD_WB_E | (portnum & 0x0f);
	buf[1] = R_INIT;
	buf[2] = (portnum & 0x0f) | (rol << 4);
	buf[3] = (baud >> 24) & 0xff;
	buf[4] = (baud >> 16) & 0xff;
	buf[5] = (baud >> 8) & 0xff;
	buf[6] = baud & 0xff;
	buf[7] = stopbits;
	buf[8] = parity;
	buf[9] = databits;
	buf[10] = ch348_rx_timeout(baud);
	buf[11] = xor;
	ch348_encode(&buf[3], rol, xor);

	ret = ch348_cmd(ch348, buf, sizeof(buf));
	if (ret)
		goto restore;

	spin_lock_irqsave(&p->lock, flags);
	p->baud = baud;
	spin_unlock_irqrestore(&p->lock, flags);

	/* enable RX, THRE, LSR and MSR interrupts */
	ret = ch348_write_reg(ch348, portnum, CMD_W_R, R_C1, 0x0f);
	if (ret)
		goto restore;

	if (set || clear)
		ch348_update_mctrl(ch348, portnum, set, clear);

	if (C_CRTSCTS(tty))
		ch348_set_uartmode(ch348, portnum, M_HF);
	else
		ch348_set_uartmode(ch348, portnum, M_NOR);

	/* fetch the current modem status, the reply arrives on status pipe */
	ch348_request_msr(ch348, portnum);

	tty_encode_baud_rate(tty, baud, baud);
	return;

restore:
	if (old)
		tty->termios = *old;
}

static void ch348_break_ctl(struct tty_struct *tty, int break_state)
{
	struct usb_serial_port *port = tty->driver_data;
	struct ch348 *ch348 = usb_get_serial_data(port->serial);

	ch348_write_reg(ch348, port->port_number, CMD_W_BR, R_C3,
			break_state ? LCR_BREAK_ON : LCR_BREAK_OFF);
}

static int ch348_tiocmget(struct tty_struct *tty)
{
	struct usb_serial_port *port = tty->driver_data;
	struct ch348 *ch348 = usb_get_serial_data(port->serial);
	struct ch348_port *p = &ch348->ports[port->port_number];
	unsigned long flags;
	unsigned int msr;

	spin_lock_irqsave(&p->lock, flags);
	msr = p->msr;
	spin_unlock_irqrestore(&p->lock, flags);

	return READ_ONCE(p->mctrl) | msr;
}

static int ch348_tiocmset(struct tty_struct *tty, unsigned int set,
			  unsigned int clear)
{
	struct usb_serial_port *port = tty->driver_data;
	struct ch348 *ch348 = usb_get_serial_data(port->serial);

	return ch348_update_mctrl(ch348, port->port_number,
				  set & (TIOCM_DTR | TIOCM_RTS),
				  clear & (TIOCM_DTR | TIOCM_RTS));
}

static void ch348_dtr_rts(struct usb_serial_port *port, int on)
{
	struct ch348 *ch348 = usb_get_serial_data(port->serial);

	if (on)
		ch348_update_mctrl(ch348, port->port_number,
				   TIOCM_DTR | TIOCM_RTS, 0);
	else
		ch348_update_mctrl(ch348, port->port_number,
				   0, TIOCM_DTR | TIOCM_RTS);
}

static int ch348_carrier_raised(struct usb_serial_port *port)
{
	struct ch348 *ch348 = usb_get_serial_data(port->serial);

	return !!(READ_ONCE(ch348->ports[port->port_number].msr) & TIOCM_CD);
}

/* ------------------------------------------------------------------------ */
/* open / close                                                              */
/* ------------------------------------------------------------------------ */

static int ch348_open(struct tty_struct *tty, struct usb_serial_port *port)
{
	struct ch348 *ch348 = usb_get_serial_data(port->serial);
	unsigned int portnum = port->port_number;
	struct ch348_port *p = &ch348->ports[portnum];
	unsigned long flags;
	int ret;

	/* enable and reset FIFOs */
	ret = ch348_write_reg(ch348, portnum, CMD_W_R, R_C2, 0x87);
	if (ret)
		return ret;
	/* MCR: OUT2 (interrupt enable), DTR/RTS/AFE off */
	ret = ch348_write_reg(ch348, portnum, CMD_W_R, R_C4, 0x08);
	if (ret)
		return ret;

	mutex_lock(&p->mctrl_lock);
	p->mctrl = 0;
	mutex_unlock(&p->mctrl_lock);
	p->uartmode = M_NOR;

	spin_lock_irqsave(&p->lock, flags);
	kfifo_reset(&p->fifo);
	p->tx_empty = true;
	p->tx_bytes = 0;
	spin_unlock_irqrestore(&p->lock, flags);

	if (tty)
		ch348_set_termios(tty, port, NULL);

	spin_lock_irqsave(&p->lock, flags);
	p->opened = true;
	spin_unlock_irqrestore(&p->lock, flags);

	return 0;
}

static void ch348_close(struct usb_serial_port *port)
{
	struct ch348 *ch348 = usb_get_serial_data(port->serial);
	struct ch348_port *p = &ch348->ports[port->port_number];
	unsigned long flags;

	spin_lock_irqsave(&p->lock, flags);
	p->opened = false;
	kfifo_reset_out(&p->fifo);
	spin_unlock_irqrestore(&p->lock, flags);

	usb_kill_urb(p->write_urb);
	del_timer_sync(&p->tx_timer);

	spin_lock_irqsave(&p->lock, flags);
	p->urb_busy = false;
	p->tx_empty = true;
	p->tx_bytes = 0;
	spin_unlock_irqrestore(&p->lock, flags);

	if (!port->serial->disconnected)
		ch348_set_uartmode(ch348, port->port_number, M_NOR);
}

/* ------------------------------------------------------------------------ */
/* probe / attach / release                                                  */
/* ------------------------------------------------------------------------ */

struct ch348_eps {
	struct usb_endpoint_descriptor *in[2];
	struct usb_endpoint_descriptor *out[2];
};

/* data pair first, then cmd/status pair, in descriptor order */
static int ch348_find_eps(struct usb_serial *serial, struct ch348_eps *eps)
{
	struct usb_host_interface *alt = serial->interface->cur_altsetting;
	struct usb_endpoint_descriptor *ep;
	int i, nin = 0, nout = 0;

	memset(eps, 0, sizeof(*eps));
	for (i = 0; i < alt->desc.bNumEndpoints; i++) {
		ep = &alt->endpoint[i].desc;
		if (usb_endpoint_is_bulk_in(ep) && nin < 2)
			eps->in[nin++] = ep;
		else if (usb_endpoint_is_bulk_out(ep) && nout < 2)
			eps->out[nout++] = ep;
	}

	return (nin == 2 && nout == 2) ? 0 : -ENODEV;
}

static int ch348_calc_num_ports(struct usb_serial *serial,
				struct usb_serial_endpoints *epds)
{
	/*
	 * Only the data IN endpoint is handed to the core (to port 0); its
	 * read URBs carry RX data for all ports. Everything else is managed
	 * by this driver.
	 */
	epds->num_bulk_in = 1;
	epds->num_bulk_out = 0;

	return CH348_NUM_PORTS;
}

static int ch348_detect(struct ch348 *ch348)
{
	u8 *buf;
	int ret;

	buf = kzalloc(4, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;

	ret = usb_control_msg(ch348->udev, usb_rcvctrlpipe(ch348->udev, 0),
			      CMD_VER,
			      USB_TYPE_VENDOR | USB_RECIP_DEVICE | USB_DIR_IN,
			      0, 0, buf, 4, CH348_CMD_TIMEOUT);
	if (ret == 4) {
		ch348->chipver = buf[0];
		ch348->chip_q = !!(buf[1] & 0x80);
		ret = 0;
	} else if (ret >= 0) {
		ret = -EPROTO;
	}

	kfree(buf);
	return ret;
}

static void ch348_free_ports(struct ch348 *ch348)
{
	struct ch348_port *p;
	int i;

	for (i = 0; i < CH348_NUM_PORTS; i++) {
		p = &ch348->ports[i];
		if (p->write_urb) {
			usb_kill_urb(p->write_urb);
			usb_free_urb(p->write_urb);
		}
		del_timer_sync(&p->tx_timer);
		kfree(p->write_buf);
		kfifo_free(&p->fifo);
	}
}

static int ch348_attach(struct usb_serial *serial)
{
	struct ch348_eps eps;
	struct ch348 *ch348;
	struct ch348_port *p;
	u8 cmd[8];
	int i, ret;

	ret = ch348_find_eps(serial, &eps);
	if (ret) {
		dev_err(&serial->interface->dev, "unexpected endpoints\n");
		return ret;
	}

	ch348 = kzalloc(sizeof(*ch348), GFP_KERNEL);
	if (!ch348)
		return -ENOMEM;

	ch348->serial = serial;
	ch348->udev = serial->dev;
	ch348->data_out_pipe = usb_sndbulkpipe(serial->dev,
					       eps.out[0]->bEndpointAddress);
	ch348->cmd_pipe = usb_sndbulkpipe(serial->dev,
					  eps.out[1]->bEndpointAddress);
	ch348->write_size = usb_endpoint_maxp(eps.out[0]);
	ch348->status_size = usb_endpoint_maxp(eps.in[1]);

	if (ch348->write_size <= CH348_TX_HDR) {
		ret = -EINVAL;
		goto err_free;
	}

	for (i = 0; i < CH348_NUM_PORTS; i++) {
		p = &ch348->ports[i];
		p->ch348 = ch348;
		p->portnum = i;
		p->tx_empty = true;
		p->baud = CH348_DEFAULT_BAUD;
		p->uartmode = M_NOR;
		spin_lock_init(&p->lock);
		mutex_init(&p->mctrl_lock);
		timer_setup(&p->tx_timer, ch348_tx_timer, 0);
	}
	for (i = 0; i < CH348_NUM_PORTS; i++) {
		p = &ch348->ports[i];
		ret = kfifo_alloc(&p->fifo, CH348_FIFO_SIZE, GFP_KERNEL);
		if (ret)
			goto err_ports;
		p->write_buf = kmalloc(ch348->write_size, GFP_KERNEL);
		p->write_urb = usb_alloc_urb(0, GFP_KERNEL);
		if (!p->write_buf || !p->write_urb) {
			ret = -ENOMEM;
			goto err_ports;
		}
		usb_fill_bulk_urb(p->write_urb, serial->dev,
				  ch348->data_out_pipe, p->write_buf,
				  ch348->write_size, ch348_write_bulk_callback,
				  serial->port[i]);
	}

	ret = ch348_detect(ch348);
	if (ret)
		dev_warn(&serial->interface->dev,
			 "failed to read chip version (%d), assuming CH348L\n",
			 ret);
	else
		dev_info(&serial->interface->dev, "CH348%c, version 0x%02x\n",
			 ch348->chip_q ? 'Q' : 'L', ch348->chipver);

	/* newer firmware: enable independent per-port RX upload */
	if (ch348->chipver >= 0x8a) {
		memset(cmd, 0, sizeof(cmd));
		cmd[0] = CMD_WB_E;
		cmd[1] = R_UP_O;
		cmd[2] = 0x01;
		ret = ch348_cmd(ch348, cmd, sizeof(cmd));
		if (ret)
			goto err_ports;
	}

	ch348->status_buf = kmalloc(ch348->status_size, GFP_KERNEL);
	ch348->status_urb = usb_alloc_urb(0, GFP_KERNEL);
	if (!ch348->status_buf || !ch348->status_urb) {
		ret = -ENOMEM;
		goto err_status;
	}
	usb_fill_bulk_urb(ch348->status_urb, serial->dev,
			  usb_rcvbulkpipe(serial->dev,
					  eps.in[1]->bEndpointAddress),
			  ch348->status_buf, ch348->status_size,
			  ch348_status_callback, ch348);

	usb_set_serial_data(serial, ch348);

	ret = usb_submit_urb(ch348->status_urb, GFP_KERNEL);
	if (ret) {
		dev_err(&serial->interface->dev,
			"failed to submit status urb: %d\n", ret);
		goto err_clear;
	}

	ret = usb_serial_generic_submit_read_urbs(serial->port[0], GFP_KERNEL);
	if (ret) {
		dev_err(&serial->interface->dev,
			"failed to submit read urbs: %d\n", ret);
		usb_kill_urb(ch348->status_urb);
		goto err_clear;
	}

	return 0;

err_clear:
	usb_set_serial_data(serial, NULL);
err_status:
	usb_free_urb(ch348->status_urb);
	kfree(ch348->status_buf);
err_ports:
	ch348_free_ports(ch348);
err_free:
	kfree(ch348);
	return ret;
}

static void ch348_stop_io(struct ch348 *ch348, bool poison)
{
	int i;

	if (poison)
		usb_poison_urb(ch348->status_urb);
	else
		usb_kill_urb(ch348->status_urb);

	for (i = 0; i < CH348_NUM_PORTS; i++) {
		if (poison)
			usb_poison_urb(ch348->ports[i].write_urb);
		else
			usb_kill_urb(ch348->ports[i].write_urb);
		del_timer_sync(&ch348->ports[i].tx_timer);
	}
}

static void ch348_disconnect(struct usb_serial *serial)
{
	struct ch348 *ch348 = usb_get_serial_data(serial);

	ch348_stop_io(ch348, true);
}

static void ch348_release(struct usb_serial *serial)
{
	struct ch348 *ch348 = usb_get_serial_data(serial);
	struct usb_serial_port *port0 = serial->port[0];
	int i;

	/* the core does not kill read URBs of port 0 on a late probe error */
	for (i = 0; i < ARRAY_SIZE(port0->read_urbs); i++)
		usb_kill_urb(port0->read_urbs[i]);

	usb_kill_urb(ch348->status_urb);
	usb_free_urb(ch348->status_urb);
	kfree(ch348->status_buf);
	ch348_free_ports(ch348);
	kfree(ch348);
}

#ifdef CONFIG_PM
static int ch348_suspend(struct usb_serial *serial, pm_message_t message)
{
	struct ch348 *ch348 = usb_get_serial_data(serial);

	ch348_stop_io(ch348, false);
	return 0;
}

static int ch348_resume(struct usb_serial *serial)
{
	struct ch348 *ch348 = usb_get_serial_data(serial);
	int ret, i;

	ret = usb_submit_urb(ch348->status_urb, GFP_NOIO);
	if (ret)
		return ret;

	ret = usb_serial_generic_submit_read_urbs(serial->port[0], GFP_NOIO);
	if (ret)
		return ret;

	for (i = 0; i < CH348_NUM_PORTS; i++)
		ch348_kick(ch348, i);

	return 0;
}
#endif

static const struct usb_device_id ch348_ids[] = {
	{ USB_DEVICE(CH348_VID, CH348_PID) },
	{ }
};
MODULE_DEVICE_TABLE(usb, ch348_ids);

static struct usb_serial_driver ch348_device = {
	.driver = {
		.owner	= THIS_MODULE,
		.name	= "ch348",
	},
	.description		= "WCH CH348",
	.id_table		= ch348_ids,
	.num_bulk_in		= 2,
	.num_bulk_out		= 2,
	.calc_num_ports		= ch348_calc_num_ports,
	.attach			= ch348_attach,
	.disconnect		= ch348_disconnect,
	.release		= ch348_release,
	.open			= ch348_open,
	.close			= ch348_close,
	.write			= ch348_write,
	.write_room		= ch348_write_room,
	.chars_in_buffer	= ch348_chars_in_buffer,
	.tx_empty		= ch348_tx_empty,
	.set_termios		= ch348_set_termios,
	.break_ctl		= ch348_break_ctl,
	.tiocmget		= ch348_tiocmget,
	.tiocmset		= ch348_tiocmset,
	.tiocmiwait		= usb_serial_generic_tiocmiwait,
	.get_icount		= usb_serial_generic_get_icount,
	.dtr_rts		= ch348_dtr_rts,
	.carrier_raised		= ch348_carrier_raised,
	.process_read_urb	= ch348_process_read_urb,
#ifdef CONFIG_PM
	.suspend		= ch348_suspend,
	.resume			= ch348_resume,
#endif
};

static struct usb_serial_driver * const serial_drivers[] = {
	&ch348_device, NULL
};

module_usb_serial_driver(serial_drivers, ch348_ids);

MODULE_DESCRIPTION(DRIVER_DESC);
MODULE_LICENSE("GPL");
