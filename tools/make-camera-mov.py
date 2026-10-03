"""把外部 H.265 码流打包成相机格式的 MOV。

定位
    camera-concat.py 只能拼相机自己的原片（要求参数集一致）。
    本工具处理"非相机码流"：把任意 Annex-B HEVC 码流装进相机那套 MOV 容器。

做法
    从模板 MOV（相机实拍）里**复用两条 trak 的骨架**——tkhd/mdhd/hdlr/minf/
    stsd 结构、以及音频轨的 lpcm 样本条目全部照抄，只替换：
      * 视频轨：stsd 里的 hvcC 换成码流自己的参数集；stsz/stsc/co64/stss 按新数据重算
      * 音频轨：stsz/stsc/co64/stts 按新数据重算（音频用静音 PCM，结构与模板一致）
    这样容器里除"样本数据与 hvcC"外都与相机原片同构。

相机 MOV 布局（实测）
    ftyp(24B, qt  + niko) | moov | free | mdat
    1080p -> mdat@655,360 ; 4K -> mdat@1,966,080（默认沿用模板的 mdat 位置）

用法
    python tools/make-camera-mov.py --es x.265 --out DSC_4500.MOV --template DSC_8951.MOV
"""
import argparse
import datetime
import importlib.util
import os
import re
import struct
import time
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from boxio import walk, children, find, track_list, box, u32   # noqa: E402

# hevc-ps.py 文件名带连字符，不能直接 import
_ps_spec = importlib.util.spec_from_file_location(
    "hevc_ps", Path(__file__).resolve().parent / "hevc-ps.py")
hevc_ps = importlib.util.module_from_spec(_ps_spec)
_ps_spec.loader.exec_module(hevc_ps)

from binpath import FFMPEG, FFPROBE
CAM_FTYP = (struct.pack(">I", 24) + b"ftyp" + b"qt  "
            + struct.pack(">I", 538315008) + b"qt  " + b"niko")


# ---------------------------------------------------------------- 码流

def split_annexb(buf):
    idx, i = [], buf.find(b"\x00\x00\x01")
    while i != -1:
        idx.append(i)
        i = buf.find(b"\x00\x00\x01", i + 3)
    out = []
    for k, pos in enumerate(idx):
        st = pos + 3
        en = idx[k + 1] if k + 1 < len(idx) else len(buf)
        nal = buf[st:en]
        if k + 1 == len(idx):
            nal = nal.rstrip(b"\x00")
        if nal:
            out.append(nal)
    return out


def to_aus(nals):
    aus, cur = [], []
    for n in nals:
        t = (n[0] >> 1) & 0x3F
        if t <= 31:
            if cur and any(((x[0] >> 1) & 0x3F) <= 31 for x in cur):
                aus.append(cur)
                cur = []
            cur.append(n)
        else:
            cur.append(n)
    if cur:
        aus.append(cur)
    return aus


def build_samples(nals):
    """相机样本结构 = AUD + 切片（参数集只放 hvcC）。"""
    out = []
    for au in to_aus(nals):
        vcl = [n for n in au if ((n[0] >> 1) & 0x3F) <= 31]
        if not vcl:
            continue
        idr = any(((n[0] >> 1) & 0x3F) in (19, 20, 21) for n in vcl)
        # 相机所有关键帧都是 IDR_W_RADL(19)，x265 封闭 GOP 给的是 IDR_N_LP(20)。
        # 两者都是 IDR、都会重置 DPB，切片头语法也完全相同（不存在"有无前导图像"
        # 的额外字段），所以改标签是安全的，只是让类型与相机一致。
        fixed_vcl = []
        for n in vcl:
            t = (n[0] >> 1) & 0x3F
            if t == 20:
                n = bytes([(n[0] & 0x81) | (19 << 1)]) + n[1:]
            fixed_vcl.append(n)
        vcl = fixed_vcl
        # AUD 载荷按 HEVC 规范：aud_pic_type u(3) 后是对齐位 1，再补 4 个 0
        #   I -> 0x10 / P -> 0x30 / B -> 0x50（实测相机 DSC_8955.MOV 就是 0x10、0x30）
        # 之前写成 (pic_type<<4)|0x08：对齐位错在 bit3，且 P 帧被标成 000(I)，
        # 相机播放器会卡在"无限加载"。
        # 本工具 bframes=0，所以非 IDR 必为 P。
        pic_type = 0 if idr else 1
        aud = bytes([0x46, 0x01, (pic_type << 5) | 0x10])
        out.append(b"".join(struct.pack(">I", len(n)) + n for n in [aud] + vcl))
    return out


def unescape_nal(b):
    """去掉 NAL 载荷里的 emulation prevention 字节（00 00 03）。"""
    out, i, zeros = bytearray(), 0, 0
    while i < len(b):
        if zeros >= 2 and b[i] == 3 and i + 1 < len(b) and b[i + 1] <= 3:
            zeros = 0
            i += 1
            continue
        zeros = zeros + 1 if b[i] == 0 else 0
        out.append(b[i])
        i += 1
    return bytes(out)


def escape_nal(b):
    """重新插入 emulation prevention 字节。"""
    out, zeros = bytearray(), 0
    for x in b:
        if zeros >= 2 and x <= 3:
            out.append(3)
            zeros = 0
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out)


