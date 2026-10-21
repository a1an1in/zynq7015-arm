# Zynq GPIO / 中断子系统 完全解析：从设备树到 virq

> 适用：Linux 5.10（PetaLinux 2021.1）@ Zynq-7015（Cortex-A9 + GIC-400）。
> 内核源码：`src/build/tmp/work-shared/zynq-generic/kernel-source`。
> 本文把"GPIO 中断从设备树一路到 `request_irq` 拿到的 virq、以及运行时怎么操作寄存器"整条链钉死，
> 重点对比 **`gpio_to_irq`（直达 domain）** 与 **`platform_get_irq`（phandle→fwnode→domain）** 两条入口。

---

## 一、总览：一个物理中断 vs 一张映射表 vs 118 根线

```
CPU (Cortex-A9)
  └─ GIC (系统唯一一级中断入口)
        └─ SPI 20（GIC 域 hwirq=52）← GPIO 控制器在 GIC 上只有这一根线（Num_parents=1）
              └─ GPIO 控制器（次级中断控制器，irq_domain，size=118）
                     ├─ 引脚 39 → 域内映射项 { hwirq=39, virq=惰性分配 }
                     ├─ 引脚 51 → 域内映射项 { hwirq=51, virq=惰性分配 }
                     └─ …最多 118 根（54 MIO + 64 EMIO，每根一条映射）
```

- **物理/父中断：1 个**（`platform_get_irq(pdev,0)`=GIC SPI 20），所有引脚共享。
- **引脚映射项：最多 118 个**，每根引脚一个 `hwirq=引脚号`；对应的 **virq（Linux 逻辑中断号）** 首次用到时才惰性分配。

> 术语澄清：**hwirq** = 该域**域内**的编号（GPIO 域就是引脚号 0~117），**不是全局编号**；**virq = Linux 全局"逻辑中断号"**（`/proc/interrupts` 第一列）。习惯上"逻辑中断号"指 **virq**；`hwirq=39` 只是 GPIO 域内的相对号，数值与 virq 通常无关——**别混叫**。

---

## 二、对象地图

| DT 节点 | phandle | 内核侧 device_node | fwnode | irq_domain | 私有数据 |
|---|---|---|---|---|---|
| `gpio@e000a000` | 0x08 | zynq 的 `device_node` | 内嵌 `fwnode`（匹配钥匙） | Xilinx GPIO 域（`irq_domain_add_simple` 建） | `host_data = gpio_chip` → `gpiodev->data` → `zynq_gpio`(含 `base_addr`) |

每根引脚中断有一个 `irq_desc`，其 `irq_data` 携三件套：**`chip`(函数表) + `chip_data`(gpio_chip/基址) + `hwirq`(引脚号)**。

---

## 三、三套编号体系（不要混）

| 编号 | 属于哪个域 | 含义 / 取值 | 例 |
|---|---|---|---|
| `hwirq` | **GPIO 域**（域内编号） | GPIO 管脚号，**0~117**（MIO 0-53 + EMIO 54-117） | `39` |
| `virq` | Linux 全局 | irq_desc 池下标 / /proc/interrupts 第一列，运行时分配，不固定 | 如 `44` |
| GIC 域 hwirq | intc 域 | SPI 物理号 = DT编号+32（PPI+16） | `<0 20 4>` → 52 |

> `hwirq`（GPIO 域）与 GIC 域的 hwirq **数值无任何映射关系**——GPIO 域是引脚号(39)，GIC 域是 SPI 物理号(52)；靠 `irq_data.parent_data` 链串起来。

---

## 四、注册都在哪发生（几乎不在 Zynq 驱动里）

| 注册 | 位置 / 时机 |
|---|---|
| GPIO irq_domain | probe：`gpiochip_add_data` → `gpiochip_add_irqchip`（gpiolib.c:731）→ `gpiochip_irqchip_add_key` → `irq_domain_add_simple` |
| 父中断 handler 挂 GIC | 同 `gpiochip_irqchip_add_key` 末尾：`irq_set_chained_handler_and_data(SPI20, zynq_gpio_irqhandler, gc)` |
| 某引脚 virq | 首次 `irq_create_mapping(domain, hwirq)`（惰性） |
| GIC 域本身 | `irq-gic.c` 系统级 `IRQCHIP_DECLARE` |
| Zynq 驱动真正做的 | 只填 `chip->irq.*`（chip/parent_handler/num_parents/parents/handler）+ 拿 `gpio->irq=platform_get_irq(pdev,0)`，然后 `gpiochip_add_data()` |

