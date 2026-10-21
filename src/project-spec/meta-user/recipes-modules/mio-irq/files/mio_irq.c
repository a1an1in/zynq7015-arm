// SPDX-License-Identifier: GPL-2.0
/*
 * mio_irq.c — Zynq GPIO 按键中断（可选：同时翻转 PL LED）的内核模块示例
 *
 * 演示链路(对应《Zynq GPIO中断子系统完全解析》):
 *   设备树节点
 *     ├ devm_gpiod_get(dev,"key",GPIOD_IN)        → desc   # 输入:按键(PS_MIO50)
 *     ├ gpiod_to_irq(key)                         → virq   # chip->irq.domain 现成域
 *     └ request_irq(virq, handler, IRQF_TRIGGER_*)
 *           └ 按键边沿来中断 → 回调 mio_isr → 计数 + dmesg
 *                                  └ (可选) 写 PL 寄存器窗的 LED_VALUE/LED_CTRL
 *
 * 为什么没有 PS 侧 LED:
 *   新板(MLK-F6P-CZ05 / XC7Z015)上 PS 侧只有 MIO51 这一颗 LED 候选，而它同时是
 *   千兆网口(PHY)复位(Vivado PS7: PCW_ENET0_RESET_IO = MIO 51)。手册“12:千兆网口”
 *   标成网口复位、“24:LED”表和厂商例程 DTS 又标成 PS LED(资料冲突，见 fpga 仓库
 *   docs/board-io-peripherals.md §4)。无论如何接线，翻转它都会动 PHY 复位 →
 *   “系统起来但没有网口”(旧板 F6-7020 的 S99psled-off 就是这么把网口弄没的)。
 *   → 所以 PS 侧不点灯：MIO51 归 PS7 独占，Linux 一律不碰。
 *
 * 两种工作模式(由设备树决定):
 *   A) 只测中断(默认)：DT 里没有 led-mask(或为 0) → 不映射 PL 窗口、不驱动任何
 *      引脚，ISR 只计数 + dmesg 打印。**烧什么位流都行**(run_led/aurora 都可)，
 *      验证依据是 /proc/interrupts 的计数与 dmesg 的 "key pressed N"。
 *   B) 中断 + PL LED：DT 给了 led-mask(非 0) 和 reg(PL 寄存器窗) → ISR 额外写
 *      aurora 的 LED_VALUE/LED_CTRL，翻转 PL 的 led_o。**需要 aurora 位流**
 *      (run_led 没有 regbank)。
 *
 * 配套设备树节点(见 recipes-bsp/device-tree/files/system-user.dtsi):
 *   compatible = "xlnx,mio-irq";
 *   key-gpios = <&gpio0 50 0>;   // PS_MIO50 -> 底板 PS 按键(上拉+RC, 按下=低, 可中断)
 *   reg = <0x40000000 0x10000>;   // PL aurora 寄存器窗(PS 视角, M_AXI_GP0 基址)  [模式 B]
 *   led-value-reg = <0x10>;       // LED_VALUE 偏移: [3:0] 点亮哪几颗                  [模式 B]
 *   led-ctrl-reg = <0x14>;        // LED_CTRL  偏移: bit0 = 使能                        [模式 B]
 *   led-mask = <0x8>;             // 用 led_o[3] -> H8 -> 板上丝印 LED1 (0/缺省 = 不点灯)  [模式 B]
 *
 * LED 极性：板载 LED 低电平点亮(0=亮)；aurora_led.v 已取反，所以**寄存器写 1 = 点亮**，
 * LED_CTRL=0 时 4 颗全灭(上电默认不亮)。
 */

#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/gpio/consumer.h>
#include <linux/interrupt.h>
#include <linux/of.h>
#include <linux/io.h>

#define LED_VALUE_REG_DEFAULT 0x10
#define LED_CTRL_REG_DEFAULT  0x14
#define LED_MASK_DEFAULT      0x1

struct mio_dev {
	struct device    *dev;      /* 仅供 dev_info 打印             */
	struct gpio_desc *key;      /* 输入:按键(MIO50)                */
	void __iomem      *regs;     /* PL aurora 寄存器窗(模式 B 才映射) */
	u32 value_reg;              /* LED_VALUE 偏移                  */
	u32 ctrl_reg;               /* LED_CTRL  偏移                  */
	u32 mask;                   /* 用哪几颗 PL LED(0 = 不点灯)      */
	u32 state;                  /* 当前点亮状态(影子寄存器)        */
	int virq;                   /* gpiod_to_irq(key) 返回的 virq   */
	unsigned int count;         /* 按键次数(演示用)                */
};