def zero_constraint_flags(nal):
    """把 profile_tier_level 里的 constraint flags 首字节清零。

    相机所有文件的 PTL constraint flags 都是全 0（实测 DSC_8955 的 VPS/SPS）。
    hvcC 记录头里那 48 位我也写成了 0；如果码流里仍是 0xF0（progressive +
    frame_only），文件就自相矛盾——相机很可能校验这一点。

    定位方式：在去转义后的载荷里找 compat flags `60 00 00 00`，它后面那个字节
    就是 constraint flags 的首字节。
    """
    if len(nal) < 3:
        return nal
    payload = unescape_nal(nal[2:])
    p = payload.find(b"\x60\x00\x00\x00")
    if p < 0 or p + 4 >= len(payload):
        return nal
    cb = p + 4
    if payload[cb] == 0:
        return nal
    fixed = bytearray(payload)
    fixed[cb] = 0x00
    return nal[:2] + escape_nal(bytes(fixed))


def hvcc(vps, sps, pps, level=150, compat=0x60000000, cfr=1):
    """HEVCDecoderConfigurationRecord（23 字节记录头 + 3 个 NAL 数组）。

    23 字节头按相机实拍逐字节对齐（DSC_8955.MOV 实测）：
        01 21 60 00 00 00 | 00 00 00 00 00 00 | 96 f0 00 | fc fd f8 f8 | 00 00 4f | 03
        ^  ^  ^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^   ^^^^^^^^   ^^^^^^^^^^   ^^^^^^^   ^
        |  |  compat 32bit  constraint 48bit   lvl+seg+   chroma+bitdepth  cfr/  numOfArrays
        |  profile_space(2)|tier(1)|profile_idc(5) = 0x21 -> Main / High tier
        ver=1
    注意 profile_idc 必须写 1(Main)：写成 0 是"未指定"，相机会不认。
    """
    arrays = []
    for t, nal in ((32, vps), (33, sps), (34, pps)):
        arrays.append(bytes([0x80 | t]) + struct.pack(">H", 1) +
                      struct.pack(">H", len(nal)) + nal)
    hdr = (bytes([1, 0x21])                      # ver=1, tier=High, profile=Main
           + struct.pack(">I", compat)           # Main + Main10 兼容
           + bytes(6)                            # constraint flags 全 0（与相机一致）
           + bytes([level])                      # general_level_idc
           + bytes([0xF0, 0])                    # min_spatial_segmentation_idc
           + bytes([0xFC])                       # parallelismType
           + bytes([0xFD])                       # chromaFormat = 1 (4:2:0)
           + bytes([0xF8, 0xF8])                 # 8-bit
           + bytes([0, 0])                       # avgFrameRate
           + bytes([((cfr & 3) << 6) | (1 << 3) | (1 << 2) | 3, 3]))
    assert len(hdr) == 23, len(hdr)
    return hdr + b"".join(arrays)


def replace_stsd_codec(d, trak, hvc):
    """把视频 trak 的 stsd 里 hvcC 换成新的（保持其余字节）。"""
    stbl = find(d, trak[0], trak[0] + trak[2], b"stbl")
    stsd = find(d, stbl[0], stbl[0] + stbl[2], b"stsd")
    inner = stsd[0] + 16                      # 第一个 sample entry 的起点
    esz = u32(d, inner)
    # stsd 载荷 = version/flags(4) + entry_count(4) + 若干 sample entry
    head = bytes(d[stsd[0] + 8:inner])        # 8 字节，含 entry_count
    entry = bytearray(d[inner:inner + esz])   # entry 自身前 4 字节就是它的 size
    p = entry.find(b"hvcC")
    if p < 0:
        raise SystemExit("模板视频条目里没有 hvcC")
    old = struct.unpack(">I", bytes(entry[p - 4:p]))[0]
    nb = box(b"hvcC", hvc)
    entry[p - 4:p - 4 + old] = nb
    new_esz = esz + len(nb) - old
    struct.pack_into(">I", entry, 0, new_esz)
    tail = bytes(d[inner + esz:stsd[0] + stsd[2]])
    # 注意：entry 里已含 size 字段，不要再额外写一次（踩过）
    return box(b"stsd", head + bytes(entry) + tail)


