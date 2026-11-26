# Out-of-tree build for the CH348 usb-serial driver (Linux 4.19)
#   make                      # build against the running kernel
#   make KDIR=/path/to/linux  # build against a specific kernel tree
#   sudo make install

obj-m := ch348.o

KDIR ?= /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean

install:
	$(MAKE) -C $(KDIR) M=$(PWD) modules_install
	depmod -a

load:
	modprobe usbserial
	insmod ./ch348.ko

unload:
	rmmod ch348
