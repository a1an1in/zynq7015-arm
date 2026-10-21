#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SD 卡烧写工具(子命令化，Python 版)。

子命令:
  list | detect   识别当前 SD 卡(WSL 可移动盘 + Windows usbipd 读卡器状态)
  release         归还 SD 卡给 Windows(usbipd detach;会弹 UAC,请点允许)
  part            只做分区 + 格式化
  mkfs|format     仅重新格式化已有分区(不重写分区表,自动卸载残留挂载)
  flash  [源]     刷包;若 SD 卡尚未分区则先自动分区再刷包(不传源时默认官方出厂包)
  deploy <选项> <源目录> <root@板IP> <设备>
                   SSH 登板(用户/密码 root/root),把本地源同步到板上 SD 分区。
                   <选项>二选一: -b|--boot 只同步 boot(FAT); -f|--fs 增量+镜像删除同步文件系统
  probe <root@板IP|板IP>   SSH 登板摸底/验证 SIMPLE 模式

源(SOURCE): 目录路径;省略时默认官方出厂设置包。刷本工程产物传 <工程>/src/images/linux。

用法例:
  utils/flash-sd.py help
  utils/flash-sd.py list
  utils/flash-sd.py part
  utils/flash-sd.py flash ./src/images/linux
  utils/flash-sd.py deploy -b ./src/images/linux root@192.168.1.100 mmcblk0
  utils/flash-sd.py deploy -f ./nfs/rootfs root@192.168.1.100 /dev/mmcblk0
  utils/flash-sd.py probe root@192.168.1.100
  utils/flash-sd.py release

说明: 本工具依赖 Linux 工具链(lsblk/parted/mount/rsync/sshpass)与 WSL 的 usbipd,
      仅支持 Linux / WSL。
