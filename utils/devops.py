#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PetaLinux 2021.1 Docker 构建入口（Linux / WSL / Windows，统一用 python utils/devops.py，需保证 python 命令可用）。

把原来分散的 shell 脚本（build-image.sh / install-sdk.sh /
unpack-offline.sh / devops.sh）合并为一个 Python 入口，编译仍全部在 Docker 里
执行，宿主只负责拼出 docker 触发命令（--network host + bind mount）。

用法：
    python utils/devops.py                        # 交互 shell，/work 为仓库根目录
    python utils/devops.py petalinux-build        # 容器内 source settings.sh 后执行命令
    python utils/devops.py petalinux-package --boot --u-boot --fpga --force
    python utils/devops.py build-image            # 构建 docker 镜像
    python utils/devops.py install-sdk [--interactive]   # 安装 SDK 到 sdk/
    python utils/devops.py unpack-offline         # 解压 downloads/sstate 离线包
    python utils/devops.py sdk-to-volume [--from <宿主sdk目录>] [--to <卷名>]
    python utils/devops.py build-to-volume [--from <宿主build目录>] [--to <卷名>]
    python utils/devops.py help

SDK 默认放在宿主的 <仓库>/sdk 目录（bind mount 到容器 /opt/xilinx）。
Windows 原生编译要改用 docker 命名卷（SDK_VOLUME=petalinux-sdk 等）——宿主只有
NTFS：SDK 与 Yocto 的 TMPDIR 都得落在卷里（VM 内 ext4）才能编，且 build/ 与
src/components/yocto 也建议进卷（Windows 的 9p 文件共享会让 bitbake 解析层文件卡死）。

可配环境变量：
    IMG_NAME            默认 petalinux-dev:2021.1
    HOST_UID/HOST_GID   构建镜像时的宿主 UID/GID（默认自动探测）
    PETALINUX_INSTALLER 显式指定安装器路径
    DOWNLOADS_TGZ/SSTATEGZ  显式指定离线包压缩包路径
    OFFLINE_SRC         容器内 /offline_src 的只读来源目录
    PETALINUX_PROJ      容器内工程目录，默认 /work/src
    DOCKER_NETWORK      强制指定网络模式（Linux/WSL 默认 --network host；Windows 用默认桥接）
    SDK_DIR             SDK 宿主目录覆盖（默认 <仓库>/sdk）
    SDK_VOLUME         SDK 放 docker 命名卷（如 petalinux-sdk），Win/WSL 共用同一份
    BUILD_VOLUME       整个 build/ 放命名卷（如 petalinux-build）
    TMPDIR_VOLUME      只把 build/tmp 放命名卷（Windows 原生编译必设；**不要用 BUILD_VOLUME**，
                        整个 build/ 进卷会让 petalinux 写配置时跨设备 rename 失败）
    COMPONENTS_VOLUME  把 src/components/yocto 放命名卷（避开 Windows 9p 卡死；需先把层文件
                        灌进卷，petalinux 就能跳过展开）
    （任一卷名设成 none/host 即退回宿主目录）
"""

import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

IMG_NAME_DEFAULT = "petalinux-dev:2021.1"
SDK_VOLUME_DEFAULT = "petalinux-sdk"
BUILD_VOLUME_DEFAULT = "petalinux-build"
TMPDIR_VOLUME_DEFAULT = "petalinux-tmp"
COMPONENTS_VOLUME_DEFAULT = "petalinux-components"
INSTALLER_NAME = "petalinux-v2021.1-final-installer.run"
DOWNLOADS_NAME = "downloads_2021.1_update1.tar.gz"
SSTATE_NAME = "sstate_arm_2021.1.tar.gz"

HELP = """\
PetaLinux 2021.1 Docker 构建入口（Linux / WSL / Windows，统一用 python utils/devops.py，需保证 python 命令可用）

用法：
  python utils/devops.py                       交互 shell（/work 为仓库根目录）
  python utils/devops.py <命令...>             容器内 source settings.sh 后执行该命令
  python utils/devops.py build-image           构建 Docker 镜像（只含 OS+依赖）
  python utils/devops.py install-sdk           用 auto-install.sh 安装 SDK 到 sdk/
  python utils/devops.py install-sdk --interactive   交互式运行真实安装器
  python utils/devops.py unpack-offline        解压 downloads/sstate 离线包到 sdk/
  python utils/devops.py sdk-to-volume [--from <宿主目录>] [--to <卷名>]
                                                把宿主 sdk/ 灌进 docker 命名卷（Win/WSL 共用）
  python utils/devops.py build-to-volume [--from <宿主目录>] [--to <卷名>] [--tmp-from <卷名>]
                                                把宿主 build/ 灌进 docker 命名卷
  python utils/devops.py repair [--dry-run]    修复中断构建留下的坏状态（uninative / recipe-sysroot）
  python utils/devops.py help                  显示本帮助

示例：
  python utils/devops.py petalinux-build
  python utils/devops.py petalinux-package --boot --u-boot --fpga --force
  # Windows 原生（Docker Desktop）：SDK 与 TMPDIR 要进卷；
  # components 默认用 tmpfs（卷不存在时），也可以显式指定
  $env:SDK_VOLUME="petalinux-sdk"; $env:TMPDIR_VOLUME="petalinux-tmp"
  $env:COMPONENTS_VOLUME="tmpfs"
  python utils/devops.py petalinux-build

