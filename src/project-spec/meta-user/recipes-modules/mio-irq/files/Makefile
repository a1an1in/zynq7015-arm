# kbuild Makefile for the mio_irq module.
# Standard PetaLinux kernel-module pattern: KERNEL_SRC is supplied by the
# bitbake `module` bbclass (STAGING_KERNEL_DIR); for ad-hoc builds run:
#   make KERNEL_SRC=/path/to/kernel-tree CROSS_COMPILE=arm-linux-gnueabihf- ARCH=arm
obj-m := mio_irq.o

KERNEL_SRC ?= /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

all:
	$(MAKE) -C $(KERNEL_SRC) M=$(PWD) modules

modules_install:
	$(MAKE) -C $(KERNEL_SRC) M=$(PWD) INSTALL_MOD_PATH=$(DESTDIR) modules_install

clean:
	$(MAKE) -C $(KERNEL_SRC) M=$(PWD) clean

.PHONY: all modules_install clean