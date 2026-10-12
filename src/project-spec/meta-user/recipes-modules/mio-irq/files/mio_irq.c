// SPDX-License-Identifier: GPL-2.0
/*
 * mio_irq.c — Zynq GPIO 按键中断 → 翻转 LED 的内核模块示例
 *
 * 演示链路(对应《Zynq GPIO中断子系统完全解析》):
 *   设备树节点
 *     ├ devm_gpiod_get(dev,"led",GPIOD_OUT_LOW) → desc   # 输出:LED
 *     ├ devm_gpiod_get(dev,"key",GPIOD_IN)      → desc   # 输入:按键
 *     ├ gpiod_to_irq(key)                       → virq   # chip->irq.domain 现成域
 *     └ request_irq(virq, handler, IRQF_TRIGGER_*)
 *           └ 按键边沿来中断 → 回调 mio_isr → gpiod_set_value(led,!val) 翻转
 *
 * 配套设备树节点(见 recipes-bsp/device-tree/files/system-user.dtsi):
 *   compatible = "xlnx,mio-irq";
 *   led-gpios = <&gpio0 51 0>;   // "led" → devm_gpiod_get(dev,"led",...)   PS_MIO51(底板1路PS端LED, Q13)
 *   key-gpios = <&gpio0 50 0>;   // "key" → devm_gpiod_get(dev,"key",...)   PS_MIO50(底板KEY2, MIO按键,可中断)
 */

#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/gpio/consumer.h>
#include <linux/interrupt.h>
#include <linux/of.h>

struct mio_dev {
	struct device    *dev;      /* 仅供 dev_info 打印             */
	struct gpio_desc *led;      /* 输出:LED                       */
	struct gpio_desc *key;      /* 输入:按键                       */
	int virq;                   /* gpiod_to_irq(key) 返回的 virq   */
	unsigned int count;         /* 按键次数(演示用)                */
};

static irqreturn_t mio_isr(int irq, void *dev_id)
{
	struct mio_dev *d = dev_id;
	int new;

	new = !gpiod_get_value(d->led);   /* 读当前电平取反 */
	gpiod_set_value(d->led, new);      /* 每按一次翻转    */
	d->count++;

	dev_info(d->dev, "key pressed %u time(s) -> led=%d\n",
		 d->count, new);
	return IRQ_HANDLED;
}

static int mio_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct mio_dev *d;
	int virq, ret;

	d = devm_kzalloc(dev, sizeof(*d), GFP_KERNEL);
	if (!d)
		return -ENOMEM;
	d->dev = dev;

	/* 1. 方向: LED=输出(初始低) / KEY=输入。底层落到芯片 set_direction 写 DIRM 等寄存器。 */
	d->led = devm_gpiod_get(dev, "led", GPIOD_OUT_LOW);
	if (IS_ERR(d->led))
		return PTR_ERR(d->led);

	d->key = devm_gpiod_get(dev, "key", GPIOD_IN);
	if (IS_ERR(d->key))
		return PTR_ERR(d->key);

	/* 2. 按键脚变成中断源: gpiod_to_irq 经由 chip->irq.domain 掏出 virq,
	 *    与 gpio_to_irq 同源, 不匹配 fwnode/不扫 irq_domain_list。 */
	virq = gpiod_to_irq(d->key);
	if (virq < 0)
		return virq;
	d->virq = virq;

	/* 3. 注册中断: 内核把 IRQF_TRIGGER_* 交给 chip->irq_set_type 写
	 *    INT_TYPE/INT_POL 寄存器, 并 irq_startup 使能; 边沿触发无需手动 ack。 */
	ret = devm_request_irq(dev, virq, mio_isr,
			       IRQF_TRIGGER_FALLING,	/* 按键按下 = 下降沿 */
			       "mio_irq", d);
	if (ret) {
		dev_err(dev, "request_irq(%d) failed: %d\n", virq, ret);
		return ret;
	}

	dev_info(dev, "mio_irq ready: virq=%d, key interrupt armed\n", virq);
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
MODULE_DESCRIPTION("Zynq GPIO key(PRESS) -> LED(TOGGLE) interrupt demo module");
MODULE_LICENSE("GPL");