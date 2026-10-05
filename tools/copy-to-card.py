"""把候选文件拷到相机 SD 卡并做逐字节校验。

用法：
    python tools/copy-to-card.py DSC_4410.MP4
    python tools/copy-to-card.py out.mp4 --name DSC_4411.MP4
    python tools/copy-to-card.py --list

为什么需要脚本
    U 盘/SD 卡写入会静默出错，而相机对"读不了"和"抽搐"的反馈无法区分这两个原因。
    每轮上卡前先比 SHA256，能排除掉一整类假故障。

自动找卡规则：可移动盘 + 卷标含 NIKON，或根目录存在 DCIM 目录。
"""
import argparse
import hashlib
import json
import os
import string
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _ps(cmd):
    """跑 PowerShell，用 UTF-8 解码（卷标里可能有中文，默认 GBK 会炸）。"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + cmd],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout or ""


def volumes():
    out = []
    for line in _ps("Get-Volume | Where-Object {$_.DriveLetter} | "
                    "ForEach-Object {\"$($_.DriveLetter)|$($_.DriveType)|"
                    "$($_.FileSystemLabel)\"}").splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0]:
            out.append((parts[0], parts[1], parts[2]))
    return out


def find_card():
    for letter, dtype, label in volumes():
        root = Path(f"{letter}:\\")
        if not root.exists():
            continue
        if dtype.lower() != "removable" and "NIKON" not in label.upper():
            continue
        if (root / "DCIM").is_dir():
            return root, label
    return None, None


def media_dir(root):
    """相机实际列文件的目录：DCIM\\<数字>NZ5_2\\"""
    subs = [p for p in (root / "DCIM").iterdir() if p.is_dir()]
    if not subs:
        raise SystemExit("DCIM 下没有子目录")
    return sorted(subs)[0]


def sha256(p, chunk=1 << 20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest().upper()


def _emit(kind, **kw):
    """给界面（app/main.py）用的机器可读进度行，协议见 tools/video-to-camera-mov.py。"""
    print("##CAMMOV " + json.dumps({"k": kind, **kw}, ensure_ascii=False),
          flush=True)


def copy_prog(src, dst):
    """分块拷贝并报进度。SD 卡写入慢（380 MB 要几十秒），界面必须有数。

    语义与 shutil.copyfile 一致：逐字节写入；写完 fsync 落盘，再交给后面的
    SHA256 回读校验（这是"卡上文件到底对不对"的唯一凭据，不能省）。
    """
    total = src.stat().st_size
    done, t_last = 0, 0.0
    _emit("stage", step=2, total=3, done="校验源哈希", running="写入卡")
    with src.open("rb") as r, dst.open("wb") as w:
        while True:
            buf = r.read(1 << 20)
            if not buf:
                break
            w.write(buf)
            done += len(buf)
            now = time.time()
            if now - t_last >= 0.5:
                t_last = now
                _emit("prog", done=done, total=total,
                      pct=round(done / total * 100, 1) if total else None)
        w.flush()
        os.fsync(w.fileno())
    _emit("prog", done=done, total=total, pct=100.0)
    return done


def existing_numbers(mdir):
    import re
    nums = set()
    for p in mdir.iterdir():
        m = re.match(r"DSC_(\d{4})\.(MP4|MOV)$", p.name, re.I)
        if m:
            nums.add(int(m.group(1)))
    return nums


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", help="要拷的本地文件")
    ap.add_argument("--name", help="卡上的文件名（默认用本地文件名）")
    ap.add_argument("--list", action="store_true", help="只列出卡上现有文件")
    ap.add_argument("--force", action="store_true",
                    help="允许覆盖同名文件（会先比哈希，相同就不重拷）")
    a = ap.parse_args()

    root, label = find_card()
    if root is None:
        raise SystemExit("没找到相机卡（可移动盘 + 含 DCIM 目录）")
    mdir = media_dir(root)
    print(f"相机卡: {root}  卷标={label}")
    print(f"视频目录: {mdir}")

    if a.list or not a.file:
        for p in sorted(mdir.iterdir()):
            print(f"  {p.name:20} {p.stat().st_size:>12,} B")
        if a.list:
            return
        raise SystemExit("请给出要拷的文件")

    src = Path(a.file)
    if not src.is_absolute():
        src = ROOT / src
    if not src.exists():
        raise SystemExit(f"本地文件不存在: {src}")

    name = a.name or src.name
    if not (name.upper().startswith("DSC_") and name.upper().endswith((".MP4", ".MOV"))):
        print(f"警告: {name} 不符合 DSC_####.MP4 命名，相机可能不列出该文件")
    used = existing_numbers(mdir)
    if name.upper().startswith("DSC_") and name[4:8].isdigit():
        n = int(name[4:8])
        if n in used:
            if not a.force:
                raise SystemExit(f"卡上已有 {name}（编号 {n} 已占用），"
                                 f"换一个编号或用 --force 覆盖")
            dst_old = mdir / name
            if dst_old.exists() and sha256(dst_old) == sha256(src):
                print(f"卡上 {name} 与源文件哈希相同，无需重拷")
                _emit("done", out=str(dst_old), bytes=dst_old.stat().st_size,
                      skipped=True)
                return
            print(f"卡上已有 {name}，--force：将覆盖")

    dst = mdir / name
    print(f"源:   {src}  ({src.stat().st_size:,} B)")
    print(f"目标: {dst}")

    t0 = time.time()
    _emit("stage", step=1, total=3, done=None, running="校验源哈希")
    h_src = sha256(src)
    copy_prog(src, dst)
    _emit("stage", step=3, total=3, done="写入卡", running="回读校验")
    h_dst = sha256(dst)
    ok = h_src == h_dst and src.stat().st_size == dst.stat().st_size

    print(f"  SHA256 源   {h_src}")
    print(f"  SHA256 卡上 {h_dst}")
    print(f"  {'[OK] 逐字节一致' if ok else '[FAIL] 校验不一致，请重新拷贝'}")

    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Write-VolumeCache -DriveLetter {root.drive[0]}"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("  写缓存已刷新，可以拔卡")
    if not ok:
        # ★ 校验不一致必须非零退出：界面（和任何自动化）靠退出码判断能不能继续，
        #   以前这里只是打印 FAIL 然后正常退出，会被当成成功。
        raise SystemExit("  卡上文件与源不一致（SHA256 不同），请重新拷贝")
    _emit("done", out=str(dst), bytes=dst.stat().st_size,
          seconds=round(time.time() - t0, 1), verify=True)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
