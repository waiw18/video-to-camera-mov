"""核对交付候选：顶层布局、udta/NCDT 帧数、样本数、音频帧数。"""
import hashlib
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from boxio import walk, find, track_list, u32   # noqa: E402


def nctg_framecount(d):
    udta = find(d, 0, len(d), b"udta")
    if not udta:
        return None
    ncdt = find(d, udta[0], udta[0] + udta[2], b"NCDT")
    if not ncdt:
        return None
    blob = d[ncdt[0]:ncdt[0] + ncdt[2]]
    p = blob.find(b"NCTG")
    if p < 0:
        return None
    p += 4
    import struct
    while p + 8 <= len(blob):
        tag = struct.unpack(">I", blob[p:p + 4])[0]
        fmt = struct.unpack(">H", blob[p + 4:p + 6])[0]
        cnt = struct.unpack(">H", blob[p + 6:p + 8])[0]
        unit = {2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4}.get(fmt, 1)
        if p + 8 + unit * cnt > len(blob):
            break
        if tag == 0x13 and fmt == 4:
            return struct.unpack(">I", blob[p + 8:p + 12])[0]
        p += 8 + unit * cnt
    return None


def main(paths):
    for p in paths:
        f = Path(p)
        if not f.exists():
            print(f"{f.name}: 不存在")
            continue
        d = f.read_bytes()
        tops = [(t.decode("latin1"), o, s) for o, t, s, h in walk(d, 0, len(d))]
        trs = track_list(d)
        v = trs[0]
        stbl = find(d, v[0], v[0] + v[2], b"stbl")
        szb = find(d, stbl[0], stbl[0] + stbl[2], b"stsz")
        vcnt = u32(d, szb[0] + 16)
        udta = find(d, 0, len(d), b"udta")
        ncdt = find(d, udta[0], udta[0] + udta[2], b"NCDT") if udta else None
        print(f"--- {f.name}  {len(d):,} B")
        print(f"    顶层: {[(t, o, f'{s:,}') for t, o, s in tops]}")
        print(f"    视频样本={vcnt}  NCDT={'无' if not ncdt else f'{ncdt[2]:,} B'}"
              f"  NCDT帧数={nctg_framecount(d)}")
        print(f"    sha256={hashlib.sha256(d).hexdigest()[:24]}")


if __name__ == "__main__":
    main(sys.argv[1:])
