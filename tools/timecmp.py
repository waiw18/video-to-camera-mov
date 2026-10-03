"""横向对比 MOV 的时间字段：mvhd / tkhd / mdhd / stts / elst / NCDT 帧率。

用来定位"快进、时长显示错、音视频不齐"这类时基问题。
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from boxio import find, track_list, u32   # noqa: E402

UNIT = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 1}


def ncdt_fps(d):
    nd = find(d, 0, len(d), b"NCDT")
    if not nd:
        return None
    nctg = find(d, nd[0] + 8, nd[0] + nd[2], b"NCTG")
    p, end = nctg[0] + 8, nctg[0] + nctg[2]
    num = den = frames = None
    while p + 8 <= end:
        tag = struct.unpack(">I", d[p:p + 4])[0]
        fmt = struct.unpack(">H", d[p + 4:p + 6])[0]
        cnt = struct.unpack(">H", d[p + 6:p + 8])[0]
        nb = UNIT.get(fmt, 1) * cnt
        if tag == 0 or p + 8 + nb > end:
            break
        if tag in (0x16, 0x17) and fmt == 5:
            num, den = struct.unpack(">II", d[p + 8:p + 16])
        if tag == 0x13 and fmt == 4:
            frames = struct.unpack(">I", d[p + 8:p + 12])[0]
        p += 8 + nb
    return num, den, frames


def stts_first(d, trak):
    stbl = find(d, trak[0], trak[0] + trak[2], b"stbl")
    b = find(d, stbl[0], stbl[0] + stbl[2], b"stts")
    cnt = u32(d, b[0] + 12)
    n, delta = struct.unpack(">II", d[b[0] + 16:b[0] + 24])
    return cnt, n, delta


def dump(f):
    d = Path(f).read_bytes()
    trs = track_list(d)
    mv = find(d, 0, len(d), b"mvhd")
    mts, mdur = u32(d, mv[0] + 20), u32(d, mv[0] + 24)
    print(f"--- {Path(f).name}")
    print(f"    mvhd   timescale={mts:<7} duration={mdur:<11} = {mdur/mts:.4f} s")
    for i, tr in enumerate(trs):
        md = find(d, tr[0], tr[0] + tr[2], b"mdhd")
        ts, du = u32(d, md[0] + 20), u32(d, md[0] + 24)
        tk = find(d, tr[0], tr[0] + tr[2], b"tkhd")
        tdu = u32(d, tk[0] + 28) if d[tk[0] + 8] == 0 else u32(d, tk[0] + 36)
        kind = "视频" if i == 0 else "音频"
        print(f"    {kind}   mdhd timescale={ts:<7} duration={du:<11} = {du/ts:.4f} s"
              f"   tkhd.duration={tdu} (movie ts = {tdu/mts:.4f} s)")
        c, n, delta = stts_first(d, tr)
        print(f"           stts: 条目={c}  首条=(count={n}, delta={delta})"
              f"  -> 每样本 {delta}/{ts} s = {ts/delta:.4f} fps")
    fps = ncdt_fps(d)
    if fps:
        print(f"    NCDT   0x16/0x17 = {fps[0]}/{fps[1]}  帧数={fps[2]}"
              f"  -> {fps[0]/fps[1]:.4f} fps")


if __name__ == "__main__":
    for a in sys.argv[1:]:
        dump(a)
