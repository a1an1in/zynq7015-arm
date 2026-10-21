#
# Zynq GPIO key(press) -> PL LED(toggle) interrupt demo kernel module.
#
SUMMARY = "MIO key->PL LED interrupt demo driver"
DESCRIPTION = "Platform module using devm_gpiod_* + gpiod_to_irq + request_irq; a key falling edge toggles a PL LED through the aurora register window (MIO51 is the ENET0 PHY reset on the new board, so it must not be driven as an LED)."
SECTION = "PETALINUX/modules"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://Makefile \
           file://mio_irq.c \
          "
S = "${WORKDIR}"

inherit module

# Point `module` bbclass at the staged (configured) kernel it was built against.
EXTRA_OEMAKE += "KERNEL_SRC=${STAGING_KERNEL_DIR}"

RDEPENDS_${PN} += "kernel-module-mio-irq"
RPROVIDES_${PN} += "kernel-module-mio-irq"

# Auto-load at boot once the device-tree node (xlnx,mio-irq) is present.
KERNEL_MODULE_AUTOLOAD += "mio_irq"