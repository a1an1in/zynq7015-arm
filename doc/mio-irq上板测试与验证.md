# Zynq mio-irq 上板测试与验证文档

> ✅ **新板 MLK-F6P-CZ05（XC7Z015-2CLG485I）上已恢复，并已通过上板验证（2026-10-21）**：
> 该板 `MIO51` 是千兆网口 PHY 复位
> （Vivado PS7 `PCW_ENET0_RESET_IO = MIO 51`），旧版 demo 让模块 probe 时把该脚当 LED 翻转，
> 等于一直按住 PHY 复位，表现为“系统起来但没有网口”。所以**输出从 PS 的 MIO51 改到 PL 的 LED**：
> **PS 只负责按键中断，ISR 去写 PL 的 aurora 寄存器窗**（一条跨 PS/PL 的完整链路）。
> `MIO51` 全程交给 PS7 驱动，**Linux 不再碰它**，网口不受影响。
> 实测记录见 **§5**（上电全灭、按 PS 键翻丝印 LED1(H8)、网口正常）。
>
> 📌 **两种工作模式**（由设备树决定，可随时切）：
> - **模式 A：只测中断（默认推荐先测这个）** —— DT 里 `led-mask` 缺省或置 0 → 模块**不映射 PL
>   窗口、不驱动任何引脚**，ISR 只计数 + 打 dmesg。**烧什么位流都行**（run_led / aurora 都可）。
>   验证依据：`/proc/interrupts` 的计数 + dmesg 的 `key pressed N`。
> - **模式 B：中断 + 顺带翻 PL 的 LED** —— DT 里 `led-mask` 非 0 → ISR 额外写 aurora 的
>   `LED_VALUE/LED_CTRL`，按一次键翻一次 **PL 灯**。默认 `led-mask=0x8` → `led_o[3]` → H8 →
>   **丝印 LED1（H8）**（`led-mask=0x8`；寄存器位号与引脚顺序相反，bit3=H8；四颗全点 `0xf`）。
>   **需要 aurora 位流**（`run_led` 没有 regbank）。
>
> 切换方法：改 `system-user.dtsi` 里 `mio-irq` 节点的 `led-mask`（改成 0 或整行删掉 = 模式 A），
> 重新 `petalinux-build` 出镜像即可。

- **目标模块**：`mio-irq`（内核模块，`/work/src/project-spec/meta-user/recipes-modules/mio-irq/`）
- **功能链路**：PS 按键（**MIO50**）按下边沿中断 → `request_irq` 的 ISR 每触发一次 →
  写 PL `aurora` 寄存器窗 `0x40000000` 的 `LED_VALUE(0x10)` / `LED_CTRL(0x14)` → 翻转 **PL 的 LED**
- **驱动实现**：`devm_gpiod_get(key) + gpiod_to_irq(key) + request_irq` + `devm_ioremap(PL 寄存器窗)`
- **设备树**：`project-spec/meta-user/recipes-bsp/device-tree/files/system-user.dtsi` 中的节点 `mio-irq`
  - `compatible = "xlnx,mio-irq"`
  - `key-gpios = <&gpio0 50 0>`（PS_MIO50 → 底板 PS 按键）
  - `reg = <0x40000000 0x10000>`（PL aurora 寄存器窗，PS 视角 M_AXI_GP0 基址）
  - `led-value-reg = <0x10>` / `led-ctrl-reg = <0x14>` / `led-mask = <0x8>`（点丝印 LED1 = H8）
- **配套原理**：见同目录《Zynq GPIO / 中断子系统 完全解析：从设备树到 virq》

> 阅读对象：拿到这套板子/镜像的人。本文给出**可复现**的实测步骤与预期输出。
> ⚠️ 板上验证需要 **aurora 位流**（`led_o` 由 `aurora` 工程 regbank 驱动）；`run_led` 位流没有寄存器窗。

---

## 1. 环境与硬件映射

