"""Read the exact picture-type sequence of the camera's video track.

For every sample, decode the slice header far enough to tell I from P from B,
and record slice type, first_mb_in_slice, frame_num, pic_order_cnt_lsb and the
reference list usage. The point is to reproduce the camera's GOP structure
exactly rather than approximating it with -bf N.
"""
import struct
import sys
from pathlib import Path

CONTAINERS = {b"moov", b"udta", b"trak", b"mdia", b"minf", b"stbl", b"edts"}


def walk(d, start, end):
    d = bytes(d)
    off = start
    while off + 8 <= end:
        sz = struct.unpack(">I", d[off:off + 4])[0]
        ty = d[off + 4:off + 8]
        hdr = 8
        if sz == 1:
            sz = struct.unpack(">Q", d[off + 8:off + 16])[0]
            hdr = 16
        elif sz == 0:
            sz = end - off
        if sz < 8:
            return
        yield off, ty, sz, hdr
        off += sz


def find_all(d, start, end, typ, out=None):
    if out is None:
        out = []
    for off, t, sz, hdr in walk(d, start, end):
        if t == typ:
            out.append((off, sz, hdr))
        if t in CONTAINERS:
            find_all(d, off + hdr, off + sz, typ, out)
    return out


def find_one(d, start, end, typ):
    r = find_all(d, start, end, typ)
    return r[0] if r else None


def u32(d, o):
    return struct.unpack(">I", d[o:o + 4])[0]


def samples(d):
    moov = find_one(d, 0, len(d), b"moov")
    moff, msz, mhdr = moov
    traks = [(o, s) for o, t, s, h in walk(d, moff + mhdr, moff + msz) if t == b"trak"]
    toff, tsz = traks[0]
    s0, s1, _ = find_one(d, toff, toff + tsz, b"stbl")
    o = find_one(d, s0, s0 + s1, b"stsz")[0]
    size = u32(d, o)
    body = d[o + 8:o + size]
    ss, n = struct.unpack(">II", body[4:12])
    sizes = ([ss] * n) if ss else [u32(body, 12 + 4*i) for i in range(n)]
    stco = find_one(d, s0, s0 + s1, b"stco")
    co64 = find_one(d, s0, s0 + s1, b"co64")
    ob = (stco or co64)[0]
    ob_size = u32(d, ob)
    ob_body = d[ob + 8:ob + ob_size]
    nc = u32(ob_body, 4)
    if stco:
        chunk_off = [u32(ob_body, 8 + 4*i) for i in range(nc)]
    else:
        chunk_off = [struct.unpack(">Q", ob_body[8 + 8*i:16 + 8*i])[0] for i in range(nc)]
    o = find_one(d, s0, s0 + s1, b"stsc")[0]
    size = u32(d, o)
    body = d[o + 8:o + size]
    ns = u32(body, 4)
    stsc = [(u32(body, 8 + 12*i), u32(body, 12 + 12*i), u32(body, 16 + 12*i))
            for i in range(ns)]
    per_chunk = []
    for i, (first, spc, _d) in enumerate(stsc):
        last = stsc[i + 1][0] - 1 if i + 1 < len(stsc) else nc
        per_chunk.extend([spc] * (last - first + 1))
    offsets, si = [], 0
    for coff, cnt in zip(chunk_off, per_chunk):
        pos = coff
        for _ in range(cnt):
            if si >= len(sizes):
                break
            offsets.append(pos)
            pos += sizes[si]
            si += 1
    return offsets, sizes


