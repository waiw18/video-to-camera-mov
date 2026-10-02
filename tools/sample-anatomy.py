"""解剖相机 MOV/MP4 的样本结构：每个样本里有哪些 NAL、参数集、切片数。

HEVC NAL 头是 2 字节：forbidden(1) nal_unit_type(6) nuh_layer_id(6) nuh_temporal_id_plus1(3)
样本在 MOV/MP4 里用 4 字节长度前缀分帧（AVCC/HVCC 格式）。

用法: python tools/sample-anatomy.py <文件> [看几个样本]
"""
import struct
import sys
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf"}

HEVC_NAL = {
    0: "TRAIL_N", 1: "TRAIL_R", 2: "TSA_N", 3: "TSA_R", 4: "STSA_N", 5: "STSA_R",
    6: "RADL_N", 7: "RADL_R", 8: "RASL_N", 9: "RASL_R",
    16: "BLA_W_LP", 17: "BLA_W_RADL", 18: "BLA_N_LP",
    19: "IDR_W_RADL", 20: "IDR_N_LP", 21: "CRA_NUT",
    32: "VPS", 33: "SPS", 34: "PPS", 35: "AUD", 39: "PREFIX_SEI", 40: "SUFFIX_SEI",
}


def boxes(d, s, e):
    off = s
    while off + 8 <= e:
        sz = struct.unpack(">I", d[off:off + 4])[0]
        ty = bytes(d[off + 4:off + 8])
        hdr = 8
        if sz == 1:
            sz = struct.unpack(">Q", d[off + 8:off + 16])[0]
            hdr = 16
        elif sz == 0:
            sz = e - off
        if sz < 8 or off + sz > e:
            return
        yield off, ty, sz, hdr
        off += sz


def find_all(d, s, e, typ, out=None):
    if out is None:
        out = []
    for off, t, sz, hdr in boxes(d, s, e):
        if t == typ:
            out.append((off, sz, hdr))
        if t in CONTAINERS:
            find_all(d, off + hdr, off + sz, typ, out)
    return out


def u32(d, o):
    return struct.unpack(">I", d[o:o + 4])[0]


def video_samples(d):
    moov = find_all(d, 0, len(d), b"moov")[0]
    traks = [x for x in boxes(d, moov[0] + moov[2], moov[0] + moov[1]) if x[1] == b"trak"]
    trak = traks[0]
    stbl = find_all(d, trak[0], trak[0] + trak[2], b"stbl")[0]
    s0, s1 = stbl[0], stbl[1]
    stsz = find_all(d, s0, s0 + s1, b"stsz")[0]
    bd = d[stsz[0] + 8:stsz[0] + stsz[1]]
    ss, cnt = struct.unpack(">II", bd[4:12])
    sizes = ([ss] * cnt) if ss else [u32(bd, 12 + 4 * i) for i in range(cnt)]
    co = find_all(d, s0, s0 + s1, b"co64") or find_all(d, s0, s0 + s1, b"stco")
    o, s, _h = co[0]
    is64 = d[o + 4:o + 8] == b"co64"
    cb = o + 8
    nc = u32(d, cb + 4)
    chunk_off = ([struct.unpack(">Q", d[cb + 8 + 8 * i:cb + 16 + 8 * i])[0]
                  for i in range(nc)] if is64 else [u32(d, cb + 8 + 4 * i) for i in range(nc)])
    stsc = find_all(d, s0, s0 + s1, b"stsc")[0]
    sb = d[stsc[0] + 8:stsc[0] + stsc[1]]
    nse = u32(sb, 4)
    ent = [(u32(sb, 8 + 12 * i), u32(sb, 12 + 12 * i)) for i in range(nse)]
    per = []
    for i, (first, spc) in enumerate(ent):
        last = ent[i + 1][0] - 1 if i + 1 < len(ent) else nc
        per.extend([spc] * (last - first + 1))
    offs, si = [], 0
    for coff, c in zip(chunk_off, per):
        pos = coff
        for _ in range(c):
            if si >= len(sizes):
                break
            offs.append(pos)
            pos += sizes[si]
            si += 1
    return sizes, offs


def parse_sample(buf):
    """返回该样本里的 NAL 列表 [(type, name, len)]"""
    out, p = [], 0
    while p + 4 <= len(buf):
        ln = struct.unpack(">I", buf[p:p + 4])[0]
        if ln == 0 or p + 4 + ln > len(buf):
            out.append(("TAIL", "?", len(buf) - p))
            break
        nal = buf[p + 4:p + 4 + ln]
        t = (nal[0] >> 1) & 0x3F
        out.append((t, HEVC_NAL.get(t, "?"), ln))
        p += 4 + ln
    return out


def main(path, n=6):
    d = Path(path).read_bytes()
    sizes, offs = video_samples(d)
    print(f"=== {Path(path).name}  样本数={len(sizes)}")
    print(f"  样本大小: min={min(sizes):,} max={max(sizes):,} mean={sum(sizes)//len(sizes):,} "
          f"total={sum(sizes):,}")
    counter = Counter()
    per_sample_counts = Counter()
    for i, (sz, off) in enumerate(zip(sizes, offs)):
        nals = parse_sample(d[off:off + sz])
        sig = tuple((t, l) if t != 35 else (t, 2) for t, _nm, l in nals)
        for t, nm, l in nals:
            counter[(t, nm)] += 1
        per_sample_counts[len(nals)] += 1
        if i < n:
            print(f"  样本{i}: size={sz:,}  NAL={[(nm, l) for _t, nm, l in nals]}")
    print(f"  NAL 总览: {dict(counter)}")
    print(f"  每样本 NAL 数分布: {dict(per_sample_counts)}")
    # 第一个样本的完整字节（前 80）
    if sizes:
        print(f"  样本0 前 80 字节: {d[offs[0]:offs[0]+80].hex(' ')}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 6)
