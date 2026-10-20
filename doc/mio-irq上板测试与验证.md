# Zynq mio-irq 上板测试与验证文档

> ⚠️ **新板 MLK-F6P-CZ05（XC7Z015-2CLG485I）上本 demo 已停用**：该板 `MIO51` 是千兆网口 PHY 复位
> （Vivado PS7 `PCW_ENET0_RESET_IO = MIO 51`），模块 probe 会把该脚拉低/翻转，等于一直按住 PHY 复位，
> 表现为“系统起来但没有网口”。设备树节点、`CONFIG_mio-irq`、`mio-irq.bb` 均已关闭
> （见 `configs/rootfs_config` 与 `system-user.dtsi` 里的注释）。下文保留为**旧板 F6-7020** 的测试记录，
> 换回旧板或在 Vivado 里把 ENET0 reset 从 MIO51 摘掉后可原样复用。

- **目标模块**：`mio-irq`（内核模块，`/work/src/project-spec/meta-user/recipes-modules/mio-irq/`）
- **功能链路**：PS KEY2（**MIO50**）按键边沿中断 → `request_irq` 的 ISR 每触发一次翻转一次 PS LED（**MIO51**）
- **驱动实现**：`devm_gpiod_get(led) + devm_gpiod_get(key) + gpiod_to_irq(key) + request_irq`
- **设备树**：`project-spec/meta-user/recipes-bsp/device-tree/files/system-user.dtsi` 中的节点 `mio-irq`
  - `compatible = "xlnx,mio-irq"`
  - `led-gpios = <&gpio0 51 0>`（PS_MIO51 → 底板 PS 端 LED）
  - `key-gpios = <&gpio0 50 0>`（PS_MIO50 → 底板 KEY2）
- **配套原理**：见同目录《Zynq GPIO / 中断子系统 完全解析：从设备树到 virq》

> 阅读对象：拿到这套板子/镜像的人。本文给出**可复现**的实测步骤与预期输出（均已在本板实测通过）。

---

## 1. 环境与硬件映射

| 项 | 值 |
|----|----|
| 板卡 | Zynq-7020（xc7z020）|
| ARM 侧 | PetaLinux 5.10.0-xilinx-v2021.1 |
| 板 IP / 账号 | `10.10.10.110` / `root:root`（dropbear）|
| GPIO 芯片 | `zynq_gpio`，chip base=906，`ngpio=118` |
| KEY | PS_MIO50 → **gpio958**（sysfs 实测号），输入 |
| LED | PS_MIO51 → **gpio957**（sysfs），输出，由驱动持有 |
| 中断 virq | `gpiod_to_irq(key)` → **virq=56** |

> ⚠️ GPIO 的 sysfs 号（957/958）是**板上实测**得到，与 `base + MIO号` 之间因为有 bank 偏移不能简单相加，核对一律以
> `cat /sys/class/gpio/` 下实际导出的号为准。

MIO50/51 未被子系统外设（DDR/QSPI/ENET0/SDIO0/UART1/SDIO1/USB0）占用，因此在 XSA `PCW_GPIO_MIO_GPIO_IO=MIO` 下天然是 GPIO，无需在 Vivado 里单独配置。

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

因此上电后 `mio_irq` 已在运行：dmesg 出现 `mio_irq ready`、`/proc/interrupts` 已有 irq56，
直接按 KEY2 就会翻转 LED，全程**不需要手动加载**。

**如何确认自动加载生效（重启后，什么都不做）**：
```bash
dmesg | grep "mio_irq ready"     # 开机即有 => 自动加载生效
grep -i mio /proc/modules         # mio_irq ... Live
grep -E '^ *56:' /proc/interrupts # 已有 irq56
```

**手动重载 / 卸载（仅调试用，板上 bash PATH 为空需绝对路径）**：
- 已自动加载时，`/sbin/modprobe mio_irq` 不会重复加载（提示已在内核）；
- 想重载需先卸载再加载；若模块未固化进镜像，才需手动 insmod：
```bash
/sbin/modprobe -r mio_irq        # 卸载（先确认无占用）
/sbin/modprobe mio_irq            # 重新加载
# 或把单独编的 .ko 考到板上再 insmod（仅当镜像里没有时才需要）：
sshpass -proot scp .../mio_irq.ko root@10.10.10.110:/tmp/
/sbin/insmod /tmp/mio_irq.ko
```

**不想自动加载时**：删掉/清空 `/etc/modules-load.d/mio_irq.conf` 后重建镜像即可；需要时再手动 `/sbin/modprobe mio_irq`。

加载成功的标志（dmesg）：

```
mio_irq mio-irq: mio_irq ready: virq=56, key interrupt armed
```

---

## 3. 功能验证（上板实测步骤）

> 板上核实一律以 **dmesg / /proc/interrupts / sysfs** 实测为准（以上三套编号体系勿混，见《GPIO中断子系统完全解析》）。

### 3.1 设备树节点就位（构建用料对不对）

```bash
cat /proc/device-tree/mio-irq/compatible          # 期望含 xlnx,mio-irq
# led/key 引脚是二进制 u32 数组(phandle + offset + flags, 大端)，用 od 看：
head -c 12 /proc/device-tree/mio-irq/led-gpios | od -tx1   # 期望 ... 51 00(偏移=0x33=MIO51)
head -c 12 /proc/device-tree/mio-irq/key-gpios  | od -tx1   # 期望 ... 50 00(偏移=0x32=MIO50)
```

### 3.2 模块已加载并注册中断

> 本模块默认开机自动加载（见 §2.3），上电后无需任何手动操作即已就绪，直接按下面命令核对即可。

