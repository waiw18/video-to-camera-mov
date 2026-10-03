"""相机 GOP 结构与 x264/NVENC 输出的逐帧对比工具（只读，不修改任何文件）。

背景
    交接文档曾把相机流描述成"全 P 片"和"POC Z 序重排序"。按相机自己的 SPS 语义
    实际解析后，两者都不成立。本工具输出可复核的原始事实：

        相机解码序：I B B P B B P ...   slice_type = 2,1,1,0,1,1,0,...
        相机显示序：I B B P B B P ...   （= 解码序，逐帧递增，无重排）
        frame_num ：0 1 1 1 2 2 2 ...   （只有 P 片递增）
        poc_lsb   ：0 1 2 3 4 5 6 ...   （每个 GOP 从 0 递增到 29）

    x264 (bframes=2,b-adapt=0) 的解码序完全相同，但**显示序**是
        I P B B P B B ...  →  每个 P 显示在它那一对 B 之前
    这才是它与相机唯一的结构差异，也是相机播放第三方流抽搐的最可能原因。

    NVENC (h264_nvenc -bf 2) 的显示序恰好是相机的 I B B P B B，但 POC 用 2×显示序。

            python tools/camera-gop-census.py [文件...]
"""
import importlib.util
import struct
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from sps import BR, unescape, parse_sps

from binpath import FFMPEG, FFPROBE
CONTAINERS = {b"moov", b"udta", b"trak", b"mdia", b"minf", b"stbl", b"edts"}

# 相机 DSC_8947 实测模板（见文档）
CAMERA_TEMPLATE = [
    (5, 3, 2, 0, 0),
    (1, 0, 1, 1, 30), (1, 0, 1, 1, 31), (1, 2, 0, 1, 3),
    (1, 0, 1, 2, 1), (1, 0, 1, 2, 2), (1, 2, 0, 2, 6),
    (1, 0, 1, 3, 4), (1, 0, 1, 3, 5), (1, 2, 0, 3, 9),
    (1, 0, 1, 4, 7), (1, 0, 1, 4, 8), (1, 2, 0, 4, 12),
    (1, 0, 1, 5, 10), (1, 0, 1, 5, 11),
]


def walk(d, start, end):
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


def video_slots(d):
    """每个视频样本的 (偏移, 大小)，来自 stsz/stsc/stco|co64。"""
    moov = find_all(d, 0, len(d), b"moov")[0]
    trak = find_all(d, moov[0] + 8, moov[0] + moov[1], b"trak")[0]
    s0, s1, _ = find_all(d, trak[0], trak[0] + trak[1], b"stbl")[0]
    o = find_all(d, s0, s0 + s1, b"stsz")[0][0]
    size = struct.unpack(">I", d[o:o + 4])[0]
    body = d[o + 8:o + size]
    ss, n = struct.unpack(">II", body[4:12])
    sizes = ([ss] * n) if ss else [struct.unpack(">I", body[12 + 4 * i:16 + 4 * i])[0]
                                   for i in range(n)]
    stco = find_all(d, s0, s0 + s1, b"stco")
    co64 = find_all(d, s0, s0 + s1, b"co64")
    ob = (stco or co64)[0][0]
    ob_size = struct.unpack(">I", d[ob:ob + 4])[0]
    ob_body = d[ob + 8:ob + ob_size]
    nc = struct.unpack(">I", ob_body[4:8])[0]
    if stco:
        chunk_off = [struct.unpack(">I", ob_body[8 + 4 * i:12 + 4 * i])[0] for i in range(nc)]
    else:
        chunk_off = [struct.unpack(">Q", ob_body[8 + 8 * i:16 + 8 * i])[0] for i in range(nc)]
    o = find_all(d, s0, s0 + s1, b"stsc")[0][0]
    size = struct.unpack(">I", d[o:o + 4])[0]
    body = d[o + 8:o + size]
    ns = struct.unpack(">I", body[4:8])[0]
    stsc = [(struct.unpack(">I", body[8 + 12 * i:12 + 12 * i])[0],
             struct.unpack(">I", body[12 + 12 * i:16 + 12 * i])[0]) for i in range(ns)]
    per_chunk = []
    for i, (first, spc) in enumerate(stsc):
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


def avcc_sps(d):
    moov = find_all(d, 0, len(d), b"moov")[0]
    trak = find_all(d, moov[0] + 8, moov[0] + moov[1], b"trak")[0]
    stsd = find_all(d, trak[0], trak[0] + trak[1], b"stsd")[0]
    e2 = stsd[0] + 16
    esz = struct.unpack(">I", d[e2:e2 + 4])[0]
    c = e2 + 86
    while c + 8 <= e2 + esz:
        csz = struct.unpack(">I", d[c:c + 4])[0]
        if d[c + 4:c + 8] == b"avcC":
            a = d[c + 8:c + csz]
            i = 5
            i += 1
            ln = struct.unpack(">H", a[i:i + 2])[0]
            i += 2
            return bytes(a[i:i + ln])
        c += csz
    return b""