| 项 | 值 |
|----|----|
| 板卡 | 米联客 MLK-F6P-CZ05（xc7z015clg485-2）|
| ARM 侧 | PetaLinux 5.10.0-xilinx-v2021.1 |
| 板 IP / 账号 | `10.10.10.110` / `root:root`（dropbear）；主机名 `zynq7015`，MAC `00:0a:35:00:1e:53` |
| GPIO 芯片 | `zynq_gpio`（MIO50 天然是 GPIO，未被任何 PS 外设占用）|
| KEY | PS_MIO50 → 底板 PS 按键（上拉+RC，按下=低），输入 |
| LED（输出） | **PL 的 `led_o[3]`** → **H8** → 板上丝印 **LED1**；经 `aurora` 寄存器窗 `0x40000000` 写（`led-mask=0x8`）|
| LED 寄存器 | `LED_VALUE 0x10`（[3:0] 选灯，**写 1 = 点亮**）/ `LED_CTRL 0x14`（bit0=使能）/ `LED_STATUS 0x18`（只读，1=亮）|
| LED 极性 | 板载 LED **低电平点亮**（0=亮）；RTL 已取反 → 寄存器语义仍是「写 1 = 点亮」，`LED_CTRL=0` 时 4 颗全灭（**上电默认不亮**）|
| 中断 virq | `gpiod_to_irq(key)` → **实测 56**（`/proc/interrupts` 里 `56: ... zynq-gpio 50 Edge mio_irq`）|
| 工作模式 | A=只测中断（`led-mask` 缺省/0，任意位流都行）；B=中断+PL LED（`led-mask`≠0，需 aurora 位流）|
| 位流 | 模式 B 必须是 **aurora**（有 regbank）；`run_led` 无寄存器窗 |
| 验证状态 | ✅ **2026-10-21 上板实测通过**（详见 §5）|

> ⚠️ 关于 MIO51：手册“12:千兆网口”把它标成网口复位，“24:LED”表和厂商例程 DTS 又把它当 PS LED
> （资料冲突，见 fpga 仓库 `docs/board-io-peripherals.md` §4）。本工程按 **ENET0 复位** 处理 ——
> **Linux 一律不碰 MIO51**，所以网口正常。

MIO50 未被子系统外设（DDR/QSPI/ENET0=16..27,51,52,53/SDIO0/UART1/SDIO1/USB0）占用，因此在 XSA `PCW_GPIO_MIO_GPIO_IO=MIO` 下天然是 GPIO，无需在 Vivado 里单独配置；且 MIO50 落在 zynq-gpio 支持中断的 MIO 组（0..15 / 50..53）内。

---

## 2. 构建与部署加载

### 2.1 先让 recipe 存在（本模块已就位，只需知道位置）

- recipe：`project-spec/meta-user/recipes-modules/mio-irq/mio-irq.bb`
- 源码：`project-spec/meta-user/recipes-modules/mio-irq/files/mio_irq.c`（Makefile 同目录）
- 设备树：`project-spec/meta-user/recipes-bsp/device-tree/files/system-user.dtsi`

### 2.2 拿到 `.ko`（统一入口：petalinux-build）

`.ko` 统一由 `./utils/devops.py petalinux-build` 编译产生：

```bash
# 进容器执行 petalinux-build：全量构建，产出含 mio_irq.ko
./utils/devops.py petalinux-build
# 只想先验证模块能否编出（更快，单编该 recipe）：
./utils/devops.py petalinux-build -c mio-irq
```

产物位置（以单编为例）：

```bash
find src/build/tmp/work -name mio_irq.ko
#   .../zynq_generic-xilinx-linux-gnueabi/mio-irq/1.0-r0/mio_irq.ko
```

> 提示：`petalinux-build` 只负责"编译出 .ko"；"是否把该模块装进最终 rootfs / 开机自动加载"由
> `user-rootfsconfig` 注册 + `petalinux-config -c rootfs --silentconfig`（同步 `IMAGE_INSTALL`）+
> `/etc/modules-load.d/` 决定，详见《petalinux-自定义应用与rootfs打包.md》与 §2.3。
> （注意：只手动改 `rootfs_config` 不会让它进镜像——真正决定打包的是 `build/conf/plnxtool.conf`。）