```bash
grep -i mio /proc/modules                  # 期望: mio_irq ... Live
dmesg | grep -i mio_irq                    # 期望: mio_irq ready: virq=56, key interrupt armed
grep -E '^ *56:' /proc/interrupts          # 期望: 56:  计数  zynq-gpio  50  Edge  mio_irq
```

注意 `/proc/interrupts` 那行的要点（也是易误判点）：

- `zynq-gpio 50` 里的 `50` 是 **GPIO 软件引脚索引，恰为 MIO50**，说明 key 确实落在 MIO50；
- **中断只有触发后才计数**，刚开机未按键时计数值为 0 属正常，不要当"没中断"。

### 3.3 按键 → 中断 → LED 翻转（核心验收）

持续按几下 KEY2，然后看两个计数是否同步增长、LED 是否翻转：

```bash
# ① 中断计数（期望每次按键 +1）
cat /proc/interrupts | grep -E '^ *56:'

# ② ISR 打印（期望按键次数递增、led 电平交替）
dmesg | grep "key pressed" | tail

# 实测输出（约 4 次）:
#   mio_irq mio-irq: key pressed 1 time(s) -> led=1
#   mio_irq mio-irq: key pressed 2 time(s) -> led=0
#   mio_irq mio-irq: key pressed 3 time(s) -> led=1
#   mio_irq mio-irq: key pressed 4 time(s) -> led=0
```

`/proc/interrupts` 计数值应等于 dmesg 里最大的 `key pressed N`，且 `-> led=` 在 1/0 间交替 → 每按一次 LED 物理亮灭翻转一次。

### 3.4 判定按键空闲/按下的电平（极性）

KEY 是输入，读 sysfs 电平即可（无需 export，仅读内核持有引脚的状态；如需强制 export 应先用 `/sbin/rmmod` 卸载否则 busy）：

```bash
while true; do printf '%s ' "$(cat /sys/class/gpio/gpio958/value)"; sleep 0.2; done   # Ctrl-C 结束
```

- 本板实测：**空闲恒读 0（低电平），按下拉到高（1）**。
- 因此驱动里 `IRQF_TRIGGER_FALLING`（下降沿）是在**松开瞬间**触发的；因每按必经一次下降沿，仍能达到"每按一次翻转一次"。

> 若希望"按下去那一刻"就触发（上升沿、语义更直觉），把 `mio_irq.c` 里 `IRQF_TRIGGER_FALLING` 改为
> `IRQF_TRIGGER_RISING` 重新编译上板即可。当前 FALLING 版本功能上已满足"按键翻转 LED"。

---

## 4. 判据（通过标准）

| 编号 | 判据 |
|------|------|
| C1 | DT 节点 `mio-irq` 存在，`compatible` 含 `xlnx,mio-irq`，`led-gpios` 偏移 51、`key-gpios` 偏移 50 |
| C2 | `/proc/modules`、dmesg 出现 `mio_irq ready: virq=56, key interrupt armed` |
| C3 | `/proc/interrupts` 出现 `56: ... zynq-gpio 50 Edge mio_irq` |
| C4 | 每按一次 KEY2，`/proc/interrupts` 的 `56:` 计数 +1 |
| C5 | dmesg `key pressed N -> led=` 的 N 随按键递增，且 `led=` 在 1/0 间交替（LED 物理翻转）|
| C6 | （附测）KEY 空闲电平 = 0（低），按下 = 1（高）|

C2–C5 全通过 => MIO50 按键边沿中断 → 翻转 MIO51 LED 的链路验收合格。

---

## 5. 故障排查

| 现象 | 可能原因 / 处理 |
|------|-----------------|
| `/proc/interrupts` 没有 `56:` 或不是 `Edge` | DT 节点没生效 / 非边沿触发。重建 device-tree 并重启（`led-gpios`/`key-gpios` 要匹配节点）|
| `mio_irq` 不在 `/proc/modules` 且 dmesg 无 ready | 正常开机即自动加载，此现象只出现在尚未固化进镜像、或已手动卸载后。先确认 §2.3 的 `mio_irq.conf` 存在；否则 `/sbin/modprobe mio_irq`，或按 §2.2 方式 A 固化 + `modules-load.d` |
| 按键计数不增 | 触发沿与按键极性不匹配。本板 KEY 空闲低/按下高，FALLING 在松开沿触发；若用 RISING 则按下沿触发，二者都"每按一次"。若换成其它按键/电平，需按 §3.4 实测电平后再选触发沿 |
| 开机后 LED 常亮 | 这是上电 FSBL 行为，与模块无关。工程镜像里的 `petalinux-image-minimal.bbappend` 的 `ps_led_off_cfg` 会在开机 `rc5.d/S99psled-off` 把 MIO51 配 out 拉低熄灭 |
| `echo 957 > /sys/class/gpio/export` 报 "Device or resource busy" | 属正常：该引脚已被驱动 `devm_gpiod_get(led)` 持有为输出，sysfs 不能再导出。验证 LED 用 dmesg `key pressed ... -> led=` 和物理灯，不由 sysfs 控制 |
| dmesg 里 `key pressed` 只出现一次就不涨 | 参考中断常驻问题排查（电平粘滞/掩蔽），并确认每次按键都确实产生边沿；必要时用 `/proc/interrupts` 计数兜底判断 |

---

## 6. 参考

- 同目录《Zynq GPIO / 中断子系统 完全解析：从设备树到 virq》（编号/GPIOD/irqdomain 原理）
- 同目录《petalinux-自定义应用与rootfs打包.md》（user-rootfsconfig 注册 + silentconfig 同步 + 镜像纳入）
- 同目录《环境与编译.md》《调试与烧写方法.md》（工程构建 / 上板）
- 源码：`project-spec/meta-user/recipes-modules/mio-irq/files/mio_irq.c`、`system-user.dtsi`

---