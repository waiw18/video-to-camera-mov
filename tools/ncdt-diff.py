"""对比两段不同时长的相机原片的 NCDT，找出与内容/时长相关的字段。

同时打印 NCDT 的子盒结构（为了重建缩略图）。
"""
import struct
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from boxio import find, track_list, u32   # noqa: E402

FMT = {1: "u8", 2: "char", 3: "u16", 4: "u32", 5: "urational", 6: "s8",
       7: "blob", 8: "s16", 9: "s32"}
UNIT = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4}


def ncdt_of(d):
    return find(d, 0, len(d), b"NCDT")


def subboxes(d, off, size):
    """NCDT 里的子盒（8 字节头）。"""
    out, p, end = [], off, off + size
    while p + 8 <= end:
        sz = struct.unpack(">I", d[p:p + 4])[0]
        t = bytes(d[p + 4:p + 8])
        if sz < 8 or p + sz > end:
            break
        out.append((t.decode("latin1"), p, sz))
        p += sz
    return out


def records(d, ncdt_off, ncdt_size):
    """NCTG 记录。"""
    nctg = find(d, ncdt_off, ncdt_off + ncdt_size, b"NCTG")
    if not nctg:
        return []
    out, p = [], nctg[0] + 8
    while p + 8 <= nctg[0] + nctg[2]:
        tag = struct.unpack(">I", d[p:p + 4])[0]
        fmt = struct.unpack(">H", d[p + 4:p + 6])[0]
        cnt = struct.unpack(">H", d[p + 6:p + 8])[0]
        nb = UNIT.get(fmt, 1) * cnt
        if tag == 0 or p + 8 + nb > ncdt_off + ncdt_size:
            break
        raw = bytes(d[p + 8:p + 8 + nb])
        out.append((tag, fmt, cnt, raw))
        p += 8 + nb
    return out


def desc(fmt, raw):
    if fmt in (2,):
        return raw.split(b"\x00")[0].decode("latin1", "replace")[:28]
    if fmt in (3, 8):
        return [struct.unpack(">H", raw[2 * k:2 * k + 2])[0] for k in range(len(raw) // 2)][:6]
    if fmt in (4, 9):
        sg = ">" + ("i" if fmt == 9 else "I")
        return [struct.unpack(sg, raw[4 * k:4 * k + 4])[0] for k in range(len(raw) // 4)][:6]
    if fmt == 5:
        return [f"{struct.unpack('>I', raw[8*k:8*k+4])[0]}/{struct.unpack('>I', raw[8*k+4:8*k+8])[0]}"
                for k in range(len(raw) // 8)][:4]
    if fmt == 7:
        return f"{len(raw)}B blob 头={raw[:16].hex(' ')}"
    return f"{len(raw)}B"


def main(paths):
    data = {}
    for p in paths:
        d = Path(p).read_bytes()
        tr = track_list(d)[0]
        stbl = find(d, tr[0], tr[0] + tr[2], b"stbl")
        szb = find(d, stbl[0], stbl[0] + stbl[2], b"stsz")
        frames = u32(d, szb[0] + 16)
        mv = find(d, 0, len(d), b"mvhd")
        mts, mdur = u32(d, mv[0] + 20), u32(d, mv[0] + 24)
        nd = ncdt_of(d)
        print(f"=== {Path(p).name}  帧={frames}  容器时长={mdur/mts:.4f}s  "
              f"NCDT@{nd[0]:,} {nd[2]:,}B")
        print("  子盒: " + ", ".join(f"{t}({s:,})" for t, _o, s in subboxes(d, nd[0] + 8, nd[2] - 8)))
        data[Path(p).name] = (frames, mdur / mts, records(d, nd[0], nd[2]))

    names = list(data)
    if len(names) >= 2:
        a, b = data[names[0]], data[names[1]]
        ra = {t: (f, c, r) for t, f, c, r in a[2]}
        rb = {t: (f, c, r) for t, f, c, r in b[2]}
        print(f"\n=== NCTG 记录差异（{names[0]} vs {names[1]}）")
        for t in sorted(set(ra) | set(rb)):
            va = desc(ra[t][0], ra[t][2]) if t in ra else "—"
            vb = desc(rb[t][0], rb[t][2]) if t in rb else "—"
            if va != vb:
                print(f"  {t:#06x}: {va}  ->  {vb}")


if __name__ == "__main__":
    main(sys.argv[1:])
