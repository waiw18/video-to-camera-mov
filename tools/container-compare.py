"""并排列出 MOV 与 MP4 的容器结构差异（全部来自实测文件）。

用法: python tools/container-compare.py <文件A> <文件B>
"""
import struct
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from boxio import walk, children, find   # noqa: E402

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf",
              b"udta", b"meta", b"ilst"}


def tree(d, s, e, depth=0, maxd=3):
    """返回 [(name, off, size, depth)]"""
    out = []
    for off, t, sz, hdr in walk(d, s, e):
        name = t.decode("latin1", "replace")
        out.append((name, off, sz, depth))
        if t in CONTAINERS and depth < maxd:
            inner = off + hdr + (8 if t == b"stsd" else 0)
            out.extend(tree(d, inner, off + sz, depth + 1, maxd))
    return out


def ftyp_of(d):
    sz = struct.unpack(">I", d[0:4])[0]
    ty = d[4:8].decode("latin1", "replace")
    major = d[8:12].decode("latin1", "replace")
    minor = struct.unpack(">I", d[12:16])[0]
    compat = [d[16 + 4 * i:20 + 4 * i].decode("latin1", "replace")
              for i in range((sz - 16) // 4)]
    return sz, ty, major, minor, compat


def sample_entry(d):
    moov = find(d, 0, len(d), b"moov")
    traks = [x for x in children(d, moov) if x[1] == b"trak"]
    res = []
    for tr in traks:
        stbl = find(d, tr[0], tr[0] + tr[2], b"stbl")
        stsd = find(d, stbl[0], stbl[0] + stbl[2], b"stsd")
        inner = stsd[0] + 16
        esz = struct.unpack(">I", d[inner:inner + 4])[0]
        fmt = d[inner + 4:inner + 8].decode("latin1", "replace")
        sub = []
        for off, t, sz, hdr in walk(d, inner + 8 + 78, inner + esz):
            sub.append((t.decode("latin1", "replace"), sz))
        res.append((fmt, esz, sub))
    return res


def main(a_path, b_path):
    A, B = Path(a_path).read_bytes(), Path(b_path).read_bytes()
    print(f"A = {Path(a_path).name}   {len(A):,} B")
    print(f"B = {Path(b_path).name}   {len(B):,} B")
    print()

    for label, d in (("A", A), ("B", B)):
        sz, ty, major, minor, compat = ftyp_of(d)
        print(f"[{label}] ftyp: size={sz} major={major!r} minor={minor} "
              f"compat={compat}")
    print()

    for label, d in (("A", A), ("B", B)):
        print(f"[{label}] 顶层 box:")
        for name, off, sz, dep in tree(d, 0, len(d), 0, 0):
            print(f"      {name:6} @{off:>12,} {sz:>12,}")
    print()

    for label, d in (("A", A), ("B", B)):
        print(f"[{label}] sample entry:")
        for fmt, esz, sub in sample_entry(d):
            print(f"      format={fmt} entry_size={esz}")
            for nm, sz in sub:
                print(f"          + {nm:8} {sz:>8,}")
    print()

    # 差异汇总
    print("=== 差异汇总 ===")
    for label, d in (("A", A), ("B", B)):
        moov = find(d, 0, len(d), b"moov")
        kids = [t.decode("latin1", "replace") for _o, t, _s, _h in children(d, moov)]
        tops = [t.decode("latin1", "replace") for _o, t, _s, _h in walk(d, 0, len(d))]
        has_wide = b"wide" in d[:64]
        print(f"  {label}: 顶层={tops} moov子={kids} 含wide={has_wide}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
