"""逐子盒十六进制对比两个 MOV 的 moov（含尾部每个字段），重点标出 16 位字段。

用法: python tail-diff.py A.MOV B.MOV
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from boxio import find, track_list, u32   # noqa: E402

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta"}


def walk(d, start, end, out, path=""):
    p = start
    while p + 8 <= end:
        size = struct.unpack(">I", d[p:p + 4])[0]
        typ = bytes(d[p + 4:p + 8])
        if size < 8 or p + size > end:
            return
        out.append((path + "/" + typ.decode("latin1"), p, size, typ))
        if typ in CONTAINERS:
            walk(d, p + 8, p + size, out, path + "/" + typ.decode("latin1"))
        p += size
    return out


def dump(path, limit_box=400):
    d = Path(path).read_bytes()
    out = []
    walk(d, 0, len(d), out)
    print(f"===== {Path(path).name}")
    for name, off, size, typ in out:
        body = d[off + 8:off + size]
        if typ in CONTAINERS or typ in (b"NCDT",):
            print(f"  {name:<34} @{off:<9,} {size:>10,} B  （容器，只列大小）")
            continue
        if typ in (b"NCTH", b"NCVW", b"NCM1"):
            print(f"  {name:<34} @{off:<9,} {size:>10,} B  （JPEG，跳过）")
            continue
        show = body if len(body) <= limit_box else body[:limit_box]
        hexs = " ".join(f"{x:02X}" for x in show)
        print(f"  {name:<34} @{off:<9,} {size:>10,} B")
        print(f"      {hexs}" + ("  …" if len(body) > limit_box else ""))
        # 顺带把明显的 16 位字段标出来
        if len(body) >= 4 and typ in (b"stsz", b"stsc", b"stts", b"co64", b"stss"):
            pass
    return out


if __name__ == "__main__":
    a, b = sys.argv[1], sys.argv[2]
    da, db = Path(a).read_bytes(), Path(b).read_bytes()
    oa, ob = [], []
    walk(da, 0, len(da), oa)
    walk(db, 0, len(db), ob)
    na = {n for n, *_ in oa}
    nb = {n for n, *_ in ob}
    print("=== 盒路径集合差异 ===")
    print("  仅 A:", sorted(na - nb) or "无")
    print("  仅 B:", sorted(nb - na) or "无")
    print()
    dump(a)
    print()
    dump(b)