Zynq probe（`gpio-zynq.c`）里的 ready 配置：
```c
girq->chip           = &zynq_gpio_edge_irqchip;
girq->parent_handler = zynq_gpio_irqhandler;
girq->num_parents    = 1;
girq->parents[0]     = gpio->irq;      /* GIC SPI 20 */
girq->default_type   = IRQ_TYPE_NONE;
girq->handler        = handle_level_irq;
```

---

## 五、desc 的三阶段配置（首次 create mapping 时）

| 阶段 | 函数 | 干的事 |
|---|---|---|
| ① 分配 desc | `__irq_alloc_descs` → `alloc_descs`（irqdesc.c:772） | 分配并初始化 `irq_desc[virq]`，`irq = virq` |
| ② 域认领 | `irq_domain_associate`（irqdomain.c:519）`__irq_domain_map` | 写 `irq_data.hwirq`、含域 pointer、`linear_revmap[hwirq]=virq` |
| ③ 挂载回调 | `gpiochip_irq_map`（gpiolib.c:1297） | `chip=zynq irqchip`、`chip_data=gpio_chip`、`irq_set_parent→GIC` |

`gpiochip_irq_map` 关键：
```c
struct gpio_chip *gc = d->host_data;
irq_set_chip_data(irq, gc);                       /* chip_data=gc            */
irq_set_chip_and_handler(irq, gc->irq.chip, gc->irq.handler);
if (gc->irq.num_parents == 1)
	irq_set_parent(irq, gc->irq.parents[0]);   /* 挂 GIC SPI20            */
---

## 六、fwnode 与 phandle（谁是"身份"，谁在"匹配"）

### phandle（编译期发号，of 层找"节点"）
- 每个可被引用的设备树节点，DTC 编译时发一个**全树唯一 32 位整数**（如 gpio0=0x08）。
- 用法：`<&gpio0 39 9>` 中，第一格 `0x08` 是 phandle，后两格是按 gpio0 的 `#interrupt-cells=<2>` 解析的业务参数（引脚号/触发类型）。
- 运行时：`of_find_node_by_phandle(handle)`（of/base.c:1196，先查缓存、miss 则 `for_each_of_allnodes` 扫 `np->phandle==handle`）**phandle 值 → `device_node*`**。

### fwnode（通用身份卡，irqdomain 层匹配 domain）
- **fwnode 不是"解析后的结构体"**。解析后的真身是 `struct device_node`（带 `properties` 属性树）；fwnode 是**内嵌在 `device_node` 里的一个通用身份壳**：
```c
struct device_node { ... struct fwnode_handle fwnode; ... };  /* of.h:55, 内嵌=值 */
```
- `struct device` / `struct irq_domain` 各持一个 `struct fwnode_handle *fwnode;` **指针**（引用节点内嵌那份）。
- irq 匹配用实例：`irq_find_matching_fwspec`（irqdomain.c:370）在 `irq_domain_list` 上按 `domain->fwnode == fwspec->fwnode` 扫出 domain——**纯句柄指针比对，完全不关心"是不是 gpio"**。

> 一句话：**phandle 管"找节点"，fwnode 管"匹配 domain"。** 任何入口最终都用节点的 fwnode 去 `irq_domain_list` 里对号。

---

## 七、★ 两条入口调用链（重点）

- **`gpio_to_irq`**：已有一个全局 GPIO 号，**直接从 chip 掏 domain**（不碰 phandle/fwnode）。
- **`platform_get_irq`**：只有一个设备节点，**必须 phandle→节点→fwnode→匹配 domain**。

两者最后都汇合到 **`irq_create_mapping_affinity(domain, hwirq, NULL)`**，后半段完全一致。

### 7.1 `gpio_to_irq`（直达 domain）

