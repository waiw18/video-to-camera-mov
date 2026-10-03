"""递归打印 MOV 盒树（类型+大小），用于两个文件的结构对比。

用法：
    python box-types.py A.MOV B.MOV      # 直接对比盒类型序列
    python box-types.py A.MOV            # 只打印
"""
import struct
import sys
from pathlib import Path

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf",
              b"udta", b"meta", b"ilst", b"----", b"moof", b"traf", b"mvex"}


def walk(d, start, end, depth=0, out=None, path=""):
    p = start
    while p + 8 <= end:
        size = struct.unpack(">I", d[p:p + 4])[0]
        typ = bytes(d[p + 4:p + 8])
        if size == 1 and p + 16 <= end:
            size = struct.unpack(">Q", d[p + 8:p + 16])[0]
            hdr = 16
        else:
            hdr = 8
        if size < hdr or p + size > end:
            out.append((depth, "!!坏盒 %r size=%d 于 %d" % (typ, size, p), 0))
            return
        out.append((depth, typ.decode("latin1"), size))
        if typ in CONTAINERS:
            walk(d, p + hdr, p + size, depth + 1, out)
        p += size
    return out


def sig(path):
    d = Path(path).read_bytes()
    return walk(d, 0, len(d), 0, [])


if __name__ == "__main__":
    if len(sys.argv) == 2:
        for dep, t, s in sig(sys.argv[1]):
            print("  " * dep + f"{t:<8} {s:,}")
    elif len(sys.argv) >= 3:
        A, B = sig(sys.argv[1]), sig(sys.argv[2])
        na = [t for _, t, _ in A]
        nb = [t for _, t, _ in B]
        print(f"  {Path(sys.argv[1]).name}: {len(na)} 个盒")
        print(f"  {Path(sys.argv[2]).name}: {len(nb)} 个盒")
        only_a = [t for t in na if t not in nb]
        only_b = [t for t in nb if t not in na]
        print(f"  仅前者有: {sorted(set(only_a)) or '无'}")
        print(f"  仅后者有: {sorted(set(only_b)) or '无'}")
        # 按类型聚合大小
        from collections import defaultdict
        sa, sb = defaultdict(int), defaultdict(int)
        ca, cb = defaultdict(int), defaultdict(int)
        for _, t, s in A:
            sa[t] += s
            ca[t] += 1
        for _, t, s in B:
            sb[t] += s
            cb[t] += 1
        print(f"  {'类型':<10}{'4521 个数':>10}{'4537 个数':>10}{'4521 总大小':>14}{'4537 总大小':>14}")
        for t in sorted(set(sa) | set(sb)):
            if ca[t] != cb[t] or (t.startswith("st") or t in ("co64", "ctts", "elst", "tkhd")):
                print(f"  {t:<10}{ca[t]:>10}{cb[t]:>10}{sa[t]:>14,}{sb[t]:>14,}")
