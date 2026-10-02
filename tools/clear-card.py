"""清卡：把卡上的文件先在本机留下校验过的副本，再删除。

安全策略（绝不裸删）：
    对每个要删的文件，先找到一个"哈希与它相同"的本机副本；
    找不到就复制到 archive/card-tests/ 再校验；只有校验通过才删卡上的。

用法
    python tools/clear-card.py --keep DSC_4440.MOV
    python tools/clear-card.py --keep DSC_4440.MOV --dry-run
"""
import argparse
import hashlib
import shutil
import sys
from pathlib import Path

VIDEO_DIR = Path(r"F:\DCIM\118NZ5_2")
LOCAL_DIRS = [Path("cam-tests"), Path("archive/card-tests"), Path(".")]
ARCHIVE = Path("cam-tests")          # 被"转移"的实拍原片放这里，作为参考基准


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 22), b""):
            h.update(blk)
    return h.hexdigest().upper()


def find_local_copy(name, digest):
    """本机是否已有同哈希副本。

    先按同名查（cam-tests / archive / 根目录），再在备份目录里跨名查
    （例如 DSC_8951-fresh-camera.mp4 其实就是卡上的 DSC_8951.MP4）。
    跨名扫描只覆盖备份目录，避免把根目录的大文件全部哈希一遍。
    """
    for d in LOCAL_DIRS:
        c = d / name
        if c.exists() and c.is_file() and sha256(c) == digest:
            return c
    for d in (Path("cam-tests"), Path("archive/card-tests")):
        if not d.exists():
            continue
        for c in d.iterdir():
            if c.is_file() and sha256(c) == digest:
                return c
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", nargs="*", default=[], help="卡上要保留的文件名")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    keep = {k.upper() for k in a.keep} | {"NC_FLLST.DAT"}
    if not VIDEO_DIR.exists():
        raise SystemExit(f"卡不在: {VIDEO_DIR}")

    files = sorted(p for p in VIDEO_DIR.iterdir() if p.is_file())
    todo = [p for p in files if p.name.upper() not in keep]
    print(f"卡上 {len(files)} 个文件，保留 {sorted(keep)}，处理 {len(todo)} 个\n")

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    saved, reused, failed = [], [], []

    for p in todo:
        digest = sha256(p)
        local = find_local_copy(p.name, digest)
        if local is not None:
            reused.append((p.name, local))
            print(f"[已有副本] {p.name:18} -> {local}")
        else:
            dst = ARCHIVE / p.name
            if a.dry_run:
                print(f"[待转移]   {p.name:18} -> {dst}")
                continue
            shutil.copy2(p, dst)
            got = sha256(dst)
            if got != digest:
                failed.append(p.name)
                print(f"[失败]     {p.name:18} 转移后哈希不符，不删卡上原件")
                continue
            saved.append((p.name, dst))
            print(f"[已转移]   {p.name:18} -> {dst}")

    if a.dry_run:
        print("\n--dry-run，未删除任何文件")
        return

    print()
    deleted = 0
    for p in todo:
        if p.name in failed:
            continue
        # 再确认本机副本存在且哈希一致，才删
        digest = sha256(p)
        if find_local_copy(p.name, digest) is None:
            print(f"[跳过]     {p.name:18} 校验不到本机副本，保留卡上原件")
            continue
        p.unlink()
        deleted += 1
        print(f"[已删除]   {p.name}")

    print(f"\n转移 {len(saved)} 个（其中 {len(reused)} 个本机已有）、删除 {deleted} 个")
    if failed:
        print(f"！{len(failed)} 个转移失败，已保留卡上原件: {failed}")
    left = sorted(q.name for q in VIDEO_DIR.iterdir() if q.is_file())
    print(f"\n卡上剩余: {left}")


if __name__ == "__main__":
    main()