```
gpio_to_irq(50)                                    include/linux/gpio.h:79（inline）
└─ __gpio_to_irq(50)                               include/asm-generic/gpio.h:110
   └─ gpiod_to_irq(gpio_to_desc(50))
       ├─ gpio_to_desc(50)                          gpiolib.c:104    选 zynq chip
       │     遍历 gpio_devices，按 gdev->base..base+ngpio 命中 zynq → &descs[50-base]
       └─ gpiod_to_irq(desc)                        gpiolib.c:3180
           ├─ gc = desc->gdev->chip
           ├─ offset = gpio_chip_hwgpio(desc) = 50
           └─ gc->to_irq(gc, 50)                    自动设成 gpiochip_to_irq
              └─ gpiochip_to_irq(gc,50)              gpiolib.c:1397
                 ├─ domain = gc->irq.domain   ★直接掏（probe 建好塞在 chip 里）
                 └─ irq_create_mapping(domain, 50)
                    └─ irq_create_mapping_affinity(domain, 50, NULL)   irqdomain.c:637
```
先 `gpio_to_desc` 按全局号段把编号换成 `gpio_desc`，经 `desc->gdev->chip` 拿到 `gpio_chip`，再从 chip 里现成的 `chip->irq.domain` 直接掏出 `domain`（不扫描不匹配），拿 `(domain, offset)` 去 `irq_create_mapping` 建/复用 irq_desc 返回 virq；hwirq 用的就是这个 `offset`（编号−base=50）。
。
> 特点：**不需要 phandle、fwnode**。`gpio_to_desc` 按全局号段定位到 zynq 的 `desc`，`desc->gdev->chip->irq.domain` 就是现成 domain。

### 7.2 `platform_get_irq`（phandle→fwnode→domain）

```
platform_get_irq(dev, 0)                           drivers/base/platform.c:260
└─ platform_get_irq_optional(dev, 0)
   └─ (DT 分支) of_irq_get(dev->of_node, 0)          drivers/of/irq.c:387
       ├─ ① of_irq_parse_one(np, 0, &oirq)          drivers/of/irq.c:286
       │       ├─ of_irq_find_parent(np)            drivers/of/irq.c:54   ← PHANDLE 开始
       │       │      └─ of_find_node_by_phandle    of/base.c:1196        phandle→controller节点
       │       └─ 按 #interrupt-cells 拷 args → 填进 oirq（类型 = struct of_phandle_args）
       └─ ③ irq_create_of_mapping(&oirq)             irqdomain.c:841
             └─ of_phandle_args_to_fwspec(np,args…)   irqdomain.c:737  ★节点→fwnode  fwspec.fwnode=&np->fwnode
                └─ irq_create_fwspec_mapping(&fwspec) irqdomain.c:750
                     ├─ domain = irq_find_matching_fwspec       irqdomain.c:370 
                     │        (h->fwnode == fwspec->fwnode) 且 bus_token 匹配
                     ├─ irq_domain_translate → hwirq/type  irqdomain.c:719  → xlate 得 hwirq
                     ├─ virq = irq_find_mapping(domain,hwirq) 已映射→复用
                     └─(无) irq_create_mapping(domain,hwirq)  irqdomain.c:827
                             └─ irq_create_mapping_affinity(domain, hwirq, NULL)   irqdomain.c:637
```
platform_get_irq 先把 DT 中断引用解析成 `fwspec`（带着 fwnode 和原始参数），用其中的 fwnode 遍历 `irq_domain_list` 匹配到 `domain`；`domain` 再把 param 翻译出 `hwirq`，拿 `(domain, hwirq)` 建映射

**`oirq` 里装了什么**（`struct of_phandle_args`，`include/linux/of.h:74`，`of_irq_parse_one` 的解析产物）：

| 字段 | 值 | 含义 |
|---|---|---|
| `oirq.np` | 控制器节点（gpio0 / intc） | phandle 反查到的**中断控制器节点** |
| `oirq.args_count` | 2 / 3 | 该控制器 `#interrupt-cells` 数 |
| `oirq.args[]` | `{39,9}` / `{0,20,4}` | 除去 phandle 的**原始 cell 值**，交给 translate 变 hwirq |

> 一句话：`oirq` = “**谁是我的中断控制器（np） + 用它的哪个参数（args[]）**”，是 OF 层的解析终点、irqdomain 层的起点。

**匹配 domain list 比 fwnode 其实发生两次**（都落在 `irq_find_matching_fwspec`，irqdomain.c:370），但两次产出的 `domain` **是两个不同函数栈帧里的局部变量**：

**第 1 次（旁路探针，可忽略）**　`of_irq_get` 里 `irq_find_host(oirq.np)`（irqdomain.h:315），把 `oirq.np` 用 `of_node_to_fwnode` 转成 fwnode 后**也遍历一遍 `irq_domain_list`**。目的只是确认 domain 已建：没建就返回 `-EPROBE_DEFER`，等 gpio 域注册完再重 probe。它找到的 `domain` **只用了一次判空即丢弃，不参与 virq 的生成**。