def pictures_from_mp4(path):
    d = Path(path).read_bytes()
    sps = avcc_sps(d)
    if not sps:
        return None, None, []
    sd = parse_sps(sps)
    offsets, sizes = video_slots(d)
    rows = []
    for k in range(len(sizes)):
        buf = d[offsets[k]:offsets[k] + sizes[k]]
        q = 0
        while q + 4 <= len(buf):
            ln = struct.unpack(">I", buf[q:q + 4])[0]
            if ln == 0 or q + 4 + ln > len(buf):
                break
            nal = bytes(buf[q + 4:q + 4 + ln])
            t = nal[0] & 0x1F
            if t in (1, 2, 5):
                rows.append((k,) + slice_fields(nal, sd))
                break
            q += 4 + ln
    return sd, sps, rows


def pictures_from_es(path):
    buf = Path(path).read_bytes()
    idx, i = [], buf.find(b"\x00\x00\x01")
    while i != -1:
        idx.append(i)
        i = buf.find(b"\x00\x00\x01", i + 3)
    nals = []
    for k, pos in enumerate(idx):
        st = pos + 3
        en = idx[k + 1] if k + 1 < len(idx) else len(buf)
        nal = buf[st:en]
        if k + 1 == len(idx):
            nal = nal.rstrip(b"\x00")
        if nal:
            nals.append(nal)
    sd = None
    rows = []
    for nal in nals:
        t = nal[0] & 0x1F
        if t == 7:
            sd = parse_sps(nal)
            continue
        if t in (1, 2, 5) and sd is not None:
            rows.append((-1,) + slice_fields(nal, sd))
    return sd, None, rows


def slice_fields(nal, sd):
    ref_idc = (nal[0] >> 5) & 3
    nt = nal[0] & 0x1F
    r = BR(unescape(nal[1:]))
    r.ue()
    st = r.ue()
    r.ue()
    fn = r.u(sd["log2_max_frame_num_minus4"] + 4)
    if nt == 5:
        r.ue()               # idr_pic_id (present for IDR pictures)
    poc = 0
    if sd["pic_order_cnt_type"] == 0:
        poc = r.u(sd["log2_max_pic_order_cnt_lsb_minus4"] + 4)
    return (nt, ref_idc, st, fn, poc)


def report(path):
    p = Path(path)
    if p.suffix.lower() in (".264", ".h264", ".avc"):
        sd, sps, rows = pictures_from_es(p)
    else:
        sd, sps, rows = pictures_from_mp4(p)
    if sd is None or not rows:
        print(f"=== {p.name}: cannot parse (no SPS or no slice)")
        return
    print(f"=== {p.name}")
    print(f"  SPS {len(sps) if sps else '?'}B  poc_type={sd['pic_order_cnt_type']} "
          f"poc_bits={sd['log2_max_pic_order_cnt_lsb_minus4'] + 4} "
          f"fn_bits={sd['log2_max_frame_num_minus4'] + 4} "
          f"ref={sd['max_num_ref_frames']}")
    print(f"  pictures={len(rows)}  slice_type={dict(Counter(r[3] for r in rows))} "
          f"(2=I, 0=P, 1=B)")
    print(f"  nal_ref_idc={dict(Counter(r[2] for r in rows))}")
    n = min(15, len(rows))
    print("  decode order (nt, ref_idc, slice_type, frame_num, poc):")
    for i in range(0, n, 3):
        print("   " + "  ".join(f"{rows[j][1:]}".ljust(26) for j in range(i, min(i + 3, n))))
    disp = sorted(range(n), key=lambda i: (rows[i][4], i))
    print(f"  display order slice_type = {[rows[i][3] for i in disp]}")
    print(f"  display order == decode order ? {disp == list(range(n))}")
    cam = [t[1:] for t in CAMERA_TEMPLATE[:n]]
    hit = sum(1 for i in range(min(len(cam), len(rows))) if cam[i] == rows[i][2:])
    print(f"  per-picture match with camera template: {hit}/{min(len(cam), len(rows))}")


def main(paths):
    if not paths:
        paths = [HERE.parent / "cam-tests" / "DSC_8947-fresh-camera.mp4"]
    for a in paths:
        report(a)
        print()


if __name__ == "__main__":
    main(sys.argv[1:])