### 2.3 加载方式（默认：开机自动加载）

本模块**随开机自动加载，无需手动 insmod/modprobe**，因为它同时具备自动加载的两个前提：

1. 模块已固化进镜像：`/lib/modules/5.10.0-xilinx-v2021.1/extra/mio_irq.ko`；
2. 打开自启开关：`/etc/modules-load.d/mio_irq.conf`（内容即模块名 `mio_irq`），
   启动早期内核模块加载器读取 `/etc/modules-load.d/*.conf` 逐行 modprobe。

因此上电后 `mio_irq` 已在运行：dmesg 出现 `mio_irq ready`、`/proc/interrupts` 已有 `zynq-gpio 50 Edge mio_irq` 那一行，
直接按板上 PS 按键就会翻转 **PL 的丝印 LED1（H8）**，全程**不需要手动加载**。

**如何确认自动加载生效（重启后，什么都不做）**：
```bash
dmesg | grep "mio_irq ready"     # 开机即有 => 自动加载生效
grep -i mio /proc/modules         # mio_irq ... Live
grep -E 'zynq-gpio.*50.*mio_irq' /proc/interrupts   # 已有 MIO50 的中断行
```

**手动重载 / 卸载（仅调试用，板上 bash PATH 为空需绝对路径）**：
- 已自动加载时，`/sbin/modprobe mio_irq` 不会重复加载（提示已在内核）；
- 想重载需先卸载再加载；若模块未固化进镜像，才需手动 insmod：
```bash
/sbin/modprobe -r mio_irq        # 卸载（先确认无占用）
/sbin/modprobe mio_irq            # 重新加载
# 或把单独编的 .ko 考到板上再 insmod（仅当镜像里没有时才需要）：
sshpass -proot scp .../mio_irq.ko root@<板子IP>:/tmp/
/sbin/insmod /tmp/mio_irq.ko
```

**不想自动加载时**：删掉/清空 `/etc/modules-load.d/mio_irq.conf` 后重建镜像即可；需要时再手动 `/sbin/modprobe mio_irq`。

加载成功的标志（dmesg）：

```
mio_irq mio-irq: mio_irq ready: virq=<本板实测>, key(MIO50) interrupt armed, mode=led
# 模式 A（只测中断）则是: ... mode=interrupt-only (no pl led)
```

---

## 3. 功能验证（上板实测步骤）

> 板上核实一律以 **dmesg / /proc/interrupts / sysfs** 实测为准（以上三套编号体系勿混，见《GPIO中断子系统完全解析》）。

### 3.1 设备树节点就位（构建用料对不对）

```bash
cat /proc/device-tree/mio-irq/compatible          # 期望含 xlnx,mio-irq
# key 引脚是二进制 u32 数组(phandle + offset + flags, 大端)，用 od 看：
head -c 12 /proc/device-tree/mio-irq/key-gpios  | od -tx1   # 期望 ... 32 00(偏移=0x32=MIO50)
# PL 寄存器窗与 LED 寄存器偏移：
od -tx1 /proc/device-tree/mio-irq/reg            # 期望 00 00 00 40 00 00 01 00 (0x40000000/0x10000)
od -tx1 /proc/device-tree/mio-irq/led-value-reg   # 期望 10 00 00 00 (0x10)
od -tx1 /proc/device-tree/mio-irq/led-ctrl-reg    # 期望 14 00 00 00 (0x14)
od -tx1 /proc/device-tree/mio-irq/led-mask        # 模式 B: 08 00 00 00 (led_o[3] -> H8 -> 丝印 LED1)
                                                  # 模式 A: 该属性缺省或全 0 (= 不点灯)
```

### 3.2 模块已加载并注册中断

> 本模块默认开机自动加载（见 §2.3），上电后无需任何手动操作即已就绪，直接按下面命令核对即可。

