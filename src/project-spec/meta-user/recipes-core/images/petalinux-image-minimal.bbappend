# ===== 已停用：PS LED 开机熄灭脚本（旧板 F6-7020 / PS_LED8 = PS_MIO51）=====
# 旧板：PS_MIO51 = LED8，且 PS7 里 ENET0 reset 也挂在 MIO51（板上并没有 PHY 复位线），
#       FSBL 上电把该脚拉高 -> LED 常亮，所以这里加 S99psled-off 在 runlevel5 把它拉低。
# 新板：米联客 MLK-F6P-CZ05（XC7Z015-2CLG485I）上 MIO51 = 千兆网口(PHY)复位
#       （硬件手册 12:千兆网口，PS_MIO51 C13“千兆网口复位”；参考 XSA 里
#        PCW_ENET0_RESET_IO = MIO 51）。
#       如果继续在用户态 export gpio957 并 output 0，就会把 PHY 一直按在复位里，
#       表现为“系统起来但没有网口”。
# 因此这里不再生成该脚本：MIO51 由 PS7 静态配置驱动为 ENET0 reset（复位释放=高）。
# ⚠️ 本段脚本不要打开：它把 MIO51 配成 out 拉低 = 一直按住 PHY 复位 → 没有网口。
#    新板的 PS 侧 LED 输出已改走 PL（mio-irq demo 改成 PS 按键中断 → 写 PL 的
#    aurora 寄存器窗 → 翻 PL 的 LED），MIO51 归 PS7 独占，Linux 侧一律不碰。
#
# ROOTFS_POSTPROCESS_COMMAND += "ps_led_off_cfg ;"
#
# ps_led_off_cfg() {
# 	if [ -d "${IMAGE_ROOTFS}/etc/rc5.d" ]; then
# 		{
# 			echo '#!/bin/sh'
# 			echo 'echo 957 > /sys/class/gpio/export 2>/dev/null'
# 			echo 'echo out > /sys/class/gpio/gpio957/direction 2>/dev/null'
# 			echo 'echo 0   > /sys/class/gpio/gpio957/value   2>/dev/null'
# 			echo 'exit 0'
# 		} > "${IMAGE_ROOTFS}/etc/rc5.d/S99psled-off"
# 		chmod 0755 "${IMAGE_ROOTFS}/etc/rc5.d/S99psled-off"
# 	fi
# }