**第 2 次（正式，virq 的真出处）**　`irq_create_of_mapping → irq_create_fwspec_mapping`（irqdomain.c:750）里**又声明一个新的局部 `domain`**。这个 `domain` 的关键是"怎么被找到的"——前一步 `of_phandle_args_to_fwspec`（irqdomain.c:737）已把 `oirq.np` 变成 `fwspec.fwnode = &oirq.np->fwnode`，于是 `irq_create_fwspec_mapping` 用带着 `fwnode` 的 `fwspec` 去匹配：

```c
domain = irq_find_matching_fwspec(fwspec, DOMAIN_BUS_WIRED);   /* irqdomain.c:759 */
if (!domain)
	domain = irq_find_matching_fwspec(fwspec, DOMAIN_BUS_ANY);  /* irqdomain.c:761 */
```

`irq_find_matching_fwspec`（irqdomain.c:370）"找到 domain"的机制——**它的返回值就是被 `domain =` 接住的那个 Xilinx 域**：加锁遍历全局链表 `irq_domain_list`（`link` 把**所有已注册的 domain**串起来），对其中**每一个** `h` 判 `h->fwnode == fwspec->fwnode`（gpiochip_domain_ops 无 select/match，走此裸比较，irqdomain.c:379）；一旦命中 Xilinx 域的 fwnode，就 `return h`。**"找到的 domain" = 它最终 `return` 出来的那个 `struct irq_domain *`**。拿到它，`irq_create_fwspec_mapping` 才 `irq_domain_translate → irq_find_mapping/irq_create_mapping` 产出 virq。**算 virq 永远用这第②个 `domain`。**

> 读代码追 virq 时可直接无视 ①；讲加载顺序（defer）时才必须提它。

> 特点：**两段找：phandle 找节点，fwnode 匹配 domain**。对外只看到 `of_irq_get` 一行，内部其实“**先预检一次（只判 defer，产出即弃）、再正式匹配一次（真正产出 virq）**”，两趟都扫全局 `irq_domain_list`。

### 7.3 汇合点之后（两条路相同）

`irq_create_mapping_affinity`（irqdomain.c:637）：
```
① virq = irq_find_mapping(domain, hwirq)  读 linear_revmap[hwirq]；非0→复用返回
② virq = irq_domain_alloc_descs(-1,1,hwirq,…)   irqdomain.c:1016 → __irq_alloc_descs irqdesc.c:772（这才建 irq_desc）
③ irq_domain_associate(domain, virq, hwirq)     irqdomain.c:519
      ├─ irq_data.hwirq = hwirq
      ├─ domain->ops->map = gpiochip_irq_map     gpiolib.c:1297  → 写 chip/chip_data/挂 GIC
      └─ linear_revmap[hwirq] = virq
④ return virq
```
> **首次=建 desc + 分配 virq；再次=查表复用，不建。**
---

## 八、`request_irq(virq)` 运行视角（不再找"控制器"）

拿到 virq 后，`request_irq` 不是"去找中断控制器"，而是**用 virq 按下标取 desc，再拿 irq_data 里现成的三件套**：

```c
request_irq(virq, key1_irq_handler, IRQF_TRIGGER_RISING, ...)
└─ request_threaded_irq(...)              kernel/irq/manage.c
     desc = irq_to_desc(virq)             irqdesc.c（非sparse: irq_desc+virq；sparse: radix_tree_lookup）
     __setup_irq(irq, desc, action)       manage.c
          └─ new->flags & IRQF_TRIGGER_MASK
              → __irq_set_trigger(desc, IRQ_TYPE_EDGE_RISING)  manage.c:1585/839
                  chip = irq_desc_get_chip(desc)                /* desc->irq_data.chip=zynq irqchip */
                  chip->irq_set_type(&desc->irq_data, IRQ_TYPE_EDGE_RISING)  = zynq_gpio_set_irq_type
```

`desc->irq_data` 里的三件套（在 `gpiochip_irq_map` 时已写）：
| 字段 | 值 | 作用 |
|---|---|---|
| `.chip` | zynq `irq_chip`（mask/unmask/ack/set_type★函数表） | 操作手册 |
| `.chip_data` | `gpio_chip` → `gpiodev->data` → `zynq_gpio.base_addr` | 硬件基址 |
| `.hwirq` | 引脚号（如 50） | 对第几根操作，算 bank/位 |