```bash
grep -i mio /proc/modules                  # 期望: mio_irq ... Live
dmesg | grep -i mio_irq                    # 期望: mio_irq ready: virq=<N>, key(MIO50) interrupt armed, mode=led|interrupt-only ...
grep -E 'zynq-gpio.*50.*mio_irq' /proc/interrupts   # 期望: <N>:  计数  zynq-gpio  50  Edge  mio_irq
```

注意 `/proc/interrupts` 那行的要点（也是易误判点）：

- `zynq-gpio 50` 里的 `50` 是 **GPIO 软件引脚索引，恰为 MIO50**，说明 key 确实落在 MIO50；
- **中断只有触发后才计数**，刚开机未按键时计数值为 0 属正常，不要当"没中断"。

### 3.3 按键 → 中断 → LED 翻转（核心验收）

持续按几下板上 PS 按键（MIO50），然后看两个计数是否同步增长、PL 的 LED 是否翻转：

```bash
# ① 中断计数（期望每次按键 +1）
cat /proc/interrupts | grep -E 'zynq-gpio.*50.*mio_irq'

# ② ISR 打印（期望按键次数递增、pl led 状态交替）
dmesg | grep "key pressed" | tail

# ③ PL LED 实际输出（LED_STATUS 0x18 的 [3:0]，用现成的 fpga 工具读）
fpga read -b 0x40000000 0x18        # 期望低 4 位 = 当前点亮的 led_o 位图
fpga read -b 0x40000000 0x10        # LED_VALUE 寄存器值

# 实测输出（约 4 次）:
#   模式 A(只测中断):
#     mio_irq mio-irq: key pressed 1 time(s) -> interrupt only(no led)
#     mio_irq mio-irq: key pressed 2 time(s) -> interrupt only(no led)
#   模式 B(中断+PL LED):
#     mio_irq mio-irq: key pressed 1 time(s) -> pl led toggled
#     mio_irq mio-irq: key pressed 2 time(s) -> pl led toggled
```

`/proc/interrupts` 计数值应等于 dmesg 里最大的 `key pressed N` → **每一次按键都产生了一次中断**。
模式 B 下还应看到 **PL 的丝印 LED1（H8）** 物理亮灭翻转（`fpga read -b 0x40000000 0x18` 的低 4 位同步变化，1=亮）；
模式 A 下没有灯变化，这完全正常 —— **中断链路本身与灯无关**。

> ⚠️ `fpga` 工具的默认基址是 `0x50000000`（那是 DMA 窗口），**读 aurora 寄存器要显式 `-b 0x40000000`**。

### 3.4 判定按键空闲/按下的电平（极性）

KEY 是输入，读 sysfs 电平即可（无需 export，仅读内核持有引脚的状态；如需强制 export 应先用 `/sbin/rmmod` 卸载否则 busy）：

```bash
# MIO50 在 7015 上的 sysfs 号以板上实际导出为准，先列出来：
ls /sys/class/gpio/ | grep gpio
# 找到 MIO50 对应的号后读电平（Ctrl-C 结束）：
while true; do printf '%s ' "$(cat /sys/class/gpio/gpioXXX/value)"; sleep 0.2; done
```

- 空闲/按下的电平**以本板实测为准**（板上按键为上拉 + RC，按下为低）。
  上下拉网络，未必相同）。
- 触发沿是 `IRQF_TRIGGER_FALLING`；无论按下/松开哪个沿触发，每按一次都会产生一次下降沿，
  都能达到"每按一次翻转一次"。

> 若希望"按下去那一刻"就触发（上升沿、语义更直觉），把 `mio_irq.c` 里 `IRQF_TRIGGER_FALLING` 改为
> `IRQF_TRIGGER_RISING` 重新编译上板即可。

---

## 4. 判据（通过标准）

