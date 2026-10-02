"""精确回答：我的复刻件与原片的差异到底是「布局」还是「数据」。

分别取两条轨的样本，按内容逐样本比对哈希。
"""
import hashlib
import struct
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from boxio import find, track_list, children, walk, u32, u64


def plan(d, trak):
    stbl = find(d, trak[0], trak[0] + trak[2], b"stbl")
    s0 = stbl[0]
    szb = find(d, s0, s0 + stbl[2], b"stsz")
    bd = d[szb[0] + 8:szb[0] + szb[2]]
    ss, cnt = struct.unpack(">II", bd[4:12])
    sizes = ([ss] * cnt) if ss else [u32(bd, 12 + 4 * i) for i in range(cnt)]
    co = find(d, s0, s0 + stbl[2], b"co64") or find(d, s0, s0 + stbl[2], b"stco")
    is64 = d[co[0] + 4:co[0] + 8] == b"co64"
    cb = co[0] + 8
    nc = u32(d, cb + 4)
    coffs = ([u64(d, cb + 8 + 8 * i) for i in range(nc)] if is64
             else [u32(d, cb + 8 + 4 * i) for i in range(nc)])
    stsc = find(d, s0, s0 + stbl[2], b"stsc")
    sb = d[stsc[0] + 8:stsc[0] + stsc[2]]
    nse = u32(sb, 4)
    ent = [(u32(sb, 8 + 12 * i), u32(sb, 12 + 12 * i)) for i in range(nse)]
    per = []
    for i, (first, spc) in enumerate(ent):
        last = ent[i + 1][0] - 1 if i + 1 < len(ent) else nc
        per.extend([spc] * (last - first + 1))
    return sizes, coffs, per


def samples(d, trak):
    sizes, coffs, per = plan(d, trak)
    out, si = [], 0
    for coff, c in zip(coffs, per):
        pos = coff
        for _ in range(c):
            if si >= len(sizes):
                break
            out.append(bytes(d[pos:pos + sizes[si]]))
            pos += sizes[si]
            si += 1
    return out


def main(a_path, b_path):
    A, B = Path(a_path).read_bytes(), Path(b_path).read_bytes()
    print(f"原片   {Path(a_path).name}  {len(A):,} B")
    print(f"复刻件 {Path(b_path).name}  {len(B):,} B\n")
    mdatA = next(x for x in walk(A, 0, len(A)) if x[1] == b"mdat")
    mdatB = next(x for x in walk(B, 0, len(B)) if x[1] == b"mdat")

    for ti in (0, 1):
        ta, tb = track_list(A)[ti], track_list(B)[ti]
        sa, sb = samples(A, ta), samples(B, tb)
        ha = [hashlib.sha1(x).hexdigest() for x in sa]
        hb = [hashlib.sha1(x).hexdigest() for x in sb]
        same = sum(1 for x, y in zip(ha, hb) if x == y)
        name = {0: "视频", 1: "音频"}[ti]
        print(f"[{name}] 样本数 A={len(sa)} B={len(sb)}  逐样本哈希相同={same}")
        if len(sa) == len(sb) and same == len(sa):
            print(f"        → 数据 100% 一致（差异只在容器）")
        else:
            for i, (x, y) in enumerate(zip(ha, hb)):
                if x != y:
                    print(f"        首个不同样本 #{i}: A={len(sa[i])}B B={len(sb[i])}B")
                    print(f"          A={sa[i][:16].hex(' ')}")
                    print(f"          B={sb[i][:16].hex(' ')}")
                    break
        # chunk 偏移
        _, ca, pa = plan(A, ta)
        _, cb, pb = plan(B, ta)
        print(f"        chunk数 A={len(ca)} B={len(cb)}  "
              f"首个 chunk 偏移 A={ca[0]:,} B={cb[0]:,} (mdat@{mdatA[0]:,}/{mdatB[0]:,})")
        print(f"        samples_per_chunk A={pa[:3]}... B={pb[:3]}...")
    print()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
