#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PetaLinux 2021.1 Docker 构建入口（Linux / WSL）。

把原来分散的 shell 脚本（build-image.sh / install-sdk.sh /
unpack-offline.sh / devops.sh）合并为一个 Python 入口，编译仍全部在 Docker 里
执行，宿主只负责拼出 docker 触发命令（--network host + bind mount）。

用法：
    python3 utils/devops.py                        # 交互 shell，/work 为仓库根目录
    python3 utils/devops.py petalinux-build        # 容器内 source settings.sh 后执行命令
    python3 utils/devops.py petalinux-package --boot --u-boot --fpga --force
    python3 utils/devops.py build-image            # 构建 docker 镜像
    python3 utils/devops.py install-sdk [--interactive]   # 安装 SDK 到 sdk/
    python3 utils/devops.py unpack-offline         # 解压 downloads/sstate 离线包
    python3 utils/devops.py help

SDK 放在宿主的 <仓库>/sdk 目录（bind mount 到容器 /opt/xilinx）。

可配环境变量：
    IMG_NAME            默认 petalinux:2021.1
    HOST_UID/HOST_GID   构建镜像时的宿主 UID/GID（默认自动探测）
    PETALINUX_INSTALLER 显式指定安装器路径
    DOWNLOADS_TGZ/SSTATEGZ  显式指定离线包压缩包路径
    OFFLINE_SRC         容器内 /offline_src 的只读来源目录
    PETALINUX_PROJ      容器内工程目录，默认 /work/src
    DOCKER_NETWORK      强制指定网络模式（默认 --network host）
    SDK_DIR             SDK 宿主目录覆盖（默认 <仓库>/sdk）
"""

import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

IMG_NAME_DEFAULT = "petalinux:2021.1"
INSTALLER_NAME = "petalinux-v2021.1-final-installer.run"
DOWNLOADS_NAME = "downloads_2021.1_update1.tar.gz"
SSTATE_NAME = "sstate_arm_2021.1.tar.gz"

HELP = """\
PetaLinux 2021.1 Docker 构建入口（Linux / WSL）

用法：
  python3 utils/devops.py                       交互 shell（/work 为仓库根目录）
  python3 utils/devops.py <命令...>             容器内 source settings.sh 后执行该命令
  python3 utils/devops.py build-image           构建 Docker 镜像（只含 OS+依赖）
  python3 utils/devops.py install-sdk           用 auto-install.sh 安装 SDK 到 sdk/
  python3 utils/devops.py install-sdk --interactive   交互式运行真实安装器
  python3 utils/devops.py unpack-offline        解压 downloads/sstate 离线包到 sdk/
  python3 utils/devops.py help                  显示本帮助

示例：
  python3 utils/devops.py petalinux-build
  python3 utils/devops.py petalinux-package --boot --u-boot --fpga --force

环境变量：IMG_NAME、HOST_UID、HOST_GID、PETALINUX_INSTALLER、DOWNLOADS_TGZ、
          SSTATEGZ、OFFLINE_SRC、PETALINUX_PROJ、DOCKER_NETWORK、SDK_DIR
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


def network_args():
    """docker run/build 的网络模式参数：默认 --network host，可用 DOCKER_NETWORK 覆盖。"""
    override = env("DOCKER_NETWORK")
    if override:
        return ["--network", override]
    return ["--network", "host"]


def mount_volume(host, container, read_only=False):
    suffix = ":ro" if read_only else ""
    return "{}:{}{}".format(str(host), container, suffix)


def sdk_host_dir():
    """SDK 在宿主的目录；可用 SDK_DIR 覆盖（默认 <仓库>/sdk）。"""
    v = env("SDK_DIR")
    return Path(v).expanduser() if v else sdk_dir()


def sdk_mount():
    return mount_volume(sdk_host_dir(), "/opt/xilinx")


def sdk_desc():
    return str(sdk_host_dir())


def sdk_ready():
    """SDK 是否可用：宿主目录里有没有 petalinux/settings.sh。"""
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


def container_run_preamble(force_tty=False):
    """构造 docker run 的公共参数（挂载 sdk、仓库根、可选离线源等）。"""
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
    cmd += ["-w", "/work", "-u", "plsdk", img_name()]
    return cmd


def cmd_shell():
    """无参：进入容器交互 shell。"""
    if not sdk_ready():
        log("[warn] {} 里没看到 petalinux/settings.sh，进容器后需要手动 source。"
            .format(sdk_desc()))
    return container_run_preamble()


YOCTO_CMDS = ("petalinux-build", "petalinux-config", "petalinux-package",
              "petalinux-bsp", "petalinux-kernel", "petalinux-rootfs")


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
            .format(sdk_desc(), sys.argv[0]))
    if any(c in " ".join(args) for c in YOCTO_CMDS):
        cleanup_stale_containers()
    proj = env("PETALINUX_PROJ") or "/work/src"
    bash_c = ('source /opt/xilinx/petalinux/settings.sh /opt/xilinx/petalinux '
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


def cmd_unpack_offline():
    arch = archives_dir()
    downloads_tgz = Path(env("DOWNLOADS_TGZ") or str(arch / DOWNLOADS_NAME))
    sstate_tgz = Path(env("SSTATEGZ") or str(arch / SSTATE_NAME))

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