| 编号 | 判据 |
|------|------|
| C1 | DT 节点 `mio-irq` 存在，`compatible` 含 `xlnx,mio-irq`，`key-gpios` 偏移 50，`reg` = 0x40000000/0x10000，`led-value-reg`=0x10、`led-ctrl-reg`=0x14 |
| C2 | `/proc/modules`、dmesg 出现 `mio_irq ready: virq=<N>, key(MIO50) interrupt armed, mode=led`（模式 A 为 `mode=interrupt-only (no pl led)`）|
| C3 | `/proc/interrupts` 出现 `<N>: ... zynq-gpio 50 Edge mio_irq`（`<N>` 以本板实际为准）|
| C4 | 每按一次板上 PS 按键，`/proc/interrupts` 里 `mio_irq` 那一行的计数 +1 |
| C5 | （**仅模式 B**）dmesg `key pressed N -> pl led toggled` 的 N 随按键递增，且**丝印 LED1(H8)** 物理翻转（`fpga read -b 0x40000000 0x18` 同步）|
| C6 | （附测）KEY 空闲/按下电平与 §3.4 实测一致 |
| C7 | （附测）**网口正常**（`ip a` 有 eth0、能 DHCP）—— 证明 MIO51 全程由 PS7 驱动、未被 Linux 拉低 |

- **只测中断（模式 A）**：C1、C2、C3、C4、C6、C7 全通过即算合格 —— **中断链路与灯无关**。
- **带灯（模式 B）**：再加 C5。
- C7 通过 => 没碰 PHY 复位（网口正常）。
- ✅ **实测（2026-10-21，模式 B）：C1~C7 全部通过**，记录见 §5。

---

## 5. 上板实测记录（2026-10-21）

**被测对象**

| 项 | 值 |
|----|----|
| 板卡 | 米联客 MLK-F6P-CZ05（MLK-F6P 底板 + MLK-CZ05-7015 核心模块，丝印 XC7Z015-2CLG485I）|
| 板子 | IP `10.10.10.110`、MAC `00:0a:35:00:1e:53`、账号 `root:root`（dropbear）、主机名 `zynq7015` |
| 内核 | `5.10.0-xilinx-v2021.1`（PetaLinux 2021.1）|
| 位流 | `aurora.bit` md5 `0f2cf7862f7102356c1678e26810418c`（1 519 248 B，含 LED 极性取反修正）|
| XSA | md5 `9c41f00b52750d70c114dc89825be17f`（701 345 B）→ `docker/archives/system.xsa` |
| 镜像 | `BOOT.BIN` 2 570 044 B、`image.ub` 13 335 284 B、`system.dtb` 20 155 B（`led-mask=0x8`）|
| rootfs | `extra/mio_irq.ko` + `/etc/modules-load.d/mio_irq.conf`（开机自加载；实测 `mio_irq 16384 0 - Live`）|

**实测输出（板上原样）**

```text
# dmesg | grep -i mio_irq
mio_irq: loading out-of-tree module taints kernel.
mio_irq 40000000.mio-irq: mio_irq ready: virq=56, key(MIO50) interrupt armed, mode=led
mio_irq 40000000.mio-irq: key pressed 1 time(s) -> pl led toggled
mio_irq 40000000.mio-irq: key pressed 2 time(s) -> pl led toggled

# /proc/interrupts | grep zynq-gpio
 56:          2          0  zynq-gpio  50 Edge      mio_irq

# /proc/device-tree/mio-irq/led-mask | od -tx1
0000000 00 00 00 08
# /proc/device-tree/mio-irq/key-gpios | od -tx1
0000000 00 00 00 08 00 00 00 32 00 00 00 00     ← phandle, 偏移 0x32=MIO50, flags 0
```

**实测结论（模式 B，`led-mask=0x8`）**

| 判据 | 实测 |
|------|------|
| 中断 | **virq=56**（`zynq-gpio 50 Edge mio_irq`），每按一次计数 +1 ✅ |
| LED | 按 PS 按键 → **丝印 LED1（H8）翻转** ✅（= `led_o[3]`，`led-mask=0x8`）|
| 上电 | 4 颗 PL 灯**全灭** ✅（旧位流是全开；RTL 取反后 `LED_CTRL=0` → 输出全 1）|
| 网口 | `macb e000b000.ethernet eth0: PHY [...] driver [Generic PHY]` + **`Link is Up - 1Gbps/Full`** ✅ |
| MIO51 | `/sys/class/gpio/` 下只有 `gpiochip906`（没有单独导出的 gpioN）→ Linux 没碰 MIO51，PHY 复位释放 ✅ |
| 开机脚本 | 无 `S99psled-off` ✅ |