def new_stbl(d, trak, sizes, chunks, per_chunk, idr, stsd_override, stts_delta,
             with_stss):
    stbl = find(d, trak[0], trak[0] + trak[2], b"stbl")
    ent, last = [], None
    for i, c in enumerate(per_chunk, start=1):
        if c != last:
            ent.append((i, c, 1))
            last = c
    parts = []
    for o, t, sz, hdr in children(d, stbl):
        if t == b"stsd":
            parts.append(stsd_override if stsd_override is not None
                         else bytes(d[o:o + sz]))
        elif t == b"stts":
            parts.append(box(b"stts", struct.pack(">II", 0, 1) +
                             struct.pack(">II", len(sizes), stts_delta)))
        elif t == b"stsc":
            pl = struct.pack(">I", 0) + struct.pack(">I", len(ent))
            for f, c, dd in ent:
                pl += struct.pack(">III", f, c, dd)
            parts.append(box(b"stsc", pl))
        elif t == b"stsz":
            if sizes and len(set(sizes)) == 1:
                parts.append(box(b"stsz", struct.pack(">III", 0, sizes[0], len(sizes))))
            else:
                parts.append(box(b"stsz", struct.pack(">III", 0, 0, len(sizes)) +
                                 b"".join(struct.pack(">I", x) for x in sizes)))
        elif t in (b"co64", b"stco"):
            parts.append(box(b"co64", struct.pack(">II", 0, len(chunks)) +
                             b"".join(struct.pack(">Q", x) for x in chunks)))
        elif t == b"stss":
            if with_stss and idr:
                parts.append(box(b"stss", struct.pack(">II", 0, len(idr)) +
                                 b"".join(struct.pack(">I", x) for x in idr)))
        elif t == b"ctts":
            # 我们码流无 B 帧、显示序=解码序，模板若有 ctts 需要清成全 0
            old = bytearray(d[o:o + sz])
            n = u32(old, 12)
            e = 20 if old[8] == 1 else 16
            for i in range(n):
                pp = e + 8 * i + 4
                if pp + 4 <= len(old):
                    struct.pack_into(">i", old, pp, 0)
            parts.append(bytes(old))
        else:
            parts.append(bytes(d[o:o + sz]))
    return box(b"stbl", b"".join(parts))


def replace_stbl(d, trak, new_stbl_bytes):
    parts = []
    for o, t, sz, hdr in children(d, trak):
        if t == b"mdia":
            mp = []
            for o2, t2, sz2, h2 in children(d, (o, t, sz, hdr)):
                if t2 == b"minf":
                    ip = []
                    for o3, t3, sz3, h3 in children(d, (o2, t2, sz2, h2)):
                        ip.append(new_stbl_bytes if t3 == b"stbl"
                                  else bytes(d[o3:o3 + sz3]))
                    mp.append(box(b"minf", b"".join(ip)))
                else:
                    mp.append(bytes(d[o2:o2 + sz2]))
            parts.append(box(b"mdia", b"".join(mp)))
        else:
            parts.append(bytes(d[o:o + sz]))
    return box(b"trak", b"".join(parts))


def patch_u32(buf, off, val):
    struct.pack_into(">I", buf, off, val)


def probe_size(es_path):
    """用 ffprobe 读码流分辨率。"""
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(es_path)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    parts = (r.stdout or "").strip().split(",")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return int(parts[0]), int(parts[1])
    return None


def set_geometry(trak_bytes, w, h):
    """把 trak 里声明的几何统一改成 w×h。

    两处都要改，否则容器声明与 SPS 不一致（模板是 4K、码流是 1080p 时会踩）：
      * tkhd 尾部 width/height（16.16 定点）
      * stsd 里视频 sample entry 的 width/height（16 位整数）
    """
    b = bytearray(trak_bytes)
    tkhd = find(b, 8, len(b), b"tkhd")
    if tkhd:
        ver = b[tkhd[0] + 8]
        base = tkhd[0] + 8 + (32 if ver == 1 else 24)
        # 从 tkhd 体尾部回推：width/height 是最后 8 字节
        struct.pack_into(">II", b, tkhd[0] + tkhd[2] - 8,
                         w << 16, h << 16)
    stsd = find(b, 8, len(b), b"stsd")
    if stsd:
        inner = stsd[0] + 16
        esz = u32(b, inner)
        if esz >= 28:
            struct.pack_into(">HH", b, inner + 24, w, h)
    return bytes(b)


def set_tkhd_dur(trak_bytes, dur):
    """tkhd.duration 是**影片时基**下的轨道时长。

    ★ 这个字段以前一直没改过 —— 模板里是模板那条片子的值（228228 = 3.8 s），
      于是相机显示/使用的时长就是错的。实测相机自己的文件里它与 mvhd 一致。
    """
    b = bytearray(trak_bytes)
    tk = find(b, 8, len(b), b"tkhd")
    if tk and b[tk[0] + 8] == 0:
        struct.pack_into(">I", b, tk[0] + 28, dur)
    return bytes(b)


# 相机只认它自己那套视频时基：1080p60 用 60000/1001、1080p30 用 30000/1001。
CAM_RATES = [
    ((24000, 1001), (24000, 1001)),
    ((30000, 1001), (30000, 1001)),
    ((60000, 1001), (60000, 1001)),
    ((24, 1), (24000, 1000)),
    ((25, 1), (25000, 1000)),
    ((30, 1), (30000, 1001)),      # 相机的 30p 模式其实是 29.97
    ((50, 1), (50000, 1000)),
    ((60, 1), (60000, 1001)),
]


