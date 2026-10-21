# petalinux-zynq7015

基于 **Docker** 的 PetaLinux **2021.1** 构建环境,面向 **Zynq-7000**(32 位 ARM Cortex-A9)。
当前目标板为 **米联客 MLK-F6P-CZ05(XC7Z015-2CLG485I)**;原 F6-7020(XC7Z020)工程与本工程
同属 zynq 模板,流程完全一致。

## 为什么用 Docker

- 编译环境可移植、可切换 —— 换机/换环境只需一个镜像 + 一份 SDK 卷。
- 目标板固定为 Zynq-7000(32 位),离线包用 `sstate_arm_2021.1.tar.gz`(不是 aarch64)。

## 目录结构

```
petalinux-zynq7015/
├── docker/
│   ├── Dockerfile            # OS(Ubuntu 18.04)+PetaLinux 依赖,不含 SDK
│   └── scripts/
│       └── auto-install.sh   # expect 自动应答安装器许可(无人值守)
├── sdk/                      # 唯一持久化卷(挂到容器 /opt/xilinx)
│   ├── petalinux/            #   ← SDK 本体(安装器写入 /opt/xilinx/petalinux)
│   ├── downloads/            #   ← 离线源码 pre-mirror
│   └── sstate/arm/           #   ← 32 位 sstate 缓存
├── src/              # petalinux 工程(arm,容器内 /work/src)
└── utils/
    ├── devops.py             # 统一入口(Linux/WSL)
    │                         #   build-image / install-sdk / unpack-offline / <命令...>
    ├── flash-sd.py           # SD 卡烧写(Linux/WSL 专用)
    └── wsl-disk.sh           # WSL 磁盘体检(Linux/WSL 专用)
```

`docker/patches/` 预留放需要的补丁(当前版本未用)。

## 文档

本 README 是总纲,模块化、细节化文档放在 `doc/` 下:

- [`doc/环境与编译.md`](doc/环境与编译.md) —— 环境搭建(Docker 镜像 / SDK / 离线包 / 进容器)+ 工程编译与打包
- [`doc/调试与烧写方法.md`](doc/调试与烧写方法.md) —— 调试期间镜像烧写方法(SD/QSPI/JTAG + TFTP/NFS、配合 Linux 调试)
- 业务模块说明(如 `gpio-demo`、`peekpoke`)规划中,后续随模块文档补进 `doc/`

## 使用流程

完整的环境搭建与编译步骤都在 [`doc/环境与编译.md`](doc/环境与编译.md),这里只给速览:

```bash
# Linux / WSL
./utils/devops.py build-image      # 1. 构建 Docker 镜像(一次性,只装 OS+依赖)
./utils/devops.py install-sdk      # 2. 安装 PetaLinux SDK 到 sdk/(前置:已下载安装器与离线包)
./utils/devops.py unpack-offline   # 3. 解压 downloads/sstate 离线包(可选但推荐,离线编译用)
./utils/devops.py                  # 4. 进容器 → source settings.sh → cd src → petalinux-build
```

## 体积与换机

- SDK（`sdk/` 目录）是唯一需要持久保留的东西;换机时整体拷走 + 重建镜像即可。
- petalinux 工程放在仓库根目录 `src/`(容器内 `/work/src`),不进 SDK,方便随代码一起管理。
- 离线包比较大,已解压后原 `.tar.gz` 可删除以省空间(见米联客手册第 5.2 节)。

## 说明与坑

- 基镜像用 Ubuntu 18.04(PetaLinux 2021.1 官方支持),20.04 缺 `libncurses5/libtinfo5`,易踩坑。
- Zynq-7000 是 32 位:模板用 `--template zynq`,sstate 用 `arm`,不是 zynqMP/aarch64。
- PetaLinux 安装器**拒绝 root 运行**,镜像内以 `plsdk` 用户(与宿主 UID/GID 对齐)执行。
- `sdk/` 不在 git 里:换机 / 重新 clone 后如果直接 `petalinux-build`,会缺 `settings.sh`。
  `devops.py` 现在会在启动容器前就拦下并提示先跑 `install-sdk`。

- **petalinux 的 silentconfig 失败会删工程配置**：失败时 `src/project-spec/configs/config`
  会被删掉（它要重写这个文件，重写失败就留在"删了"的状态）。真删了用
  `git checkout -- src/project-spec/configs/config` 恢复。
- **构建失败会留下"僵尸容器"**：bitbake 的 cooker server 失败时不退出，容器一直挂着并占着
  bitbake 锁（`bitbake.lock` 里记的是**容器内** PID，下次构建遇到同号 PID 会误判"锁被占用"，
  于是报 `Unable to connect to bitbake server` / `Failed to create workspace directory`）。
  `devops.py` 现在每次构建前自动清掉自己残留的构建容器，并删掉 build 目录里残留的
  `bitbake.sock` / `bitbake.lock` / `hashserve.sock`。手工排查：
  `docker ps --filter ancestor=petalinux:2021.1`。
- **空 TMPDIR 的第一次构建会报一条 `quilt-native` 签名错**：
  `ERROR: quilt-native-0.66-r0 do_rm_work: Taskhash mismatch` —— Xilinx 的 sstate 签名锁
  （`build/conf/locked-sigs.inc`）与本机重算的签名对不上，**只影响构建期工具、不影响产物**；
  第二次（增量）构建就干净了。