> **极性结论（实测确认）**：板载 LED **低电平点亮**（引脚 0=亮、1=灭）。
> `aurora_led.v` / `run_led.v` 已取反，于是寄存器语义保持「**写 1 = 点亮**」，
> `LED_CTRL=0` 时 4 颗全灭（上电默认不亮）。
>
> **注**：`Generic PHY` 是预期现象 —— Xilinx 5.10 内核没有 `motorcomm.c`（YT8531 的专用驱动要到 6.x），
> 本工程把它当通用 PHY 用；千兆链路实测能协商到 1Gbps/Full。
> 详见 fpga 仓库 `docs/board-io-peripherals.md` §六。

---

## 6. 故障排查

| 现象 | 可能原因 / 处理 |
|------|-----------------|
| `/proc/interrupts` 里没有 `zynq-gpio ... 50 ... mio_irq` 或不是 `Edge` | DT 节点没生效 / 非边沿触发。重建 device-tree 并重启（`key-gpios` 要匹配节点）|
| `mio_irq` 不在 `/proc/modules` 且 dmesg 无 ready | 正常开机即自动加载，此现象只出现在尚未固化进镜像、或已手动卸载后。先确认 §2.3 的 `mio_irq.conf` 存在；否则 `/sbin/modprobe mio_irq`，或按 §2.2 方式 A 固化 + `modules-load.d` |
| probe 报 `no 'reg' resource` / `ioremap ... failed` | 模式 B 下 DT 缺 `reg`（PL 寄存器窗）或地址越界；**已自动退化成模式 A**（只中断不点灯），dmesg 会打 warn 提示 |
| 按键计数不增 | 触发沿与按键极性不匹配。按 §3.4 实测电平后再选 FALLING/RISING；两者都"每按一次" |
| dmesg 有 `key pressed` 但 **PL 的灯不亮** | ① 烧的不是 `aurora` 位流（`run_led` 没有 regbank）；② `led-mask` 选的位与实际观察的灯对不上（bit0→U16、bit1→AB21、bit2→T16、bit3→H8=丝印 LED1）；③ 位流不是带取反的新版（2026-10-21 之前的 aurora.bit 是「1=亮」语义，会点反）—— 用新位流即可 |
| `fpga read` 读不到 0x40000000 窗口 | 基址用错：aurora 寄存器窗在 **0x40000000**（`fpga` 默认的 0x50000000 是 DMA 窗口）；或 UIO 未加载（`modprobe uio_pdrv_genirq`），此时用 `/dev/mem` 回退 |
| **网口不通 / `eth0` 起不来** | 检查 MIO51：它必须保持由 PS7 驱动为 ENET0 复位（高=释放复位）。**Linux 侧任何把 MIO51 对应的 sysfs 号 export 成 out 拉低的操作都会把 PHY 按在复位里**（号以板上 `ls /sys/class/gpio` 为准）—— `S99psled-off` 脚本正是拉低 MIO51 把网口弄没的，该脚本已停用 |
| dmesg 里 `key pressed` 只出现一次就不涨 | 参考中断常驻问题排查（电平粘滞/掩蔽），并确认每次按键都确实产生边沿；必要时用 `/proc/interrupts` 计数兜底判断 |

---

## 7. 参考

- 同目录《Zynq GPIO / 中断子系统 完全解析：从设备树到 virq》（编号/GPIOD/irqdomain 原理）
- 同目录《petalinux-自定义应用与rootfs打包.md》（user-rootfsconfig 注册 + silentconfig 同步 + 镜像纳入）
- 同目录《环境与编译.md》《调试与烧写方法.md》（工程构建 / 上板）
- 源码：`project-spec/meta-user/recipes-modules/mio-irq/files/mio_irq.c`、`system-user.dtsi`

---