def normalize_fps(num, den):
    """把输出帧率归一到相机的时基约定。

    ★ 实测踩坑：把 30 fps 写成 `30/1`（视频时基 = 30）时，相机会**退回默认的
      1080p60 模式**，四个症状同时出现：
          2 倍快进 / 时长显示错 / 音视频不齐 / 视频播不全。
      换成相机自己的写法 `30000/1001`（时基 30000，每样本 1001 tick）就正常。
      59.94 本来就用 60000/1001，所以一直没暴露这个问题。
    """
    for (n, d), out in CAM_RATES:
        if n / d and abs(num / den - n / d) / (n / d) < 0.002:
            return out
    f = max(1, 1000 // max(1, int(num)))
    return num * f, den * f


def _nctg_records(blob):
    """遍历 NCDT 里的 NCTG 记录，yield (tag, fmt, cnt, 值起点, 值字节数)。"""
    nctg = blob.find(b"NCTG")
    if nctg < 0:
        return
    p = nctg + 4
    while p + 8 <= len(blob):
        tag = struct.unpack(">I", bytes(blob[p:p + 4]))[0]
        fmt = struct.unpack(">H", bytes(blob[p + 4:p + 6]))[0]
        cnt = struct.unpack(">H", bytes(blob[p + 6:p + 8]))[0]
        unit = {2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4}.get(fmt, 1)
        nbytes = unit * cnt
        if tag == 0 or p + 8 + nbytes > len(blob):
            return
        yield tag, fmt, cnt, p + 8, nbytes
        p += 8 + nbytes


def read_ncdt_tz(ncdt):
    """读 NCTG 0x19 的时区字符串（如 '+08:00'）-> 秒偏移。"""
    for tag, fmt, cnt, off, nb in _nctg_records(ncdt):
        if tag == 0x19 and fmt == 2:
            s = bytes(ncdt[off:off + nb]).split(b"\x00")[0].decode("latin1")
            m = re.match(r"^([+-])(\d{2}):?(\d{2})", s)
            if m:
                sign = 1 if m.group(1) == "+" else -1
                return sign * (int(m.group(2)) * 3600 + int(m.group(3)) * 60)
    return 0


def patch_ncdt_times(ncdt, local_str):
    """把 NCDT/NCTG 的 0x11 / 0x12（拍摄时间，ASCII 'YYYY:MM:DD HH:MM:SS'）改掉。

    实测相机 DSC_8955.MOV：0x11 与 0x12 都是 '2026:10:01 18:29:31'（**本地时间**）。
    容器头里的 creation/modification 则是同一时刻的 UTC（比它早 8 小时）。
    两处必须一起改，否则自相矛盾。
    """
    b = bytearray(ncdt)
    want = local_str.encode("latin1")[:19]
    if len(want) < 19:
        want = want.ljust(19, b" ")
    hit = 0
    for tag, fmt, cnt, off, nb in _nctg_records(b):
        if tag in (0x11, 0x12) and fmt == 2 and nb >= 19:
            b[off:off + 19] = want
            hit += 1
    return bytes(b), hit


def set_box_times(box_bytes, mac):
    """把 mvhd / tkhd / mdhd 的 creation_time 与 modification_time 都设为 mac。

    两个字段都在 box 内偏移 12 / 16（version 0：4 字节 version/flags 之后）。
    """
    b = bytearray(box_bytes)
    typ = bytes(b[4:8])
    if typ in (b"mvhd", b"tkhd", b"mdhd") and b[8] == 0:
        struct.pack_into(">II", b, 12, mac, mac)
    return bytes(b)


def set_trak_times(trak_bytes, mac):
    """trak 里的 tkhd 与 mdhd 都要改。

    注意用递归的 find：mdhd 在 mdia 里面，walk 只遍历同级会漏掉（踩过）。
    """
    b = bytearray(trak_bytes)
    for typ in (b"tkhd", b"mdhd"):
        box = find(b, 8, len(b), typ)
        if box and b[box[0] + 8] == 0:
            struct.pack_into(">II", b, box[0] + 12, mac, mac)
    return bytes(b)


def local_to_mac(local_str, tz_seconds):
    """'YYYY:MM:DD HH:MM:SS'（本地时间）+ 时区偏移 -> QuickTime 纪元秒（UTC）。

    QuickTime/MOV 的时间起点是 1904-01-01。
    """
    dt = datetime.datetime.strptime(local_str[:19], "%Y:%m:%d %H:%M:%S")
    epoch1904 = datetime.datetime(1904, 1, 1)
    return int((dt - datetime.timedelta(seconds=tz_seconds) - epoch1904).total_seconds())


def rebuild_ncdt_previews(udta_bytes, thumbs):
    """用真实视频的截图替换 NCDT 里的预览图。

    NCDT 子盒里嵌着几张 JPEG（实测尺寸）：
        NCTH  160x120     （小图，播放列表用）
        NCVW  全分辨率     （1920x1080 或 4K，大预览）
        NCM1  640x360     （中图）
        NCM2  1920x1080   （仅 4K 文件有；1080p 是 8 字节空盒）
    模板这几张图是**模板那条片子**的画面，不改的话相机会拿别人的画面当缩略图。

    thumbs: {'NCTH': bytes, 'NCVW': bytes, 'NCM1': bytes}，只替换给出的项。
    子盒尺寸变化后要重建 NCDT；moov 变大由 free 盒吸收（mdat 位置不变）。
    """
    if not thumbs:
        return udta_bytes, 0
    u = bytearray(udta_bytes)
    ncdt = find(u, 8, len(u), b"NCDT")
    if not ncdt:
        return udta_bytes, 0
    off, size = ncdt[0], ncdt[2]
    parts, p, end, hit = [], off + 8, off + size, 0
    while p + 8 <= end:
        sz = struct.unpack(">I", bytes(u[p:p + 4]))[0]
        t = bytes(u[p + 4:p + 8])
        if sz < 8 or p + sz > end:
            break
        name = t.decode("latin1")
        if name in thumbs:
            parts.append(box(t, thumbs[name]))
            hit += 1
        else:
            parts.append(bytes(u[p:p + sz]))
        p += sz
    if not hit:
        return udta_bytes, 0
    new_ncdt = box(b"NCDT", b"".join(parts))
    # ★ 子盒尺寸变了，外层 udta 自己的 size 字段必须跟着重建。
    #   前两个补丁（帧数/时间）是等长改写，所以没暴露这一点；换缩略图会变长变短。
    #   不修的话 udta 声明的大小是旧的，整棵 moov 的解析会错位。
    kids, p = [], 8
    while p + 8 <= len(u):
        sz = struct.unpack(">I", bytes(u[p:p + 4]))[0]
        t = bytes(u[p + 4:p + 8])
        if sz < 8 or p + sz > len(u):
            break
        kids.append(new_ncdt if t == b"NCDT" else bytes(u[p:p + sz]))
        p += sz
    return box(b"udta", b"".join(kids)), hit


def patch_ncdt_framecount(ncdt, n_frames):
    """把 NCDT/NCTG 里的帧数字段改成实际帧数。

    NCTG 记录布局是 tag(4) | fmt(2) | cnt(2) | 值。
    实测 tag=0x13 是 [帧数, 0]（DSC_8951 是 144、DSC_8955 是 228，与各自帧数一致）。
    模板的 NCDT 描述的是模板那条片子，不改会与实际时长不符。
    """
    b = bytearray(ncdt)
    nctg = b.find(b"NCTG")
    if nctg < 0:
        return bytes(b), False
    start = nctg + 4
    p = start
    while p + 8 <= len(b):
        tag = struct.unpack(">I", bytes(b[p:p + 4]))[0]
        fmt = struct.unpack(">H", bytes(b[p + 4:p + 6]))[0]
        cnt = struct.unpack(">H", bytes(b[p + 6:p + 8]))[0]
        unit = {2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4}.get(fmt, 1)
        nbytes = unit * cnt
        if p + 8 + nbytes > len(b):
            break
        if tag == 0x13 and fmt == 4 and cnt >= 1:
            struct.pack_into(">I", b, p + 8, n_frames)
            return bytes(b), True
        p += 8 + nbytes
    return bytes(b), False


def set_mdhd_timescale(trak_bytes, ts):
    """视频 mdhd 的 timescale 要跟着输出帧率走。

    相机 1080p60 用 60000（每样本 1001 tick）；若输出改成 29.97，
    时基必须改成 30000，否则 stts/mdhd 的时长会差一倍。
    """
    b = bytearray(trak_bytes)
    md = find(b, 8, len(b), b"mdhd")
    if md and b[md[0] + 8] == 0:
        struct.pack_into(">I", b, md[0] + 20, ts)
    return bytes(b)


def patch_ncdt_fps(ncdt, num, den):
    """把 NCDT/NCTG 的 0x16/0x17（帧率，urational）改成实际帧率。"""
    b = bytearray(ncdt)
    hit = 0
    for tag, fmt, cnt, off, nb in _nctg_records(b):
        if tag in (0x16, 0x17) and fmt == 5 and nb >= 8:
            struct.pack_into(">II", b, off, num, den)
            hit += 1
    return bytes(b), hit


def set_mdhd_dur(trak_bytes, dur):
    b = bytearray(trak_bytes)
    mdia = find(b, 8, len(b), b"mdia")
    mdhd = find(b, mdia[0], mdia[0] + mdia[2], b"mdhd")
    patch_u32(b, mdhd[0] + 24, dur)
    return bytes(b)


def set_elst_dur(trak_bytes, dur):
    b = bytearray(trak_bytes)
    elst = find(b, 8, len(b), b"elst")
    if elst:
        patch_u32(b, elst[0] + 16, dur)
    return bytes(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--es", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--fps-num", type=int, default=1001)
    ap.add_argument("--mdat", type=int, default=None,
                    help="强制 mdat 位置（默认沿用模板）；1080p 相机 MOV 是 655360")
    ap.add_argument("--drop-udta", action="store_true",
                    help="不带模板的 udta/NCDT（用于让 moov 变小、mdat 能落在 1080p 位置）")
    ap.add_argument("--audio-pcm", default=None,
                    help="PCM 音频原始字节（s24le 48kHz 立体声）；不给则用静音")
    ap.add_argument("--time", default=None,
                    help="拍摄时间，格式 'YYYY:MM:DD HH:MM:SS'（本地时间）或 now")
    a = ap.parse_args()
    tl = a.time
    if tl and tl.lower() == "now":
        tl = datetime.datetime.now().strftime("%Y:%m:%d %H:%M:%S")
    build(a.es, a.out, a.template, mdat=a.mdat, drop_udta=a.drop_udta,
          audio_pcm=a.audio_pcm, time_local=tl)


def build(es_path, out_path, template, mdat=None, drop_udta=False, audio_pcm=None,
          align_flags=None, display_size=None, time_local=None, thumbs=None,
          fps=(60000, 1001), level_idc=None):
    _t0 = time.time()
    nals = split_annexb(Path(es_path).read_bytes())
    vps = next(n for n in nals if ((n[0] >> 1) & 0x3F) == 32)
    sps = next(n for n in nals if ((n[0] >> 1) & 0x3F) == 33)
    pps = next(n for n in nals if ((n[0] >> 1) & 0x3F) == 34)
    samples = build_samples(nals)
    idr = []
    for i, s in enumerate(samples):
        p, ok = 0, False
        while p + 4 <= len(s):
            ln = struct.unpack(">I", s[p:p + 4])[0]
            if ((s[p + 4] >> 1) & 0x3F) in (19, 20, 21):
                ok = True
                break
            p += 4 + ln
        if ok:
            idr.append(i + 1)
    n = len(samples)
    print(f"码流: VPS={len(vps)} SPS={len(sps)} PPS={len(pps)} 样本={n} IDR={len(idr)}")

    tmpl = Path(template).read_bytes()
    tops = list(walk(tmpl, 0, len(tmpl)))
    mdat_t = next(x for x in tops if x[1] == b"mdat")
    target_mdat = mdat or mdat_t[0]
    moov_t = find(tmpl, 0, len(tmpl), b"moov")
    traks_t = track_list(tmpl)
    vtra_t, atra_t = traks_t[0], traks_t[1]
    print(f"模板 {Path(template).name}: mdat@{target_mdat:,} moov={moov_t[2]:,}")

    # 时长：视频每样本 fps_den tick @fps_num（相机 1080p60 = 1001 @ 60000）
    # ★ 先归一到相机的时基约定，否则相机认不出帧率（见 normalize_fps 注释）
    fps = normalize_fps(*fps)
    fps_num, fps_den = fps
    print(f"  视频时基 {fps_num}/{fps_den}（每样本 {fps_den} tick @ {fps_num}）")
    dur_v = n * fps_den
    # 音频：PCM s24le 立体声 = 每样本 6 字节；每样本占 1 个采样点 @48000
    if audio_pcm:
        pcm = Path(audio_pcm).read_bytes()
        n_audio = len(pcm) // 6
    else:
        pcm = None
        n_audio = int(dur_v / fps_num * 48000)
    dur_a = n_audio

    _t_chunk = time.time()          # 上面是码流解析，下面是分块后装配
    # 分块
    # ★★ 相机的分块规则是「每 0.5 秒一块」，不是「每 30 帧一块」★★
    #   铁证来自相机自己的两个原片（每块都恰好 0.5005 s）：
    #     59.94 原片 DSC_8955: 视频 30 帧/chunk、音频 24,024 样本/chunk
    #     29.97 原片 DSC_8960: 视频 15 帧/chunk、音频 24,024 样本/chunk  ← 关键对照
    #   59.94 时 30 帧正好也是 0.5 秒，所以"每 30 帧"这个错误假设长期没暴露；
    #   一到 29.97 就全错（视频块长一倍、音频分块只覆盖一半）→ 相机播到一半跳出。
    #
    #   音频每块样本数由「视频块覆盖的时间」推出，两种帧率都是 24,024，与相机完全一致。
    VPC = max(1, round(fps_num / (2 * fps_den)))      # 0.5 秒对应多少帧
    v_per = [VPC] * (n // VPC)
    if n % VPC:
        v_per.append(n % VPC)
    a_per, prev, cum_v = [], 0, 0
    for k in range(len(v_per)):
        cum_v += v_per[k]
        cum = min(n_audio, int(cum_v * fps_den / fps_num * 48000 + 1e-9))
        a_per.append(max(0, cum - prev))
        prev = cum
    if prev < n_audio:
        a_per[-1] += n_audio - prev
    a_per = [x for x in a_per if x > 0]

    v_sizes = [len(s) for s in samples]
    a_sizes = [6] * n_audio
    pos = target_mdat + 16
    v_chunk, a_chunk = [], []
    vsi = asi = 0
    for k in range(max(len(v_per), len(a_per))):
        if k < len(v_per):
            v_chunk.append(pos)
            pos += sum(v_sizes[vsi:vsi + v_per[k]])
            vsi += v_per[k]
        if k < len(a_per):
            a_chunk.append(pos)
            pos += sum(a_sizes[asi:asi + a_per[k]])
            asi += a_per[k]

    # 视频 stsd 换 hvcC（参数集来自码流）
    # 按相机实测值对齐参数集：
    #   1) VPS/SPS 的 PTL constraint flags 清零（与 hvcC 记录头、与相机一致）
    #      注意用 hevc_ps.set_constraint_flags：它是**按语法解析** PTL 定位的，
    #      而原来的字节特征匹配只认 x265 的 0x60000000，认不出 NVENC 的 0x40000000。
    #   2) DPB / latency 改成相机值，并给 VPS 插入时序信息
    #   3) conformance_window / aspect_ratio / PPS 开关
    vps_f = hevc_ps.set_constraint_flags(vps)
    sps_f = hevc_ps.set_constraint_flags(sps)
    # 4) PTL 的 level 按输出帧率（1080p60=150 / 1080p30=123）。
    #    ★★ tier 分帧率对待（都有真机证据，不要凭推断统一）：
    #       59.94：**tier=0（Main）** —— DSC_4521 就是 tier=0 且真机能播+快进正常；
    #              改成 tier=1 的 DSC_4544 相机直接无效。
    #       29.97：**tier=1（High）** —— 没有任何 tier=0 的 29.97 成功先例，
    #              而相机自己的 30fps 原片 DSC_8960 是 tier=1；
    #              tier=0 的 DSC_4557 真机播不了。
    #   相机自己的 59.94 原片虽然也是 tier=1，但 59.94 已有 tier=0 的成功实证，
    #   所以 59.94 保持 0（"退回 21 版本"）。
    if level_idc is None:
        level_idc = 150 if fps[0] / fps[1] > 40 else 123
    _tier = 0 if fps[0] / fps[1] > 40 else 1
    vps_f = hevc_ps.set_ptl(vps_f, level_idc, tier=_tier)
    sps_f = hevc_ps.set_ptl(sps_f, level_idc, tier=_tier)
    pps_f = pps
    flags = dict(align_flags or {})
    try:
        vps_f = hevc_ps.align(vps_f, **flags)
        sps_f = hevc_ps.align(sps_f, **flags)
        pps_f = hevc_ps.align(pps_f, **flags)
        print(f"  参数集已按相机对齐：VPS {len(vps)}->{len(vps_f)} B，"
              f"SPS {len(sps)}->{len(sps_f)} B，PPS {len(pps)}->{len(pps_f)} B")
    except Exception as e:
        print(f"  ！参数集对齐失败，VPS/SPS 仅清零 constraint flags：{e}")
        vps_f, sps_f, pps_f = zero_constraint_flags(vps), zero_constraint_flags(sps), pps
    # ★ hvcC 里的 general_level_idc 必须与**流内** VPS/SPS 的 level 完全一致，
    #   否则相机不认（踩过：流内 150/Main、hvcC 150/High，矛盾）。
    #   相机实测：1080p60 -> 150(5.0)、1080p30 -> 123(4.1)，tier 一律 High。
    if level_idc is None:
        level_idc = 150 if fps[0] / fps[1] > 40 else 123
    v_stsd = replace_stsd_codec(tmpl, vtra_t, hvcc(vps_f, sps_f, pps_f, level=level_idc))

    v_stbl = new_stbl(tmpl, vtra_t, v_sizes, v_chunk, v_per, idr, v_stsd, fps_den, True)
    a_stbl = new_stbl(tmpl, atra_t, a_sizes, a_chunk, a_per, None, None, 1, False)

    # ★ mdhd 时基必须跟着输出帧率改（60000/1001 -> 60000；30000/1001 -> 30000），
    #   否则 stts/mdhd 报出来的时长会差一倍。
    # ★ elst 的 segment_duration 用的是**影片时基**（mvhd.timescale，相机是 60000），
    #   而 dur_v 是视频时基，两者不一定相同，必须换算。
    # ★ 影片时基与视频时基一致（相机就是这么写的），下面的 tkhd/elst 都用它。
    mts_movie = fps_num
    dur_v_movie = round(dur_v * mts_movie / fps_num)

    # ★★ tkhd.duration 必须**保持模板的原始值，绝对不要改**！★★
    #    真机证据（6 个能播的文件全是"没改过"的，所有改过的全部失败）：
    #      DSC_4450 / 4470 / 4496 / 4500 / 4512 / 4521
    #        视频 tkhd.duration 一律 228,228（= 模板 DSC_8955 的值），音频一律 228,220
    #        → 全部能播；其中 DSC_4521 快进越界时**相机正常夹紧**
    #      把 tkhd.duration 改成"正确值"的文件（4542/4545/4548/4554…）
    #        → 快进超过文件末尾时**相机不夹紧 → 卡死**
    #    决定性 diff：DSC_4521 vs DSC_4555 全文件**只差 6 个字节**（就是这两条
    #      tkhd.duration），行为随之翻转 → 相机拿它当"可播放长度"来夹紧快进目标。
    #    （早先我把它改成正确值，正是把一切搞坏的原因。）
    v_trak = set_elst_dur(
        set_mdhd_dur(set_mdhd_timescale(replace_stbl(tmpl, vtra_t, v_stbl), fps_num), dur_v),
        dur_v_movie)
    a_trak = set_elst_dur(
        set_mdhd_dur(replace_stbl(tmpl, atra_t, a_stbl), dur_a),
        round(dur_a * mts_movie / 48000))

    # 几何：模板与码流分辨率可能不同（例如模板 4K、码流 1080p），必须统一。
    # display_size 用于"编码尺寸 != 显示尺寸"：相机编 1920x1088，再用
    # conformance_window 裁成 1080，容器里应当写 1080。
    size = display_size or probe_size(es_path)
    if size:
        v_trak = set_geometry(v_trak, *size)
        print(f"  容器几何写为 {size[0]}x{size[1]}（模板 {Path(template).name}）")

    mvhd = find(tmpl, 0, len(tmpl), b"mvhd")
    mvhd_b = bytearray(tmpl[mvhd[0]:mvhd[0] + mvhd[2]])
    # ★ 相机把 mvhd.timescale 设成**视频轨的时基**：
    #   实测 59.94 原片是 60000、30fps 原片是 30000。模板是 60000，
    #   输出 29.97 时要一起改成 30000，否则影片时基与视频轨不一致。
    patch_u32(mvhd_b, 20, fps_num)
    # mvhd 要覆盖**最长的轨道**（源常见音频比视频略长，写小了会切掉音频尾部）。
    dur_movie = max(round(dur_v * fps_num / fps_num),
                    round(dur_a * fps_num / 48000))
    patch_u32(mvhd_b, 24, dur_movie)
    udta = find(tmpl, 0, len(tmpl), b"udta")
    parts = [bytes(mvhd_b), v_trak, a_trak]
    if udta and not drop_udta:
        u = bytes(tmpl[udta[0]:udta[0] + udta[2]])
        u, ok = patch_ncdt_framecount(u, n)
        print(f"  NCDT 帧数字段已改为 {n}" if ok else "  NCDT 里没找到帧数字段")
        if fps != (60000, 1001):
            u, fhit = patch_ncdt_fps(u, fps_num, fps_den)
            print(f"  NCDT 帧率已改为 {fps_num}/{fps_den}（命中 {fhit} 处）")
        if time_local:
            # 时间要改两套：NCDT 里是本地时间字符串，容器头里是同刻的 UTC。
            # 只改一套会自相矛盾（相机显示的时间取自 NCDT）。
            tz = read_ncdt_tz(u)
            mac = local_to_mac(time_local, tz)
            u, hit = patch_ncdt_times(u, time_local)
            parts[0] = set_box_times(parts[0], mac)
            v_trak = set_trak_times(v_trak, mac)
            a_trak = set_trak_times(a_trak, mac)
            parts[1], parts[2] = v_trak, a_trak
            print(f"  时间已改为 {time_local}（UTC{tz // 3600:+d}，"
                  f"NCDT 命中 {hit} 处 + 容器头）")
        if thumbs:
            u, thit = rebuild_ncdt_previews(u, thumbs)
            print(f"  缩略图已替换 {thit} 张（{', '.join(sorted(thumbs))}）"
                  if thit else "  缩略图替换失败：NCDT 里没找到对应子盒")
        parts.append(u)
    elif drop_udta:
        print("  已按要求丢弃模板 udta/NCDT（moov 会小很多）")
    body = b"".join(parts)

    out = bytearray(CAM_FTYP)
    out += box(b"moov", body)
    pad = target_mdat - len(out)
    if pad < 8:
        raise SystemExit(f"moov 到 {len(out)}，超过 {target_mdat}")
    out += struct.pack(">I", pad) + b"free" + b"\x00" * (pad - 8)
    assert len(out) == target_mdat

    inter = []
    vs0 = as0 = 0
    for k in range(max(len(v_per), len(a_per))):
        if k < len(v_per):
            # 用累计下标，别写 sum(v_per[:k])（那是 O(n^2)，长时间轴会卡死）
            inter.append(b"".join(samples[vs0:vs0 + v_per[k]]))
            vs0 += v_per[k]
        if k < len(a_per):
            cnt_a = a_per[k]
            if pcm is not None:
                inter.append(pcm[as0 * 6:(as0 + cnt_a) * 6])
            else:
                inter.append(bytes(6) * cnt_a)
            as0 += cnt_a
    payload = bytes(8) + b"".join(inter)
    out += struct.pack(">I", 8 + len(payload)) + b"mdat" + payload
    Path(out_path).write_bytes(bytes(out))
    _t_write = time.time()
    print(f"-> {out_path}: {len(out):,} B  mdat@{target_mdat:,}  "
          f"视频 {n} 帧 / 音频 {n_audio} 样本"
          f"{'（真实音频）' if pcm is not None else '（静音）'}")
    print(f"  [build] 码流解析+分块 {_t_chunk - _t0:.1f}s  "
          f"容器装配+写盘 {_t_write - _t_chunk:.1f}s")

    # 整体解码校验：这一步要把整片解一遍，长片很花时间（255 秒的片子软解要 55–72 s，
    # 占整条流水线一半以上）。所以分两层：
    #   ① 全程走 NVDEC（GPU 解码，快 2.6 倍）—— 验证容器结构与整片可解；
    #   ② 前 15 秒再走**软件**解码器 —— 软解语法检查更严，能抓住参数集/切片头问题。
    t_v = time.time()
    r = subprocess.run([FFMPEG, "-v", "error", "-hwaccel", "cuda", "-i", out_path,
                        "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    errs = [l for l in ((r.stdout or "") + (r.stderr or "")).splitlines() if l.strip()]
    r2 = subprocess.run([FFMPEG, "-v", "error", "-i", out_path, "-t", "15",
                         "-f", "null", "-"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    errs += [l for l in ((r2.stdout or "") + (r2.stderr or "")).splitlines() if l.strip()]
    print(f"ffmpeg -v error: {len(errs)} 行（全程 GPU + 前 15 s 软解）"
          f"  [build] 整体解码校验 {time.time() - t_v:.1f}s")
    for e in errs[:6]:
        print(f"  {e}")


if __name__ == "__main__":
    main()