class BR:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def u(self, n):
        v = 0
        for _ in range(n):
            if self.p >> 3 >= len(self.d):
                raise EOFError
            v = (v << 1) | ((self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def se(self):
        v = self.ue()
        return (v + 1) // 2 if v % 2 else -(v // 2)


def parse_slice(nal, log2_frame_num_minus4, poc_type, log2_poc_minus4):
    if len(nal) < 2:
        return None
    t = nal[0] & 0x1F
    if t not in (1, 2, 5):
        return None
    # remove emulation prevention bytes
    payload = nal[1:]
    out = bytearray()
    zeros = 0
    for b in payload:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    b = BR(bytes(out))
    try:
        first_mb = b.ue()
        slice_type = b.ue()
        b.ue()                       # pps id
        frame_num = b.u(log2_frame_num_minus4 + 4)
        field_pic = 0
        if poc_type == 0:
            poc_lsb = b.u(log2_poc_minus4 + 4)
        else:
            poc_lsb = None
        return dict(nal_type=t, first_mb=first_mb, slice_type=slice_type,
                    frame_num=frame_num, poc_lsb=poc_lsb)
    except EOFError:
        return None


def avcc_sps(d):
    moov = find_one(d, 0, len(d), b"moov")
    traks = [(o, s) for o, t, s, h in walk(d, moov[0] + 8, moov[0] + moov[1]) if t == b"trak"]
    toff, tsz = traks[0]
    s0, s1, _ = find_one(d, toff, toff + tsz, b"stbl")
    stsd = find_one(d, s0, s0 + s1, b"stsd")
    e = stsd[0] + 16
    esz = u32(d, e)
    c = e + 86
    while c + 8 <= e + esz:
        csz = u32(d, c)
        if d[c + 4:c + 8] == b"avcC":
            a = d[c + 8:c + csz]
            i = 5
            n = a[i] & 0x1F
            i += 1
            ln = struct.unpack(">H", a[i:i + 2])[0]
            i += 2
            return a[i:i + ln]
        c += csz
    return None


def sps_params(nal):
    b = BR(nal[1:])
    profile = b.u(8)
    b.u(8)
    b.u(8)
    b.ue()
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = b.ue()
        if chroma == 3:
            b.u(1)
        b.ue(); b.ue(); b.u(1)
        if b.u(1):
            n = 8 if chroma != 3 else 12
            for i in range(n):
                if b.u(1):
                    size = 16 if i < 6 else 64
                    last = nxt = 8
                    for _ in range(size):
                        if nxt:
                            nxt = (last + b.se() + 256) % 256
                        last = nxt if nxt else last
    lfn = b.ue()
    poc = b.ue()
    lpoc = b.ue() if poc == 0 else 0
    return lfn, poc, lpoc


for path in sys.argv[1:]:
    d = Path(path).read_bytes()
    offsets, sizes = samples(d)
    sps = avcc_sps(d)
    lfn, poc_type, lpoc = sps_params(sps)
    print("=" * 78)
    print(f"{Path(path).name}: {len(sizes)} samples  "
          f"(SPS: log2_max_frame_num_minus4={lfn}, poc_type={poc_type}, "
          f"log2_max_poc_lsb_minus4={lpoc})")
    print("=" * 78)
    seq = []
    for i, (off, sz) in enumerate(zip(offsets, sizes)):
        buf = d[off:off + sz]
        p = 0
        types = []
        info = None
        while p + 4 <= len(buf):
            ln = struct.unpack(">I", buf[p:p + 4])[0]
            if ln == 0 or p + 4 + ln > len(buf):
                break
            nal = buf[p + 4:p + 4 + ln]
            types.append(nal[0] & 0x1F)
            if info is None:
                info = parse_slice(nal, lfn, poc_type, lpoc)
            p += 4 + ln
        kind = "?"
        if info:
            st = info["slice_type"]
            # 7/4 = I, 5/0 = P, 6/3 = B  (mod 5)
            base = st % 5
            kind = {7: "I", 4: "SI", 5: "P", 0: "P", 6: "B", 3: "B", 1: "B", 2: "I"}.get(st, "?")
            if st in (7,):
                kind = "I"
            elif st in (5, 0):
                kind = "P"
            elif st in (6, 3, 1):
                kind = "B"
            elif st == 2:
                kind = "I"
            else:
                kind = "?"
        seq.append((kind, info["frame_num"] if info else None,
                    info["poc_lsb"] if info else None, types))
    # print compact
    print("frame types (first 60):")
    print("  " + " ".join(f"{k}" for k, _, _, _ in seq[:60]))
    print("frame_num (first 40):")
    print("  " + " ".join(str(f) for _, f, _, _ in seq[:40]))
    print("poc_lsb (first 40):")
    print("  " + " ".join(str(p) for _, _, p, _ in seq[:40]))
    from collections import Counter
    print("counts:", dict(Counter(k for k, _, _, _ in seq)))
    print("nal type sets:", dict(Counter(tuple(t) for _, _, _, t in seq)))