"""

import fnmatch
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FACTORY_SRC = ("/mnt/c/Users/a1an1in/workspace/xlinx/F6_7020/V2022/"
               "01_user_start/02_start_linux/03_restore_factory/"
               "rst_to_factory_img/rst_to_factory_img/sdcard_image")
SELF_SRC = str(HERE / "src" / "images" / "linux")   # 本工程 PetaLinux 产物
PSWIN = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

SSHOPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=8"]
SSH_E = "sshpass -p root ssh " + " ".join(SSHOPTS)


def _is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


_SUDO = [] if _is_root() else ["sudo"]


def run(argv, *, sudo=False, capture=False, input_text=None, check=False):
    cmd = list(argv)
    if sudo:
        cmd = _SUDO + cmd
    kwargs = {}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
    if input_text is not None:
        kwargs["input"] = input_text
    p = subprocess.run(cmd, text=True, **kwargs)
    if check and p.returncode != 0:
        sys.exit(p.returncode if p.returncode else 1)
    if capture:
        return p.returncode, p.stdout
    return p.returncode


def capture(argv, *, sudo=False):
    return run(argv, sudo=sudo, capture=True)


def sudo_ok():
    if not _SUDO:
        return True
    return subprocess.run(["sudo", "-v"]).returncode == 0


def is_block(p):
    try:
        return stat.S_ISBLK(os.stat(p).st_mode)
    except OSError:
        return False


def device_size(p):
    rc, out = capture(["lsblk", "-dno", "SIZE", p])
    return out.strip() if rc == 0 and out.strip() else "?"


def human_size(p):
    rc, out = capture(["du", "-h", str(p)])
    if rc == 0 and out:
        return out.split("\t")[0].strip()
    return "?"


def usage():
    print(__doc__.rstrip())


def host_of(dst):
    return dst.split("@", 1)[1] if "@" in dst else dst


def rsh(ip, remote_cmd, capture_output=True):
    return run(["sshpass", "-p", "root", "ssh"] + SSHOPTS +
               ["root@" + ip, remote_cmd], capture=capture_output)


def ensure_tools():
    missing = [t for t in ("parted", "mkfs.vfat", "rsync", "tar", "mount")
               if shutil.which(t) is None]
    if missing:
        print(">> 缺工具: {} ,安装 parted dosfstools rsync tar ...".format(" ".join(missing)))
        run(["apt-get", "update", "-qq"], sudo=True, check=True)
        run(["apt-get", "install", "-y", "parted", "dosfstools", "rsync", "tar"],
            sudo=True, check=True)
    if shutil.which("sshpass") is None:
        print(">> 缺 sshpass(deploy 用),安装 ...")
        run(["apt-get", "install", "-y", "sshpass"], sudo=True, check=True)


def _removable_devs():
    rc, out = capture(["lsblk", "-dno", "NAME,SIZE,RM,TYPE,MODEL"])
    devs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "1" and parts[0] != "NAME":
            devs.append("/dev/" + parts[0])
    return devs


def pick_dev():
    print("WSL 当前可见可移动盘(应见 ~29G 的那块):", file=sys.stderr)
    rc, out = capture(["lsblk", "-dno", "NAME,SIZE,RM,TYPE,MODEL"])
    for line in out.splitlines():
        parts = line.split()
        if parts and (parts[0] == "NAME" or (len(parts) >= 3 and parts[2] == "1")):
            print(line, file=sys.stderr)

    devs = _removable_devs()
    while True:
        print("输入要操作的 SD 卡设备(如 /dev/sdg)并回车: ", end="", file=sys.stderr, flush=True)
        raw = sys.stdin.readline()
        if raw == "":   # 无 stdin(EOF)
            if len(devs) == 1:
                print("[自动] 仅检测到一块可移动盘,选中 {}".format(devs[0]), file=sys.stderr)
                return devs[0]
            print("[错误] 检测到 {} 块可移动盘,但无输入,无法自动选择".format(len(devs)), file=sys.stderr)
            return None
        dev = raw.strip()
        if not dev:
            if len(devs) == 1:
                print("[自动] 仅检测到一块可移动盘,选中 {}".format(devs[0]), file=sys.stderr)
                return devs[0]
            print("[提示] 输入为空,请重新输入(检测到 {} 块可移动盘)".format(len(devs)), file=sys.stderr)
            continue
        if not is_block(dev):
            print("[错误] {} 不是块设备".format(dev), file=sys.stderr)
            continue
        return dev


def resolve_src(s=None):
    s = s or FACTORY_SRC
    if os.path.isdir(s):
        return s
    print("!目录不存在: {}".format(s), file=sys.stderr)
    return None


def do_part(dev):
    print("!!!! 即将把 {} ({}) 整卡抹掉重分区 !!!!".format(dev, device_size(dev)))
    yy = input("确认请输入大写 YES 继续: ").strip()
    if yy != "YES":
        print("已取消")
        return 0
    if not sudo_ok():
        print("[错误] sudo 认证失败")
        return 1
    run(["sync"])
    rc, out = capture(["lsblk", dev, "-o", "mountpoint", "--noheadings"])
    for m in out.splitlines():
        m = m.strip()
        if m:
            run(["umount", m], sudo=True)
    rc, out = capture(["lsblk", "-l", dev])
    for line in out.splitlines():
        parts = line.split()
        if "part" in parts:
            run(["swapoff", "/dev/" + parts[0]], sudo=True)
    print("[分区] fat32(BOOT,1-100MiB,可引导) + ext4(rootfs,到盘尾)...")
    run(["parted", "-s", dev, "mklabel", "msdos"], sudo=True, check=True)
    run(["parted", "-s", dev, "mkpart", "primary", "fat32", "1MiB", "100MiB"], sudo=True, check=True)
    run(["parted", "-s", dev, "set", "1", "boot", "on"], sudo=True, check=True)
    run(["parted", "-s", dev, "mkpart", "primary", "ext4", "100MiB", "100%"], sudo=True, check=True)
    if run(["partprobe", dev], sudo=True) != 0:
        run(["partx", "-u", dev], sudo=True)
    time.sleep(2)
    run(["mkfs.vfat", "-F", "32", "-n", "BOOT", dev + "1"], sudo=True, check=True)
    run(["mkfs.ext4", "-L", "rootfs", dev + "2"], sudo=True, input_text="y\n", check=True)
    print(">> 分区完成:")
    rc, out = capture(["lsblk", "-o", "NAME,SIZE,FSTYPE,LABEL", dev])
    print(out, end="")
    return 0


def do_mkfs(dev):
    print("即将重新格式化 {}1(BOOT/vfat) 与 {}2(rootfs/ext4),保留分区表不重写。".format(dev, dev))
    print("当前布局:")
    rc, out = capture(["lsblk", "-o", "NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT", dev])
    for line in out.splitlines():
        if line.strip():
            print(line)
    yy = input("确认请输入大写 YES 继续: ").strip()
    if yy != "YES":
        print("已取消")
        return 0
    if not sudo_ok():
        print("[错误] sudo 认证失败")
        return 1
    rc, out = capture(["lsblk", dev, "-o", "mountpoint", "--noheadings"])
    for m in out.splitlines():
        m = m.strip()
        if m:
            run(["umount", m], sudo=True)
    run(["mkfs.vfat", "-F", "32", "-n", "BOOT", dev + "1"], sudo=True, check=True)
    run(["mkfs.ext4", "-L", "rootfs", dev + "2"], sudo=True, input_text="y\n", check=True)
    print(">> 格式化完成:")
    rc, out = capture(["lsblk", "-o", "NAME,SIZE,FSTYPE,LABEL", dev])
    print(out, end="")
    return 0


BOOT_EXCLUDES = ["--exclude=rootfs*", "--exclude=pxelinux*", "--exclude=vmlinux*",
                 "--exclude=*.cpio*", "--exclude=*.jffs2", "--exclude=*.ext4",
                 "--exclude=*.manifest", "--exclude=config", "--exclude=*.elf"]


def do_flash(dev, src):
    if os.path.isdir(os.path.join(src, "boot")):
        bootdir = os.path.join(src, "boot")
    elif os.path.isfile(os.path.join(src, "BOOT.BIN")) or os.path.isfile(os.path.join(src, "BOOT.bin")):
        bootdir = src
    else:
        print("! 源里找不到启动文件({}/boot 或 BOOT.bin)".format(src), file=sys.stderr)
        return 1
    rootfs_tgz = None
    if os.path.isfile(os.path.join(src, "rootfs", "rootfs.tar.gz")):
        rootfs_tgz = os.path.join(src, "rootfs", "rootfs.tar.gz")
    elif os.path.isfile(os.path.join(src, "rootfs.tar.gz")):
        rootfs_tgz = os.path.join(src, "rootfs.tar.gz")
    if not sudo_ok():
        print("[错误] sudo 认证失败")
        return 1

    print(">> 刷 boot({}) 到 {}1 ...".format(bootdir, dev))
    m1 = tempfile.mkdtemp()
    run(["mount", "-t", "vfat", dev + "1", m1], sudo=True, check=True)
    try:
        if bootdir == src:
            print(">> 工程源,已排除非启动文件(rootfs*, pxelinux*, vmlinux*, *.cpio, *.ext4, *.jffs2, *.manifest, config, *.elf):")
            cmd = ["rsync", "-rv"] + BOOT_EXCLUDES + [bootdir + "/", m1 + "/"]
        else:
            cmd = ["rsync", "-rv", bootdir + "/", m1 + "/"]
        run(cmd, sudo=True, check=True)
    finally:
        run(["sync"], sudo=True)
        run(["umount", m1], sudo=True)
        shutil.rmtree(m1, ignore_errors=True)

    if rootfs_tgz:
        print(">> 解压 rootfs.tar.gz({},从 C 盘读取较慢)到 {}2 ...".format(human_size(rootfs_tgz), dev))
        m2 = tempfile.mkdtemp()
        run(["mount", "-t", "ext4", dev + "2", m2], sudo=True, check=True)
        ok = True
        try:
            if shutil.which("pv"):
                print("   (带进度条,pv 显示;走 /mnt/c 可能慢,请等到 100% 后出现 '刷包完成')")
                run(["sh", "-c", "pv -f '{}' | tar zx -C '{}'".format(rootfs_tgz, m2)],
                    sudo=True, check=True)
            else:
                print("   (无 pv,无进度条;正在解压 420M 级文件,请不要中断,完成后打印字节总数)")
                if run(["tar", "--totals", "-zxf", rootfs_tgz, "-C", m2], sudo=True) != 0:
                    print("[!] rootfs 解压失败", file=sys.stderr)
                    ok = False
        finally:
            run(["sync"], sudo=True)
            run(["umount", m2], sudo=True)
            shutil.rmtree(m2, ignore_errors=True)
        if not ok:
            return 1
    else:
        print(">> 无 rootfs.tar.gz,ext4 分区留空(工程若用 image.ub 内 ramdisk 则正常)。")
    run(["sync"], sudo=True)
    print(">> 刷包完成。")
    return 0


def detect():
    print("== WSL 可见可移动盘 ==")
    rc, out = capture(["lsblk", "-dno", "NAME,SIZE,RM,TYPE,MODEL"])
    for line in out.splitlines():
        parts = line.split()
        if parts and (parts[0] == "NAME" or (len(parts) >= 3 and parts[2] == "1")):
            print(line)
    print()
    if os.access(PSWIN, os.X_OK):
        print("== Windows usbipd 读卡器状态(SD/读卡器相关行) ==")
        rc, out = capture([PSWIN, "-NoProfile", "-Command", "usbipd list"])
        matched = [l for l in out.splitlines() if re.search(r"BUS|14cd|SD|USB", l, re.I)]
        for l in matched[:10]:
            print(l)
        if rc != 0 or not matched:
            print("(无法读取 usbipd,可在管理员 PowerShell 手动运行 usbipd list)")
    return 0


def release():
    if not os.access(PSWIN, os.X_OK):
        print("未找到 Windows interop,powershell 不可用")
        return 1
    rc, out = capture([PSWIN, "-NoProfile", "-Command", "usbipd list"])
    busids = [l.split()[0] for l in out.splitlines() if "Attached" in l]
    if not busids:
        print(">> 当前没有处于 Attached 的 usbip 设备,无需归还。")
        return 0
    for bid in busids:
        print(">> 归还 {} (会弹 UAC 提升窗口,请点'允许')...".format(bid))
        inner = ('Start-Process powershell -Verb RunAs -ArgumentList '
                 "'-NoProfile -Command \"usbipd detach --busid " + bid + "\"'")
        if run([PSWIN, "-NoProfile", "-Command", inner]) == 0:
            print(">> 已发起 detach(UAC 窗口中点允许即可)。")
        else:
            print("! 无法自动提升。请在管理员 PowerShell 手动执行: usbipd detach --busid {}".format(bid))
        print("   或: usbipd detach --busid {}".format(bid))
    return 0


def collect_files_and_links(base):
    res = set()
    for root, dirs, files in os.walk(base):
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), base)
            res.add(rel.replace(os.sep, "/"))
        for name in dirs:
            p = os.path.join(root, name)
            if os.path.islink(p):
                rel = os.path.relpath(p, base)
                res.add(rel.replace(os.sep, "/"))
    return res


def fs_change_report(out, srcdir):
    upd, dele = set(), set()
    with open(out, "r", errors="replace") as f:
        for line in f:
            fields = line.split()
            if (len(fields) > 1 and
                    fields[0] not in ("sending", "receiving") and
                    "deleting" not in fields[0] and
                    "Warning" not in fields[0] and
                    "Permanently" not in fields[0]):
                p = fields[1].lstrip("/")
                if not p.endswith("/"):
                    upd.add(p)
            m = re.match(r"^\*?deleting\s+(.+)$", line)
            if m:
                p = m.group(1)
                if not p.endswith("/"):
                    dele.add(p)
    source = collect_files_and_links(srcdir)
    unch = source - upd

    print(">> --- 文件级变更明细 ---")
    print("  ♻ 新增/更新: {} 个".format(len(upd)))
    for p in sorted(upd):
        print("      +  " + p)
    print("  ✖ 镜像删除(源已无,板上已删): {} 个".format(len(dele)))
    for p in sorted(dele):
        print("      -  " + p)
    print("  ○ 未变化(目标已与源一致): {} 个(均为一致,不再逐条列出)".format(len(unch)))


def mmc_list_cmd():
    return ('for d in /sys/block/mmcblk*/device/type; do '
            'b=$(basename $(dirname $(dirname "$d"))); t=$(cat "$d" 2>/dev/null); '
            'echo "  /dev/$b  type=$t"; done')


PROBE_SCRIPT = r'''echo "-- uname --"; uname -a
echo "  cmdline: $(cat /proc/cmdline)"
echo "-- dma@50000000 节点 --"
D=/proc/device-tree/amba_pl/dma@50000000
ls "$D" 2>&1 | grep -v "^$" | head -30
echo -n "  compatible   = "; cat "$D/compatible" 2>/dev/null; echo
echo -n "  include-sg?  = "; if [ -e "$D/xlnx,include-sg" ]; then echo "YES(仍是 SG!)"; else echo "NO (SIMPLE)"; fi
echo -n "  sg-length-width = "; cat "$D/xlnx,sg-length-width" 2>/dev/null | od -An -tx1; echo "(空/无=simple)"
echo "-- dmesg xilinx DMA 驱动 --"
dmesg 2>/dev/null | grep -iE "xilinx-vdma|xilinx_axidma|axi-dma|xlnx-s2mm|dma-chip|dmaengine" | tail -30
echo "-- /sys/class/dma 通道 --"
ls /sys/class/dma/ 2>/dev/null
echo "-- IRQ 61 --"; grep -w " 61 " /proc/interrupts 2>/dev/null'''


def do_deploy(args):
    mode = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-b", "--boot"):
            mode = "boot"
            i += 1
        elif a in ("-f", "--fs"):
            mode = "fs"
            i += 1
        else:
            break
    rest = args[i:]
    if mode is None:
        print("[错误] 必须用 -b/--boot 或 -f/--fs 二选一指定同步内容(boot 模块 / 文件系统)", file=sys.stderr)
        return 1
    if len(rest) < 3:
        print("[错误] 用法: flash-sd.py deploy -b|-f <源目录> <root@板IP> <设备>", file=sys.stderr)
        return 1
    src, dst, dev = rest[0], rest[1], rest[2]
    if mode == "fs":
        if not (os.path.isdir(src) or os.path.isfile(src)):
            print("[错误] 文件系统源不存在(应为目录或 .tar.gz/.tgz 归档): {}".format(src), file=sys.stderr)
            return 1
    elif not os.path.isdir(src):
        print("[错误] 源目录不存在: {}".format(src), file=sys.stderr)
        return 1
    ip = host_of(dst)
    remote_dev = dev if dev.startswith("/dev/") else "/dev/" + dev
    ensure_tools()

    print(">> 连接 root@{} (密码 root) ...".format(ip))
    rc, _ = rsh(ip, "echo ok")
    if rc != 0:
        print("[错误] 无法 SSH 到 {}(确认板已联网、sshd 已启动、密码为 root)".format(ip), file=sys.stderr)
        return 1

    bname = os.path.basename(remote_dev)
    rc, rem_out = rsh(ip, "[ -e '{}' ] && cat /sys/block/{}/removable 2>/dev/null".format(remote_dev, bname))
    rem = rem_out.splitlines()[0].strip() if rem_out.strip() else ""
    if not rem:
        print("[错误] 板上不存在目标设备 {},或读取 removable 失败".format(remote_dev), file=sys.stderr)
        return 1
    if rem == "1":
        print(">> 目标设备 {} 已确认为可移动 SD 介质 ✅".format(remote_dev))
    else:
        rc, dtype_out = rsh(ip, "cat /sys/block/{}/device/type 2>/dev/null".format(bname))
        dtype = dtype_out.splitlines()[0].strip() if dtype_out.strip() else ""
        if dtype == "":
            print("[错误] 无法读取 {} 的类型(removable={} 且 device/type 为空)".format(remote_dev, rem), file=sys.stderr)
            return 1
        elif dtype.startswith("SD") or dtype == "SDIO":
            print(">> 板上未提供 removable 标记(板载 SD 槽常见,=0),按 device/type={} 判定 {} 为 SD 卡 ✅".format(dtype, remote_dev))
        else:
            print("[错误] {} 不是 SD 介质(device/type={},疑似内嵌 eMMC/本地盘),拒绝操作。板上 mmc 设备:".format(remote_dev, dtype), file=sys.stderr)
            _, mmc_out = rsh(ip, mmc_list_cmd())
            if mmc_out.strip():
                print(mmc_out, file=sys.stderr, end="")
            return 1

    tgt_fstype = "vfat" if mode == "boot" else "ext4"
    awk_cmd = ("awk -v d='__DEV__' -v t='__TYPE__' "
               "'$1 ~ (\"^\" d \"p\") && $3==t {print $1, $2; exit}' /proc/mounts").replace("__DEV__", remote_dev).replace("__TYPE__", tgt_fstype)
    rc, out = rsh(ip, awk_cmd)
    line = out.splitlines()[0].strip() if out.strip() else ""
    parts = line.split()
    dev_part = parts[0] if len(parts) >= 1 else ""
    mountpoint = parts[1] if len(parts) >= 2 else ""
    if not dev_part or not mountpoint:
        pnum = "1" if mode == "boot" else "2"
        rdev = remote_dev[len("/dev/"):]
        dev_part = "/dev/{}p{}".format(rdev, pnum)
        mountpoint = "/mnt/deploy-{}-{}p{}".format(mode, rdev, pnum)
        print(">> 分区未挂载,尝试自动挂载 {} → {} ...".format(dev_part, mountpoint))
        mcmd = ("mkdir -p '{}' && mount '{}' '{}' >/dev/null 2>&1 "
                "&& grep -q '{} {} ' /proc/mounts").format(mountpoint, dev_part, mountpoint, dev_part, mountpoint)
        rc, _ = rsh(ip, mcmd)
        if rc != 0:
            print("[错误] 自动挂载 {} 失败(确认分区已建;或先手动 mount 再重试)".format(dev_part), file=sys.stderr)
            return 1
        print(">> 已自动挂载,后续同步结束会 umount")

    print(">> 目标分区: {}  挂载点: {}  ({})".format(dev_part, mountpoint, tgt_fstype))
    print(">> 同步 {} → 板上 {}/ ...".format(src, mountpoint))
    rc = 0
    if mode == "boot":
        skipped = []
        patterns = ("rootfs*", "pxelinux*", "vmlinux*", "*.cpio*", "*.jffs2",
                    "*.ext4", "*.manifest", "config", "*.elf")
        for f in sorted(os.listdir(src)):
            if any(fnmatch.fnmatch(f, p) for p in patterns):
                skipped.append(f)
                continue
            fp = os.path.join(src, f)
            r, _ = capture(["sshpass", "-p", "root", "scp"] + SSHOPTS +
                           ["-p", fp, "root@{}:{}/".format(ip, mountpoint)])
            if r == 0:
                print("  → " + f)
            else:
                print("[!] 拷贝失败: {}".format(fp), file=sys.stderr)
                rc = 1
        if skipped:
            print("  (跳过 {} 个非启动文件: {})".format(len(skipped), " ".join(skipped)))
    else:
        srcdir = src
        tarsrc = None
        if src.endswith((".tar.gz", ".tgz", ".tar")):
            tarsrc = tempfile.mkdtemp(prefix="deploy-fs.")
            rc_t, _ = capture(["tar", "-xzf", src, "-C", tarsrc])
            if rc_t != 0:
                print("[错误] 解压源失败: {}".format(src), file=sys.stderr)
                shutil.rmtree(tarsrc, ignore_errors=True)
                return 1
            print(">> 源 {} 是归档,已解压到临时目录 {}(同步后自动清理)".format(os.path.basename(src), tarsrc))
            srcdir = tarsrc
            entries = os.listdir(tarsrc)
            if len(entries) == 1:
                only = os.path.join(tarsrc, entries[0])
                if os.path.isdir(only):
                    srcdir = only
        rc_check, _ = rsh(ip, "command -v rsync >/dev/null 2>&1")
        if rc_check == 0:
            fd, rlog = tempfile.mkstemp(prefix="deploy-rsync.")
            os.close(fd)
            print(">> 同步 {}/ → 板上 {}/ ...".format(srcdir, mountpoint))
            exclude = ["--exclude=proc", "--exclude=sys", "--exclude=dev",
                       "--exclude=run", "--exclude=tmp",
                       "--exclude=media/sd-*", "--exclude=mnt/sd-*",
                       "--exclude=mnt/deploy-*"]
            rsync_cmd = (["sshpass", "-p", "root", "rsync", "-i", "-a", "-c",
                          "--delete", "--delete-during", "--numeric-ids"] +
                         exclude + ["-e", SSH_E, srcdir + "/",
                                    "root@{}:{}/".format(ip, mountpoint)])
            with open(rlog, "w") as fh:
                rc2 = subprocess.run(rsync_cmd, stdout=fh, stderr=subprocess.STDOUT,
                                     text=True).returncode
            if rc2 == 0:
                fs_change_report(rlog, srcdir)
            else:
                print("[!] rsync 同步出错,完整输出:", file=sys.stderr)
                with open(rlog, "r", errors="replace") as fh:
                    sys.stderr.write(fh.read())
                rc = 1
            os.remove(rlog)
        else:
            print("[错误] 板上无 rsync(deploy -f 需板上已带可用的 rsync,工程交叉静态版)。", file=sys.stderr)
            print("        板 rootfs 已带 rsync 即可;否则先把静态 rsync 放 boot 的 tools/,并 mount -t vfat /dev/{}p1 /mnt 后重试。".format(remote_dev[len('/dev/'):]), file=sys.stderr)
            rc = 1
        if tarsrc:
            shutil.rmtree(tarsrc, ignore_errors=True)

    rsh(ip, "sync; umount '{}' 2>/dev/null || true".format(mountpoint))
    if rc == 0:
        print(">> deploy({}) 完成。拔电/reboot 后生效。".format(mode))
    else:
        print("[!] 同步出错(退出码 {})".format(rc), file=sys.stderr)
        return rc
    return 0


def do_probe(dst):
    if not dst:
        print("用法: flash-sd.py probe root@板IP|板IP")
        return 1
    ip = host_of(dst)
    print("== 板 {} 摸底 (SIMPLE 模式) ==".format(ip))
    rsh(ip, PROBE_SCRIPT, capture_output=False)
    print("== probe done ==")
    return 0


def main(argv):
    if not sys.platform.startswith("linux"):
        print("[说明] flash-sd.py 依赖 Linux 工具(lsblk/parted/mount/rsync/sshpass)，仅支持 Linux/WSL。",
              file=sys.stderr)
        return 1
    args = list(argv)
    if not args:
        usage()
        return 0
    cmd = args[0]
    rest = args[1:]
    if cmd in ("list", "detect"):
        return detect()
    if cmd == "release":
        return release()
    if cmd == "part":
        dev = pick_dev()
        if dev is None:
            return 1
        ensure_tools()
        return do_part(dev)
    if cmd in ("mkfs", "format"):
        dev = pick_dev()
        if dev is None:
            return 1
        ensure_tools()
        return do_mkfs(dev)
    if cmd == "flash":
        srcarg = rest[0] if rest else None
        dev = pick_dev()
        if dev is None:
            return 1
        s = resolve_src(srcarg)
        if s is None:
            return 1
        ensure_tools()
        if not is_block(dev + "1"):
            print(">> {} 尚未分区,先分区...".format(dev))
            r = do_part(dev)
            if r != 0:
                return r
        return do_flash(dev, s)
    if cmd == "deploy":
        return do_deploy(rest)
    if cmd == "probe":
        return do_probe(rest[0] if rest else "")
    if cmd in ("-h", "--help", "help"):
        usage()
        return 0
    print("未知子命令: {}".format(cmd))
    usage()
    return 1


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    except KeyboardInterrupt:
        rc = 130
    sys.exit(0 if rc is None else rc)