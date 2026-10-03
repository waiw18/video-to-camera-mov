"""Determine the camera's B-pyramid structure from nal_ref_idc and POC.

Display order must be reproduced exactly, and that depends on the GOP's reference
hierarchy. Rather than guess, this reads the camera's own pictures and reports:

    index, NAL type, nal_ref_idc, slice_type, frame_num, poc_lsb

A non-reference B (nal_ref_idc 0) means a plain B; a reference B means a pyramid.
The POC values then give the exact display order to reproduce.
"""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import importlib.util
spec = importlib.util.spec_from_file_location("spsm", HERE / "sps.py")
spsm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(spsm)

_g = {}
p = HERE / ".allfit.py"
g = {"__file__": str(p), "__name__": "m"}
exec(compile(p.read_text(encoding="utf-8-sig")
             .replace('if __name__ == "__main__":\n    main()', ""), "allfit", "exec"), g)
_g.update(g)
video_slots = _g["video_slots"]

CONT = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


def find(d, s, e, t):
    d = bytes(d)
    off = s
    while off + 8 <= e:
        sz = struct.unpack(">I", d[off:off + 4])[0]
        ty = d[off + 4:off + 8]
        if sz < 8:
            return None
        if ty == t:
            return off, sz
        if ty in CONT:
            r = find(d, off + 8, off + sz, t)
            if r:
                return r
        off += sz
    return None


def poc_bits_of(d):
    moov = find(d, 0, len(d), b"moov")
    traks = []
    off, end = moov[0] + 8, moov[0] + moov[1]
    while off + 8 <= end:
        sz = struct.unpack(">I", d[off:off + 4])[0]
        if d[off + 4:off + 8] == b"trak":
            traks.append((off, sz))
        off += sz
    stbl = find(d, traks[0][0], traks[0][0] + traks[0][1], b"stbl")
    stsd = find(d, stbl[0], stbl[0] + stbl[1], b"stsd")
    e2 = stsd[0] + 16
    esz = struct.unpack(">I", d[e2:e2 + 4])[0]
    c = e2 + 86
    while c + 8 <= e2 + esz:
        csz = struct.unpack(">I", d[c:c + 4])[0]
        if d[c + 4:c + 8] == b"avcC":
            a = d[c + 8:c + csz]
            i = 5
            n = a[i] & 0x1F
            i += 1
            ln = struct.unpack(">H", a[i:i + 2])[0]
            i += 2
            sd = spsm.parse_sps(bytes(a[i:i + ln]))
            return sd["log2_max_pic_order_cnt_lsb_minus4"] + 4, sd
        c += csz
    return 5, {}


def main(path, count=32):
    d = Path(path).read_bytes()
    poc_bits, sd = poc_bits_of(d)
    offsets, sizes = video_slots(d)
    print(f"=== {Path(path).name}")
    print(f"  poc_lsb {poc_bits} bits, poc_type={sd.get('pic_order_cnt_type')} "
          f"ref={sd.get('max_num_ref_frames')}")
    print(f"  {'idx':>3} {'nal':>4} {'ref_idc':>7} {'slice_type':>10} "
          f"{'frame_num':>9} {'poc_lsb':>7}")
    rows = []
    for k in range(min(count, len(sizes))):
        buf = d[offsets[k]:offsets[k] + sizes[k]]
        q = 0
        while q + 4 <= len(buf):
            ln = struct.unpack(">I", buf[q:q + 4])[0]
            if ln == 0 or q + 4 + ln > len(buf):
                break
            nal = bytes(buf[q + 4:q + 4 + ln])
            t = nal[0] & 0x1F
            if t in (1, 2, 5):
                ref_idc = (nal[0] >> 5) & 3
                # minimal parse with the right width
                from sps import BR, unescape as un
                r = BR(un(nal[1:]))
                first_mb = r.ue()
                st = r.ue()
                pps = r.ue()
                fn = r.u(4)
                idr = r.ue() if t == 5 else None
                poc = 0
                if sd.get("pic_order_cnt_type") == 0:
                    poc = r.u(poc_bits)
                rows.append((k, t, ref_idc, st, fn, poc, idr))
                break
            q += 4 + ln
    for row in rows:
        k, t, ref_idc, st, fn, poc, idr = row
        print(f"  {k:>3} {t:>4} {ref_idc:>7} {st:>10} {fn:>9} {poc:>7}")
    # display order by POC
    order = sorted(range(len(rows)), key=lambda i: (rows[i][5], i))
    print(f"  display order by poc: {[rows[i][0] for i in order]}")
    # which B are references
    brefs = [r[0] for r in rows if r[1] == 1 and r[2] != 0]
    print(f"  reference B pictures at indices: {brefs[:12]}")


if __name__ == "__main__":
    main(str(HERE.parent / "cam-tests" / "DSC_8947-fresh-camera.mp4"))
    main(str(HERE.parent / "out.mp4"))
