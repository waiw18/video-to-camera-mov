"""打印相机文件的 chunk 交错顺序：每个 chunk 的轨道、偏移、样本数、字节数。"""
import struct
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from boxio import find, track_list, walk, u32, u64


def plan(d, trak):
    stbl = find(d, trak[0], trak[0] + trak[2], b"stbl")
    szb = find(d, stbl[0], stbl[0] + stbl[2], b"stsz")
    ss = u32(d, szb[0] + 12)
    cnt = u32(d, szb[0] + 16)
    sizes = ([ss] * cnt) if ss else [u32(d, szb[0] + 20 + 4 * i) for i in range(cnt)]
    co = find(d, stbl[0], stbl[0] + stbl[2], b"co64") or \
         find(d, stbl[0], stbl[0] + stbl[2], b"stco")
    is64 = d[co[0] + 4:co[0] + 8] == b"co64"
    cb = co[0] + 8
    nc = u32(d, cb + 4)
    coffs = ([u64(d, cb + 8 + 8 * i) for i in range(nc)] if is64
             else [u32(d, cb + 8 + 4 * i) for i in range(nc)])
    stsc = find(d, stbl[0], stbl[0] + stbl[2], b"stsc")
    sb = d[stsc[0] + 8:stsc[0] + stsc[2]]
    nse = u32(sb, 4)
    ent = [(u32(sb, 8 + 12 * i), u32(sb, 12 + 12 * i)) for i in range(nse)]
    per = []
    for i, (first, spc) in enumerate(ent):
        last = ent[i + 1][0] - 1 if i + 1 < len(ent) else nc
        per.extend([spc] * (last - first + 1))
    return sizes, coffs, per


def main(path):
    d = Path(path).read_bytes()
    mdat = next(x for x in walk(d, 0, len(d)) if x[1] == b"mdat")
    print(f"{Path(path).name}: mdat@{mdat[0]:,}\n")
    trs = track_list(d)
    lanes = []
    for ti, tr in enumerate(trs):
        sizes, coffs, per = plan(d, tr)
        si = 0
        for ci, (coff, c) in enumerate(zip(coffs, per)):
            nbytes = sum(sizes[si:si + c])
            lanes.append((coff, ti, ci, si, c, nbytes))
            si += c
    lanes.sort()
    print(f"{'序':>3} {'偏移':>12} {'轨道':>4} {'chunk':>5} {'首样本':>8} {'样本数':>8} {'字节数':>12}")
    for k, (coff, ti, ci, si, c, nb) in enumerate(lanes):
        print(f"{k:>3} {coff:>12,} {ti:>4} {ci:>5} {si:>8,} {c:>8,} {nb:>12,}")


if __name__ == "__main__":
    main(sys.argv[1])