环境变量：IMG_NAME、HOST_UID、HOST_GID、PETALINUX_INSTALLER、DOWNLOADS_TGZ、
          SSTATEGZ、OFFLINE_SRC、PETALINUX_PROJ、DOCKER_NETWORK、SDK_DIR、
          SDK_VOLUME（SDK 放命名卷）、BUILD_VOLUME（整个 build/ 放卷，**不要用**）、
          TMPDIR_VOLUME（只放 build/tmp）、COMPONENTS_VOLUME（放 src/components/yocto；
          设为 tmpfs 用内存盘，Windows 上卷不存在时默认就是 tmpfs）
          卷名设 none/host 即退回宿主目录。
"""


def log(msg):
    print(msg, flush=True)


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def here():
    """仓库根目录（utils/ 的上一级）。"""
    return Path(__file__).resolve().parent.parent


def env(name):
    v = os.environ.get(name)
    return v if v else None


def img_name():
    return env("IMG_NAME") or IMG_NAME_DEFAULT


def sdk_dir():
    return here() / "sdk"


def docker_bin():
    return shutil.which("docker") or "docker"


def is_windows():
    return os.name == "nt"


def is_linux():
    return sys.platform.startswith("linux")


def _vol_env(name, default=None):
    """读卷名环境变量；值为 none/host/-/off 时表示"别用卷，走宿主目录"。"""
    v = env(name)
    if v is not None:
        if v.lower() in ("none", "host", "-", "off"):
            return None
        return v
    return default


_vol_cache = {}
_vol_noted = False


def _auto_volume(name):
    """Windows 原生编译：卷已建好就自动用它（免得每次都设环境变量）。

    只在 Windows 下自动；Linux/WSL 保持"什么都不设 = 全用宿主目录"。
    """
    if name in _vol_cache:
        return _vol_cache[name]
    found = None
    if is_windows():
        try:
            rc = subprocess.call([docker_bin(), "volume", "inspect", name],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            if rc == 0:
                found = name
        except Exception:
            found = None
    _vol_cache[name] = found
    return found


def sdk_volume():
    """SDK 放哪个 docker 命名卷（Windows 下卷存在就自动用）。"""
    return _vol_env("SDK_VOLUME", _auto_volume(SDK_VOLUME_DEFAULT))


def build_volume():
    """整个 build/ 放哪个 docker 命名卷。

    注意：**不要用** —— petalinux 写配置时会跨设备 rename 失败(EXDEV)。
    """
    return _vol_env("BUILD_VOLUME")


def tmpdir_volume():
    """只把 build/tmp 放哪个 docker 命名卷（Windows 下卷存在就自动用）。"""
    return _vol_env("TMPDIR_VOLUME", _auto_volume(TMPDIR_VOLUME_DEFAULT))


def components_tmpfs():
    """components 用 tmpfs（内存盘）代替卷/宿主目录。

    为什么需要它（两个原因）：
      1) petalinux 展开 Yocto SDK 时，安装器内部的 tar 会 chmod 目标根目录。Windows 侧
         创建的目录在 9p 里是 root 属主，容器用户(uid 1000)不是 owner，于是报

             tar: .: Cannot change mode to rwxr-xr-x: Operation not permitted
             ERROR: Failed to Extract Yocto SDK.

         （注：9p 本身是支持 chmod 的——挂载带 metadata；失败原因是属主。）
      2) 即使展开成功，bitbake 解析 components/yocto 下 1.3 万个层文件时每个都要走 9p，
         实测会长时间卡在 "Parsing recipes..."（10 月那次也是）。

    tmpfs 两个问题都没有，而且整个展开只要约 1 秒（99 M / 13k 文件）。

    取值：
      COMPONENTS_VOLUME=tmpfs   显式用 tmpfs
      COMPONENTS_VOLUME=<卷名>   用命名卷
      COMPONENTS_VOLUME=none    退回宿主目录（NTFS，展开会失败，只用于排查）
      不设：Windows 上若默认卷存在就用卷，不存在则自动用 tmpfs；
            Linux/WSL 保持宿主目录（ext4 本来就支持 chmod）
    """
    v = env("COMPONENTS_VOLUME")
    if v is not None:
        return v.strip().lower() == "tmpfs"
    if not is_windows():
        return False
    return _auto_volume(COMPONENTS_VOLUME_DEFAULT) is None


def components_volume():
    """把 src/components/yocto 放哪个 docker 命名卷（Windows 下卷存在就自动用）。"""
    if components_tmpfs():
        return None
    return _vol_env("COMPONENTS_VOLUME", _auto_volume(COMPONENTS_VOLUME_DEFAULT))


COMPONENTS_MARKER = "environment-setup-cortexa9t2hf-neon-xilinx-linux-gnueabi"


def components_bootstrap():
    """tmpfs 模式下，每次容器启动时把 Yocto SDK 展开进去（约 1 秒）。

    tmpfs 是易失的，但展开很快；比让 bitbake 去读 9p 上的 1.3 万个文件划算得多。
    """
    if not components_tmpfs():
        return ""
    return (
        'if [ ! -f "/work/src/components/yocto/{marker}" ]; then '
        '  echo ">> [info] components 用 tmpfs，展开 Yocto SDK ..."; '
        '  src=/opt/xilinx/petalinux/components/yocto/source/arm; '
        '  [ -f "$src" ] || src=/opt/xilinx/petalinux/components/yocto/source/aarch64; '
        # 必须用 -y：安装器脚本里 answer="" 会清掉环境变量，只有 -y 才会自动应答；
        # 再补 </dev/null，即使临时开了 TTY 也不会卡在 "Proceed [Y/n]?" 上。
        '  if ! "$src" -y -d /work/src/components/yocto '
        '       </dev/null >/tmp/components-install.log 2>&1; then '
        '    tail -20 /tmp/components-install.log; '
        '    echo "!! components 展开失败（详见 /tmp/components-install.log）"; exit 1; '
        '  fi; '
        # 安装器会在根目录留 .devtoolbase；它的存在会让 devtool 认为这是 fixed setup，
        # 从而不注册 create-workspace，petalinux 随后就报 "Failed to create workspace
        # directory"。卷里那份没有这个文件，所以这里删掉保持一致。
        '  rm -f /work/src/components/yocto/.devtoolbase; '
        'fi; '
    ).format(marker=COMPONENTS_MARKER)


def network_args():
    """docker run/build 的网络模式参数。

    - Linux/WSL：默认 --network host（便于访问宿主网络）
    - macOS/Windows(Docker Desktop)：不支持 --network host，省略走默认桥接
    - 可用 DOCKER_NETWORK 强制覆盖
    """
    override = env("DOCKER_NETWORK")
    if override:
        return ["--network", override]
    if is_linux():
        return ["--network", "host"]
    return []


def mount_volume(host, container, read_only=False):
    suffix = ":ro" if read_only else ""
    host = str(host)
    # Windows 盘符路径（C:\...）转正斜杠，避免 Docker 解析出错。
    # 但 **UNC 路径（\\wsl.localhost\...、\\server\share）必须保留反斜杠**：
    # 实测转成正斜杠（//wsl.localhost/...）后 Docker 会**静默忽略**这个挂载，
    # 容器里的 /work 变成镜像自带的空目录，petalinux 随后报
    # "You are not inside a PetaLinux project"。
    if is_windows() and not host.startswith("\\\\"):
        host = host.replace("\\", "/")
    return "{}:{}{}".format(host, container, suffix)


def sdk_host_dir():
    """SDK 在宿主的目录；可用 SDK_DIR 覆盖（默认 <仓库>/sdk）。"""
    v = env("SDK_DIR")
    return Path(v).expanduser() if v else sdk_dir()


def sdk_mount():
    """SDK 的 -v 参数：优先 docker 命名卷，否则宿主目录 bind mount。"""
    vol = sdk_volume()
    if vol:
        return "{}:/opt/xilinx".format(vol)
    return mount_volume(sdk_host_dir(), "/opt/xilinx")


def sdk_desc():
    """日志/报错里用的 SDK 位置描述。"""
    vol = sdk_volume()
    if vol:
        return "docker 卷 {}".format(vol)
    return str(sdk_host_dir())


def sdk_ready():
    """SDK 是否可用：命名卷进容器探 settings.sh，宿主目录直接查文件。"""
    vol = sdk_volume()
    if vol:
        # 先确认卷存在，避免探测时把空卷凭空建出来
        if subprocess.call([docker_bin(), "volume", "inspect", vol],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) != 0:
            return False
        try:
            rc = subprocess.call(
                [docker_bin(), "run", "--rm",
                 "-v", "{}:/opt/xilinx:ro".format(vol),
                 "--entrypoint", "sh", img_name(), "-c",
                 "test -f /opt/xilinx/petalinux/settings.sh"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return rc == 0
        except Exception:
            return False
    return (sdk_host_dir() / "petalinux" / "settings.sh").is_file()


def offline_src():
    """容器内 /offline_src 的只读来源目录（可选）。"""
    v = env("OFFLINE_SRC")
    if v:
        p = Path(v)
        return p if p.is_dir() else None
    archives = here() / "docker" / "archives"
    if archives.is_dir():
        return archives
    p = Path("/mnt/c/Users/a1an1in/Downloads/baidu")
    return p if p.is_dir() else None


def want_tty():
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def host_uid():
    v = env("HOST_UID")
    if v:
        return v
    try:
        return str(os.getuid())
    except AttributeError:
        return "1000"


def host_gid():
    v = env("HOST_GID")
    if v:
        return v
    try:
        return str(os.getgid())
    except AttributeError:
        return "1000"


def run(cmd):
    """执行宿主命令，透传标准输入/输出/错误，返回退出码。"""
    return subprocess.call(cmd)


def docker_env_args(*names):
    """把指定宿主环境变量以 -e K=V 形式传给容器。"""
    out = []
    for n in names:
        v = env(n)
        if v is not None:
            out += ["-e", "{}={}".format(n, v)]
    return out


def ensure_volume(vol):
    """命名卷卷根默认属主是 root，先改成 plsdk(=1000) 才能写。

    只改卷根，不递归（卷内内容都是 plsdk 自己建的）；失败也不报错，交由后续
    命令自行暴露权限问题。
    """
    if not vol:
        return
    try:
        subprocess.call(
            [docker_bin(), "run", "--rm", "-u", "root",
             "-v", "{}:/t".format(vol),
             "--entrypoint", "chown", img_name(), "1000:1000", "/t"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def container_run_preamble(force_tty=False):
    """构造 docker run 的公共参数（挂载 sdk、仓库根、可选离线源等）。"""
    if is_windows() and not _vol_noted:
        vols = [v for v in (sdk_volume(), tmpdir_volume(), components_volume()) if v]
        if vols:
            log(">> [info] 自动使用 docker 卷: {}".format(", ".join(vols)))
        if components_tmpfs():
            log(">> [info] components 使用 tmpfs（每次启动展开 Yocto SDK，约 1 秒）")
        globals()["_vol_noted"] = True
    cmd = [docker_bin(), "run", "--rm", "-i"]
    if force_tty or want_tty():
        cmd.append("-t")
    cmd += network_args()
    cmd += ["-e", "TERM=xterm"]
    src = offline_src()
    if src is not None:
        cmd += ["-v", mount_volume(src, "/offline_src", read_only=True)]
    cmd += ["-v", sdk_mount()]
    cmd += ["-v", mount_volume(here(), "/work")]

    # 构建目录：Windows 原生(宿主=NTFS)必须把 TMPDIR 放卷里 —— Yocto 的 sanity 检查会因
    # "TMPDIR 在大小写不敏感的文件系统上"直接拒绝构建（sanity.bbclass 的
    # check_case_sensitive）。
    #
    # 注意 BUILD_VOLUME（整个 build/ 进卷）**不要用**：petalinux 写配置时会把临时文件
    # rename 到 project-spec/configs/config，临时文件在卷里、目标在 NTFS 树上 → 跨设备
    # rename 失败(EXDEV) → 报 "Error during writing of the configuration"，而且旧配置
    # 已经被改名成 config.old，工程配置就这么没了。所以 build/ 本体留在宿主，只把
    # build/tmp 放进卷。
    bvol = build_volume()
    tvol = tmpdir_volume()
    if bvol:
        log(">> [warn] BUILD_VOLUME={}：整个 build/ 进卷会让 petalinux 写配置时跨设备 "
            "rename 失败(EXDEV)，建议改用 TMPDIR_VOLUME(只挂 build/tmp)。".format(bvol))
    if tvol:
        ensure_volume(tvol)
        cmd += ["-v", "{}:/work/src/build/tmp".format(tvol)]
    elif bvol:
        ensure_volume(bvol)
        cmd += ["-v", "{}:/work/src/build".format(bvol)]

    # Yocto 层文件(src/components/yocto/layers, 1.3 万个文件)在 Windows 上要走
    # 9p 读，实测会把 bitbake 卡死在 p9_client_rpc（读字节数停在同一值不涨）。
    # 放进卷里就全在 VM 内 ext4 上读，既快又不会卡。
    #
    # tmpfs 模式：petalinux 展开 SDK 时安装器的 tar 要 chmod 目标根目录，9p 不支持
    # chmod（会报 "Cannot change mode ... Operation not permitted"），tmpfs 支持；
    # 展开只要约 1 秒，由 components_bootstrap() 在容器启动时做。
    if components_tmpfs():
        cmd += ["--tmpfs",
                "/work/src/components/yocto:rw,size=4g,exec,uid=1000,gid=1000,mode=755"]
    else:
        cvol = components_volume()
        if cvol:
            ensure_volume(cvol)
            cmd += ["-v", "{}:/work/src/components/yocto".format(cvol)]

    cmd += ["-w", "/work", "-u", "plsdk", img_name()]
    return cmd


def cmd_repair(args):
    """修复"编译被中途打断"留下的坏状态。

    典型症状（都是实测遇到的）：
      - ERROR: Error running gcc --version: /bin/sh: gcc: command not found
        （build/tmp/sysroots-uninative 里只剩断链，重新解压后缓存签名又全变）
      - FileNotFoundError / FileExistsError ...
        recipe-sysroot-native/installeddeps/xxx(.complete)

    原因：编译被 docker rm -f / 重启 Docker / 断电打断，派生目录写了一半。
    做法：删掉 build/tmp/sysroots-uninative 和所有 recipe-sysroot*，
          下次 petalinux-build 会从 sstate 重建（不是全量重编）。
    """
    dry = "--dry-run" in args
    paths = ("find \"$W\" -depth -type d "
             "\\( -name recipe-sysroot -o -name recipe-sysroot-native \\) "
             "-exec rm -rf {} + 2>/dev/null || true; ")
    script = (
        'set -e; '
        'U=/work/src/build/tmp/sysroots-uninative; '
        'W=/work/src/build/tmp/work; '
        'echo ">> uninative staging: $([ -e "$U" ] && echo 存在 || echo 不存在)"; '
        'n=$(find "$W" -depth -type d '
        '\\( -name recipe-sysroot -o -name recipe-sysroot-native \\) '
        '2>/dev/null | wc -l); '
        'echo ">> recipe-sysroot 目录: $n 个"; '
    )
    if dry:
        script += 'echo ">> --dry-run：未做任何删除"; '
    else:
        script += ('rm -rf "$U"; ' + paths +
                   'echo ">> 已清理，重新执行 python utils/devops.py petalinux-build 即可"')
    return run(container_run_preamble() + ["bash", "-c", script])


def cmd_shell():
    """无参：进入容器交互 shell（自动 source SDK 并进入 /work/src）。"""
    if not sdk_ready():
        log("[warn] {} 里没看到 petalinux/settings.sh，进容器后需要手动 source。"
            .format(sdk_desc()))
    cmd = container_run_preamble()
    pre = (components_bootstrap() +
           'source /opt/xilinx/petalinux/settings.sh /opt/xilinx/petalinux '
           '>/dev/null 2>&1 || echo "!! source /opt/xilinx/petalinux/settings.sh 失败"; '
           # PetaLinux 工程在 /work/src；停在 /work 会报
           # "You are not inside a PetaLinux project"，直接进来更省事
           'cd /work/src 2>/dev/null; '
           'echo ">> 已 source SDK 并进入 $(pwd)（可直接 petalinux-build）"; '
           'exec bash')
    return cmd + ["bash", "-c", pre]


YOCTO_CMDS = ("petalinux-build", "petalinux-config", "petalinux-package",
              "petalinux-bsp", "petalinux-kernel", "petalinux-rootfs")


def tmpdir_case_sensitive():
    """容器内探一下 TMPDIR 所在文件系统是否大小写敏感；探不了返回 None。

    Yocto 的 sanity.bbclass 会在 TMPDIR 里建 TmPxxxxx 再查它的全小写名是否存在，
    大小写不敏感（NTFS）会直接拒绝构建 —— 这里用同样的办法先探一下。
    """
    script = (
        "d=/work/src/build/tmp; mkdir -p \"$d\" 2>/dev/null || exit 2; "
        "rm -f \"$d/TmPcase\" \"$d/tmpcase\"; "
        "touch \"$d/TmPcase\" 2>/dev/null || exit 2; "
        "if [ -e \"$d/tmpcase\" ]; then echo INSENSITIVE; "
        "else echo SENSITIVE; fi; rm -f \"$d/TmPcase\""
    )
    try:
        out = subprocess.check_output(
            container_run_preamble() + ["bash", "-c", script],
            stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
    except Exception:
        return None
    if "INSENSITIVE" in out:
        return False
    if "SENSITIVE" in out:
        return True
    return None


def check_build_volume(args):
    """Windows 上跑 petalinux 命令前先探一次大小写，别让 Yocto 去撞墙。

    不探的后果：petalinux 会在 silentconfig 阶段失败，并把工程配置
    src/project-spec/configs/config 删掉（实测踩过）。
    """
    if not is_windows():
        return
    if not any(c in " ".join(args) for c in YOCTO_CMDS):
        return
    if build_volume() or tmpdir_volume():
        return                      # 已经在卷里了
    if tmpdir_case_sensitive() is not False:
        return
    fail(
        "Windows 原生编译必须把 build/tmp 放到 docker 命名卷。\n"
        "  当前 build/ 在 NTFS 上（大小写不敏感），Yocto 会拒绝构建；实测\n"
        "  petalinux 会在 silentconfig 阶段失败，并删掉工程配置\n"
        "  src/project-spec/configs/config。\n"
        "    $env:TMPDIR_VOLUME=\"petalinux-tmp\"    # 只挂 build/tmp\n"
        "  另外强烈建议把 Yocto 层文件也放进卷（否则 bitbake 解析层文件会被 9p 卡死）：\n"
        "    $env:COMPONENTS_VOLUME=\"petalinux-components\"\n"
        "  （Linux/WSL 不需要：ext4 本来就大小写敏感）")


def cleanup_stale_containers():
    """清掉上次构建残留的容器。

    bitbake 的 cooker server 在构建失败时不会退出，容器就一直挂着，并占着
    bitbake 锁（bitbake.lock 里记的是**容器内**的 PID；下次构建遇到同号 PID
    会误判"锁被占用"而连不上 server，构建在 silentconfig 阶段就挂掉）。
    """
    try:
        out = subprocess.check_output(
            [docker_bin(), "ps", "--format", "{{.ID}} {{.Command}}",
             "--filter", "ancestor={}".format(img_name())],
            stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
    except Exception:
        return
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        cid, cmd = parts[0], parts[1]
        # 只清我们自己的构建容器，别动用户手开的 petalinux 容器
        if "petalinux/settings.sh" not in cmd:
            continue
        log(">> [warn] 清理上次残留的构建容器 {}（bitbake server 没退出）".format(cid))
        subprocess.call([docker_bin(), "rm", "-f", cid],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def cmd_run(args):
    """带参：容器内 source settings.sh 并 cd 到工程目录后执行命令。"""
    if not args:
        fail("用法: {} <命令...>".format(sys.argv[0]))
    if not sdk_ready():
        fail(
            "未找到 PetaLinux SDK（{}）。\n"
            "  sdk 不在 git 里，换机/重新 clone 后必须重做一次：\n"
            "    1) 下载 petalinux-v2021.1-final-installer.run\n"
            "    2) 放到 docker/archives/ 下（或设置 PETALINUX_INSTALLER=<路径>）\n"
            "    3) {} install-sdk\n"
            "  若 SDK 已装在 WSL 的 ext4 里、想让 Windows 原生也用它（免去重新下载）：\n"
            "    a) 在 WSL 里：python3 utils/devops.py sdk-to-volume   # 灌进 docker 卷 petalinux-sdk\n"
            "    b) 两边都设 SDK_VOLUME=petalinux-sdk 再跑本命令\n"
            .format(sdk_desc(), sys.argv[0]))
    if any(c in " ".join(args) for c in YOCTO_CMDS):
        check_build_volume(args)
        cleanup_stale_containers()
    proj = env("PETALINUX_PROJ") or "/work/src"
    bash_c = (components_bootstrap() +
              'source /opt/xilinx/petalinux/settings.sh /opt/xilinx/petalinux '
              '&& cd "$1" && shift && '
              # 上次异常退出留下的锁/socket 会让 bitbake 连不上 server，先清掉
              'rm -f "{p}/build/bitbake.sock" "{p}/build/bitbake.lock" '
              '"{p}/build/hashserve.sock" ; '
              'exec "$@"').format(p=proj)
    return container_run_preamble() + ["bash", "-c", bash_c, "plsdk", proj] + list(args)


def cmd_build_image():
    uid = host_uid()
    gid = host_gid()
    log(">> 构建镜像: {}  (UID={}, GID={})".format(img_name(), uid, gid))
    cmd = ([docker_bin(), "build"] + network_args() +
           ["--build-arg", "HOST_USER_UID=" + uid,
            "--build-arg", "HOST_USER_GID=" + gid,
            "-t", img_name(), str(here() / "docker")])
    return run(cmd)


def find_installer():
    cands = []
    v = env("PETALINUX_INSTALLER")
    if v:
        cands.append(Path(v))
    cands.append(sdk_host_dir() / "downloads" / INSTALLER_NAME)
    cands.append(here() / "docker" / "archives" / INSTALLER_NAME)
    cands.append(here() / "downloads" / INSTALLER_NAME)
    cands.append(Path("/mnt/c/Users/a1an1in/Downloads/baidu") / INSTALLER_NAME)
    for c in cands:
        if c.is_file():
            return c
    return None


def dir_size(p):
    if not shutil.which("du"):
        return None
    try:
        out = subprocess.check_output(["du", "-sh", str(p)],
                                      stderr=subprocess.DEVNULL).decode()
        return out.split("\t")[0]
    except Exception:
        return None


def cmd_install_sdk(args):
    interactive = "--interactive" in args
    installer = find_installer()
    if installer is None:
        fail("未找到安装器。请设置 PETALINUX_INSTALLER=<路径> 后重试。")
    log(">> 安装器: {}".format(installer))

    vol = sdk_volume()
    if vol:
        # 卷模式：SDK 直接装进卷（卷由镜像里的 /opt/xilinx 初始化，属主 plsdk）
        ensure_volume(vol)
        log(">> SDK 目标: docker 卷 {}:/opt/xilinx（不写宿主 sdk/ 目录）".format(vol))
    else:
        for sub in ("petalinux", "downloads", "sstate"):
            (sdk_host_dir() / sub).mkdir(parents=True, exist_ok=True)
    (here() / "src").mkdir(parents=True, exist_ok=True)

    inst_dir = installer.parent
    inst_name = installer.name
    install_target = "/opt/xilinx/petalinux"

    if interactive:
        log(">> 交互模式启动 SDK 安装器 ...（请在本终端操作）")
        log(">> 到 LICENSE AGREEMENTS 时按 q 关闭、y 接受，确认安装目录 "
            + install_target)
        cmd = ([docker_bin(), "run", "--rm", "-it"] + network_args() +
               ["-v", mount_volume(inst_dir, "/installer", read_only=True),
                sdk_mount(),
                "-u", "plsdk", "--workdir", "/tmp", img_name(),
                "/installer/" + inst_name, "--dir", install_target])
        return run(cmd)

    log(">> SDK 安装中（自动应答许可，实时打印进度心跳）... SDK 将写入: "
        + sdk_desc())
    cmd = ([docker_bin(), "run", "--rm", "-t"] + network_args() +
           docker_env_args("SAP_EXP_TTY", "SAP_EXP_LOG") +
           ["-v", mount_volume(inst_dir, "/installer", read_only=True),
            "-v", sdk_mount(),
            "-v", mount_volume(here() / "docker" / "scripts", "/scripts",
                                read_only=True),
            "-u", "plsdk", "--workdir", "/tmp", img_name(),
            "expect", "/scripts/auto-install.sh",
            "/installer/" + inst_name, install_target])
    rc = run(cmd)
    if rc != 0:
        return rc

    if not sdk_ready():
        fail("!! 安装后在 {} 里仍未发现 petalinux/settings.sh，安装可能未完成。"
             "用 SAP_EXP_TTY=1 重跑可看安装器原始输出。".format(sdk_desc()))

    log(">> SDK 安装完成:")
    log("    位置: {}".format(sdk_desc()))
    if not sdk_volume():
        size = dir_size(sdk_host_dir() / "petalinux")
        if size:
            log("    大小:   {}".format(size))
    log(">> 下一步: {} unpack-offline 解压离线包，然后 {} 进入工程开发"
        .format(sys.argv[0], sys.argv[0]))
    return 0
def archives_dir():
    cands = []
    cands.append(Path("/mnt/c/Users/a1an1in/softs/xlinx"))
    cands.append(here() / "docker" / "archives")
    cands.append(Path("/mnt/c/Users/a1an1in/Downloads/baidu"))
    for c in cands:
        if c.is_dir():
            return c
    return cands[0]


def extract_targz(tgz, dest, strip=None):
    tgz = Path(tgz)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if not tgz.is_file():
        fail("缺少压缩包: {}".format(tgz))
    if shutil.which("tar"):
        cmd = ["tar", "-xzf", str(tgz), "-C", str(dest)]
        if strip is not None:
            cmd += ["--strip-components", str(strip)]
        subprocess.check_call(cmd)
    else:
        _extract_targz_python(tgz, dest, strip or 0)


def _extract_targz_python(tgz, dest, strip):
    def norm_parts(name):
        return [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]

    with tarfile.open(str(tgz), "r:gz") as tf:
        for member in tf.getmembers():
            parts = norm_parts(member.name)
            if strip:
                parts = parts[strip:]
            if not parts or ".." in parts:
                continue
            member.name = "/".join(parts)
            try:
                tf.extract(member, str(dest))
            except Exception as e:
                log("    [warn] 跳过 {}: {}".format(member.name, e))


def _unpack_offline_in_container(downloads_tgz, sstate_tgz):
    """SDK 在命名卷里时，解压必须在容器内做（宿主看不到卷内容）。"""
    vol = sdk_volume()
    for tgz in (downloads_tgz, sstate_tgz):
        if not tgz.is_file():
            fail("缺少压缩包: {}".format(tgz))

    mounts = []
    refs = []
    for i, tgz in enumerate((downloads_tgz, sstate_tgz)):
        mounts += ["-v", mount_volume(tgz.parent, "/pkg{}".format(i), read_only=True)]
        refs.append("/pkg{}/{}".format(i, tgz.name))

    script = (
        "set -e; "
        "mkdir -p /opt/xilinx/downloads; "
        "echo '==> 解压 downloads ...'; "
        "tar -xzf {dl} -C /opt/xilinx/downloads --strip-components=1; "
        "echo '==> 解压 sstate ...'; "
        "tar -xzf {ss} -C /opt/xilinx; "
        "if [ -d /opt/xilinx/sstate_arm_2021.1/arm ]; then "
        "  rm -rf /opt/xilinx/sstate/arm; mkdir -p /opt/xilinx/sstate; "
        "  mv /opt/xilinx/sstate_arm_2021.1/arm /opt/xilinx/sstate/arm; "
        "  rmdir /opt/xilinx/sstate_arm_2021.1 2>/dev/null || true; "
        "else echo '警告: 未在预期位置找到 sstate/arm, 请检查包结构。'; fi; "
        "du -sh /opt/xilinx/downloads /opt/xilinx/sstate"
    ).format(dl=refs[0], ss=refs[1])

    log(">> SDK 在 {} 里，解压改为容器内执行".format(sdk_desc()))
    cmd = ([docker_bin(), "run", "--rm", "-t"] + network_args() +
           ["-v", "{}:/opt/xilinx".format(vol)] + mounts +
           ["-u", "plsdk", img_name(), "sh", "-c", script])
    return run(cmd)


def cmd_unpack_offline():
    arch = archives_dir()
    downloads_tgz = Path(env("DOWNLOADS_TGZ") or str(arch / DOWNLOADS_NAME))
    sstate_tgz = Path(env("SSTATEGZ") or str(arch / SSTATE_NAME))

    if sdk_volume():
        return _unpack_offline_in_container(downloads_tgz, sstate_tgz)

    downloads_dst = sdk_host_dir() / "downloads"
    sstate_dst = sdk_host_dir() / "sstate"

    log("==> 解压 downloads ...")
    log("    源: {}".format(downloads_tgz))
    extract_targz(downloads_tgz, downloads_dst, strip=1)

    log("==> 解压 sstate ...")
    log("    源: {}".format(sstate_tgz))
    extract_targz(sstate_tgz, sdk_host_dir(), strip=None)

    sstate_top = sdk_host_dir() / "sstate_arm_2021.1"
    if (sstate_top / "arm").is_dir():
        sstate_dst.mkdir(parents=True, exist_ok=True)
        target = sstate_dst / "arm"
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(sstate_top / "arm"), str(target))
        try:
            sstate_top.rmdir()
        except OSError:
            pass
    else:
        log("警告: 未在预期位置找到 sstate/arm, 请检查包结构。")

    log("==> 离线包就位:")
    log("    downloads: {}".format(downloads_dst))
    log("    sstate   : {}".format(sstate_dst))
    log("==> 在 petalinux-config 的 Yocto Settings 中设置:")
    log("    pre-mirror url      = file:///opt/xilinx/downloads")
    log("    local sstate feeds  = /opt/xilinx/sstate  /arm")
    log("    并取消勾选 Enable Network sstate feeds")
    return 0


def cmd_sdk_to_volume(args):
    """把装好的宿主 sdk/ 目录灌进 docker 命名卷（供 Windows 与 WSL 共用）。

    典型场景：SDK 装在 WSL 的 ext4 里（如 /home/<user>/.../sdk），Windows 原生
    docker 够不到那个路径（\\wsl$ 不是合法挂载源），于是搬进命名卷 —— 卷在 WSL2
    VM 的 ext4 内，两个客户端都能挂。

    注：源在 WSL 的 ext4 时，走 bind mount 会让 docker daemon 走 9p 读（很慢）。
    想快就从 WSL 里用管道流式灌：
        tar cf - -C <sdk目录> . | docker run -i -v <卷>:/v <镜像> tar xf - -C /v
    """
    src = None
    vol = None
    i = 0
    while i < len(args):
        if args[i] == "--from" and i + 1 < len(args):
            src = Path(args[i + 1]).expanduser()
            i += 2
            continue
        if args[i] == "--to" and i + 1 < len(args):
            vol = args[i + 1]
            i += 2
            continue
        fail("未知参数: {}\n用法: {} sdk-to-volume [--from <宿主sdk目录>] [--to <卷名>]"
             .format(args[i], sys.argv[0]))

    src = src or sdk_host_dir()
    vol = vol or sdk_volume() or SDK_VOLUME_DEFAULT

    if not (src / "petalinux" / "settings.sh").is_file():
        fail("{} 里没有 petalinux/settings.sh，先确认这是装好 SDK 的目录".format(src))

    log(">> 把 {} 灌进 docker 卷 {} ...".format(src, vol))
    log("   源挂 /src:ro，卷挂 /opt/xilinx（卷由镜像目录初始化，属主 plsdk，可直接写）")
    cmd = ([docker_bin(), "run", "--rm", "-u", "root"] + network_args() +
           ["-v", mount_volume(src, "/src", read_only=True),
            "-v", "{}:/opt/xilinx".format(vol),
            img_name(), "sh", "-c",
            "cd /src && tar cf - . | tar xf - -C /opt/xilinx "
            "&& chown -R 1000:1000 /opt/xilinx "
            "&& echo '>> 拷贝完成' && du -sh /opt/xilinx"])
    rc = run(cmd)
    if rc == 0:
        log(">> 完成。之后带 SDK_VOLUME={} 使用：".format(vol))
        log("     Linux/WSL: SDK_VOLUME={} ./utils/devops.py petalinux-build".format(vol))
        log("     Windows:   $env:SDK_VOLUME=\"{}\"; python utils\\devops.py petalinux-build"
            .format(vol))
        log("   确认无误后可删掉宿主那份 sdk/ 释放空间（卷里那份才是唯一真相）。")
    return rc


def cmd_build_to_volume(args):
    """把宿主 <仓库>/src/build 灌进 docker 命名卷（Windows 原生编译用）。

    整个 build/ 进卷后：conf/cache/downloads/sstate-cache/tmp 全在 VM 内 ext4 上，
    Win 与 WSL 共用同一份增量状态；--tmp-from 可把旧 TMPDIR_VOLUME 卷里的增量状态
    合并成 <卷>/tmp，避免从头再编一遍。
    """
    src = None
    vol = None
    tmp_vol = None
    i = 0
    while i < len(args):
        if args[i] == "--from" and i + 1 < len(args):
            src = Path(args[i + 1]).expanduser()
            i += 2
            continue
        if args[i] == "--to" and i + 1 < len(args):
            vol = args[i + 1]
            i += 2
            continue
        if args[i] == "--tmp-from" and i + 1 < len(args):
            tmp_vol = args[i + 1]
            i += 2
            continue
        fail("未知参数: {}\n用法: {} build-to-volume [--from <宿主build目录>] "
             "[--to <卷名>] [--tmp-from <旧TMPDIR卷名>]".format(args[i], sys.argv[0]))

    src = src or (here() / "src" / "build")
    vol = vol or build_volume() or BUILD_VOLUME_DEFAULT

    log(">> 把 {} 灌进 docker 卷 {} ...".format(src, vol))
    mounts = ["-v", "{}:/to".format(vol)]
    if src.is_dir():
        mounts += ["-v", mount_volume(src, "/from", read_only=True)]
    else:
        log("   [warn] 宿主 {} 不存在，只迁移 tmp".format(src))
    if tmp_vol:
        mounts += ["-v", "{}:/fromtmp:ro".format(tmp_vol)]
        log("   旧 TMPDIR 卷 {} 的内容合并成 {}/tmp".format(tmp_vol, vol))

    script = (
        "set -e; "
        "mkdir -p /to; "
        "if [ -d /from ]; then "
        "  for d in conf cache downloads sstate-cache misc; do "
        "    if [ -d /from/$d ]; then echo \"==> 拷贝 $d\"; cp -a /from/$d /to/; fi; "
        "  done; "
        "fi; "
        "if [ -d /fromtmp ]; then "
        "  echo '==> 拷贝 tmp（来自旧 TMPDIR 卷）'; mkdir -p /to/tmp; "
        "  cp -a /fromtmp/. /to/tmp/; "
        "elif [ -d /from/tmp ]; then "
        "  echo '==> 拷贝 tmp（来自宿主 build/tmp）'; rm -rf /to/tmp; cp -a /from/tmp /to/; "
        "else "
        "  mkdir -p /to/tmp; "
        "fi; "
        "chown -R 1000:1000 /to; "
        "echo '>> 拷贝完成'; du -sh /to/*"
    )
    cmd = ([docker_bin(), "run", "--rm", "-u", "root"] + network_args() +
           mounts + [img_name(), "sh", "-c", script])
    rc = run(cmd)
    if rc == 0:
        log(">> 完成。之后带 BUILD_VOLUME={} 使用：".format(vol))
        log("     Linux/WSL: BUILD_VOLUME={} ./utils/devops.py petalinux-build".format(vol))
        log("     Windows:   $env:BUILD_VOLUME=\"{}\"; python utils\\devops.py petalinux-build"
            .format(vol))
    return rc


def main(argv):
    args = list(argv)
    if not args:
        return run(cmd_shell())
    first = args[0]
    if first in ("help", "-h", "--help"):
        print(HELP)
        return 0
    if first == "build-image":
        return cmd_build_image()
    if first == "install-sdk":
        return cmd_install_sdk(args[1:])
    if first == "unpack-offline":
        return cmd_unpack_offline()
    if first in ("sdk-to-volume", "sdk2vol"):
        return cmd_sdk_to_volume(args[1:])
    if first in ("build-to-volume", "build2vol"):
        return cmd_build_to_volume(args[1:])
    if first == "repair":
        return cmd_repair(args[1:])
    if first in ("shell", "sh"):
        return run(cmd_shell())
    if first in ("run", "exec"):
        if len(args) < 2:
            print(HELP, file=sys.stderr)
            return 2
        return run(cmd_run(args[1:]))
    # 默认：把整条参数原样丢进容器执行（与旧 devops.sh 带参行为一致）
    return run(cmd_run(args))


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc if isinstance(rc, int) else 0)