static irqreturn_t mio_isr(int irq, void *dev_id)
{
	struct mio_dev *d = dev_id;

	/* 模式 B: 翻转影子状态 → 写 PL: LED_VALUE 选灯, LED_CTRL bit0=使能 */
	if (d->regs) {
		d->state ^= d->mask;
		writel(d->state, d->regs + d->value_reg);
		writel(1, d->regs + d->ctrl_reg);
	}
	d->count++;

	dev_info(d->dev, "key pressed %u time(s) -> %s\n",
		 d->count,
		 d->regs ? "pl led toggled" : "interrupt only(no led)");
	return IRQ_HANDLED;
}

static int mio_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct mio_dev *d;
	struct resource *res;
	int virq, ret;

	d = devm_kzalloc(dev, sizeof(*d), GFP_KERNEL);
	if (!d)
		return -ENOMEM;
	d->dev = dev;

	/* 1. 方向: KEY=输入。底层落到芯片 set_direction 写 DIRM 等寄存器。 */
	d->key = devm_gpiod_get(dev, "key", GPIOD_IN);
	if (IS_ERR(d->key))
		return PTR_ERR(d->key);

	/* 2. led-mask: 0 / 缺省 => 模式 A(只测中断，不点灯、不碰 PL) */
	of_property_read_u32(dev->of_node, "led-mask", &d->mask);
	of_property_read_u32(dev->of_node, "led-value-reg", &d->value_reg);
	of_property_read_u32(dev->of_node, "led-ctrl-reg", &d->ctrl_reg);
	if (!d->value_reg)
		d->value_reg = LED_VALUE_REG_DEFAULT;
	if (!d->ctrl_reg)
		d->ctrl_reg = LED_CTRL_REG_DEFAULT;

	if (d->mask) {
		/* 模式 B: 映射 PL aurora 寄存器窗(与 system-user.dtsi 里
		 * aurora@40000000 的 generic-uio 是同一块物理区：UIO 给用户态，
		 * 本模块在内核态单独 ioremap)。缺 reg 就退化成模式 A，不影响中断。 */
		res = platform_get_resource(pdev, IORESOURCE_MEM, 0);
		if (res) {
			d->regs = devm_ioremap(dev, res->start, resource_size(res));
			if (d->regs) {
				/* 初始: 全灭 */
				writel(0, d->regs + d->value_reg);
				writel(0, d->regs + d->ctrl_reg);
			} else {
				dev_err(dev, "ioremap 0x%llx failed, continue as interrupt-only\n",
					(unsigned long long)res->start);
			}
		} else {
			dev_warn(dev, "no 'reg' resource, continue as interrupt-only\n");
		}
	}

	/* 3. 按键脚变成中断源: gpiod_to_irq 经由 chip->irq.domain 掏出 virq,
	 *    与 gpio_to_irq 同源, 不匹配 fwnode/不扫 irq_domain_list。 */
	virq = gpiod_to_irq(d->key);
	if (virq < 0)
		return virq;
	d->virq = virq;

	/* 4. 注册中断: 内核把 IRQF_TRIGGER_* 交给 chip->irq_set_type 写
	 *    INT_TYPE/INT_POL 寄存器, 并 irq_startup 使能; 边沿触发无需手动 ack。 */
	ret = devm_request_irq(dev, virq, mio_isr,
			       IRQF_TRIGGER_FALLING,	/* 按键按下 = 下降沿 */
			       "mio_irq", d);
	if (ret) {
		dev_err(dev, "request_irq(%d) failed: %d\n", virq, ret);
		return ret;
	}

	dev_info(dev, "mio_irq ready: virq=%d, key(MIO50) interrupt armed, mode=%s%s\n",
		     virq,
		     d->regs ? "led" : "interrupt-only",
		     d->regs ? "" : " (no pl led)");
	return 0;
}

static const struct of_device_id mio_of_match[] = {
	{ .compatible = "xlnx,mio-irq" },
	{ /* sentinel */ }
};
MODULE_DEVICE_TABLE(of, mio_of_match);

static struct platform_driver mio_driver = {
	.probe  = mio_probe,
	.driver = {
		.name          = "mio_irq",
		.of_match_table = mio_of_match,
	},
};
module_platform_driver(mio_driver);

MODULE_AUTHOR("Zynq");
MODULE_DESCRIPTION("Zynq GPIO key(PRESS) interrupt demo module (optional PL LED toggle)");
MODULE_LICENSE("GPL");