- 触发方式：`chip->irq_set_type(d,...)` 用 `d->hwirq` 算 bank，写 `INT_TYPE/INT_POL/INT_ANY` 寄存器。
- 使能：generic irq 调 `chip->irq_unmask/irq_startup`，用 `chip_data` 拿基址置 `IER`。
- 中断来了：SPI20 chained handler → 分发到该 `desc` → `desc->handle_irq(=handle_level_irq)` → `chip->irq_ack/irq_mask` → 调 `desc->action->handler=` **你的 `key1_irq_handler`**。
- 全程用 `chip/chip_data/hwirq`，**不再查设备树**。

---

## 九、怎么核对实际 virq

- `/proc/interrupts` 第一列就是 virq；找 GPIO 控制器那几行可确认实际分配号。
- probe 里 `platform_get_irq` 返回的就是 GIC SPI20 那条（GIC 域 virq，不是 GPIO 引脚 virq）。
- 某引脚 virq 用 `gpio_to_irq(脚号)` / `gpiod_to_irq(desc)` 拿。

---

## 附：源码路径与行号索引

内核根：`src/build/tmp/work-shared/zynq-generic/kernel-source`

| 内容 | 文件:行 |
|---|---|
| driver probe 拿父中断 GIC | `drivers/gpio/gpio-zynq.c:921`（`platform_get_irq(pdev,0)`） |
| girq 配置（num_parents=1 等） | `drivers/gpio/gpio-zynq.c:969-981` |
| 自动建 irqchip+domain | `drivers/gpio/gpiolib.c:731`（`gpiochip_add_irqchip`）→`gpiochip_irqchip_add_key` |
| domain 建立 | `gpiolib.c` `gpiochip_irqchip_add_key` 里 `irq_domain_add_simple`；host_data=gc、ops=gpiochip_domain_ops |
| 父中断挂 GIC handler | `gpiochip_irqchip_add_key` 末尾 `irq_set_chained_handler_and_data(SPI20, parent_handler, gc)` |
| gpio_to_irq | `include/linux/gpio.h:79` |
| __gpio_to_irq | `include/asm-generic/gpio.h:110` |
| gpio_to_desc（选 chip） | `gpiolib.c:104` |
| gpiod_to_irq | `gpiolib.c:3180` |
| gpiochip_to_irq（掏 domain） | `gpiolib.c:1397`（`gc->to_irq` 默认此函数：`gpiolib.c:1549`） |
| irq_create_mapping | `kernel/irq/irqdomain.c`（wrapper） |
| platform_get_irq | `drivers/base/platform.c:260` |
| of_irq_get | `drivers/of/irq.c:387` |
| of_irq_parse_one / of_irq_find_parent | `drivers/of/irq.c:286` / `:54` |
| of_find_node_by_phandle | `drivers/of/base.c:1196` |
| struct of_phandle_args（oirq 类型） | `include/linux/of.h:74` |
| irq_find_host（预检：of_node→fwnode 进 list） | `include/linux/irqdomain.h:315`（→ irq_find_matching_host:309 / _fwnode:299） |
| of_phandle_args_to_fwspec（节点→fwspec） | `kernel/irq/irqdomain.c:737` |
| irq_create_of_mapping（节点→fwnode） | `kernel/irq/irqdomain.c:841` |
| irq_create_fwspec_mapping（fwnode→domain→映射） | `kernel/irq/irqdomain.c:750` |
| irq_find_matching_fwspec（按 fwnode 匹配） | `kernel/irq/irqdomain.c:370` |
| irq_create_mapping_affinity | `kernel/irq/irqdomain.c:637` |
| irq_domain_alloc_descs | `kernel/irq/irqdomain.c:1016` |
| __irq_alloc_descs / alloc_descs | `kernel/irq/irqdesc.c:772` |
| irq_domain_associate | `kernel/irq/irqdomain.c:519` |
| gpiochip_irq_map（写 chip/chip_data/挂 GIC） | `gpiolib.c:1297` |
| 反查表 linear_revmap | `irqdomain.h:413`（读）/`irqdomain.c:480`（写） |
| GIC SPI xlate（SPI→hwirq=+32） | `drivers/irqchip/irq-gic.c` `gic_irq_domain_translate` |

> 相关参考：本仓库 doc 下的《环境与编译.md》《调试与烧写方法.md》等。
```