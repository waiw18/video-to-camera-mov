"""把多个相机原片拼成一个长视频（用相机自己的码流与容器，必然能播）。

为什么这条路必然可行
    不重新编码：视频样本、音频样本、参数集、NCDT 元数据全部取自相机原片，
    只重建"容器"这一层（样本表、chunk 偏移、时长）。相机解码器面对的是它自己
    产出的码流，所以不存在"第三方文件不被保证播放"的问题。

拼接成立的前提
    1. 所有输入的**编码与参数集必须一致**（脚本会校验 avcC/hvcC 指纹，不一致就拒绝）；
    2. 每段以自己的 IDR 开头（相机每个文件都是），解码器在 IDR 处重置，
       所以直接把样本依次接起来即可。

容器布局沿用相机自己的：
    MP4: ftyp(28) | moov | free | mdat @ 524,288
    MOV: ftyp(24) | moov | free | mdat @ 655,360
    mdat 载荷前 8 字节填充；视频/音频交错；co64 偏移

用法：
    python tools/camera-concat.py --out DSC_4420.MP4 DSC_8947.MP4 DSC_8948.MP4 ...
    python tools/camera-concat.py --out DSC_4421.MOV --mov DSC_8955.MOV DSC_8956.MOV
"""
import argparse
import hashlib
import struct
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from boxio import walk, children, find, box, u32, u64, track_list   # noqa: E402

from binpath import FFMPEG, FFPROBE

CAM_FTYP_MP4 = (struct.pack(">I", 28) + b"ftyp" + b"mp42" + struct.pack(">I", 1)
                + b"mp42" + b"avc1" + b"niko")
CAM_FTYP_MOV = (struct.pack(">I", 24) + b"ftyp" + b"qt  "
                + struct.pack(">I", 538315008) + b"qt  " + b"niko")
CAM_MDAT_MP4 = 524288
CAM_MDAT_MOV = 655360


def plan_track(d, trak):
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
    if not per:
        per = [cnt]
    return sizes, coffs, per


def extract(d, sizes, coffs, per):
    blobs, si = [], 0
    for coff, c in zip(coffs, per):
        pos = coff
        for _ in range(c):
            if si >= len(sizes):
                break
            blobs.append(bytes(d[pos:pos + sizes[si]]))
            pos += sizes[si]
            si += 1
    return blobs


def codec_hash(d, trak):
    stbl = find(d, trak[0], trak[0] + trak[2], b"stbl")
    stsd = find(d, stbl[0], stbl[0] + stbl[2], b"stsd")
    inner = stsd[0] + 16
    esz = u32(d, inner)
    entry = d[inner:inner + esz]
    for tag in (b"avcC", b"hvcC"):
        p = entry.find(tag)
        if p >= 0:
            ln = struct.unpack(">I", entry[p - 4:p])[0]
            rec = entry[p + 4:p - 4 + ln]
            return tag.decode(), hashlib.sha1(rec).hexdigest()[:10], bytes(
                struct.pack(">I", 8 + len(rec)) + tag + rec)
    raise SystemExit("找不到参数集记录")


def stss_of(d, trak):
    stbl = find(d, trak[0], trak[0] + trak[2], b"stbl")
    st = find(d, stbl[0], stbl[0] + stbl[2], b"stss")
    if not st:
        return None                      # 全同步
    bd = d[st[0] + 8:st[0] + st[2]]
    n = u32(bd, 4)
    return [u32(bd, 8 + 4 * i) for i in range(n)]


def au_sizes_duration(d, trak, n_samples):
    """音频总时长：从输入文件的 stts 推每个样本的时长常量。

    AAC：每样本 1024 个采样点（timescale 48000）；
    PCM：每样本 1 个采样点。用第一个样本的 stts delta 乘样本数即得。
    """
    stbl = find(d, trak[0], trak[0] + trak[2], b"stbl")
    stts = find(d, stbl[0], stbl[0] + stbl[2], b"stts")
    bd = d[stts[0] + 8:stts[0] + stts[2]]
    delta = u32(bd, 12)              # 第一条 (count, delta) 的 delta
    return delta * n_samples


def build_stsc(per_chunk):
    """按每个 chunk 的样本数生成 stsc 条目（行程编码）。

    stsc 是"从第几个 chunk 起每 chunk 有多少样本"。拼接后 chunk 布局变了，
    必须重建；照抄源文件的 stsc 会让解码器按错误的 chunk 边界取数据
    （症状：长度前缀对不上、Invalid NAL unit size）。
    """
    ent = []
    for i, c in enumerate(per_chunk, start=1):
        if ent and ent[-1][1] == c:
            continue
        ent.append([i, c, 1])
    return ent


def rebuild_stbl(d, trak, sizes, chunks, stss, per_chunk, codec_box=None):
    """重建 stbl：stsz / stsc / co64 / stss 全部按新布局重算。

    参数顺序容易传错，这里加一道断言：sizes 必须是"每个样本的字节数"列表。
    """
    if len(sizes) == 0:
        raise SystemExit("rebuild_stbl: sizes 为空")
    if len(sizes) < len(chunks):
        raise SystemExit(
            f"rebuild_stbl: sizes({len(sizes)}) 比 chunks({len(chunks)}) 还少，"
            f"参数大概传错了（应传 per-sample sizes）")
    stbl = find(d, trak[0], trak[0] + trak[2], b"stbl")
    ent = build_stsc(per_chunk)
    parts = []
    for o, t, sz, hdr in children(d, stbl):
        if t == b"stsz":
            # 样本大小全相同时用紧凑格式（sample_size + count），否则逐样本。
            # 音频 PCM 每样本恒为 6 字节，逐样本会写出 1.46 MB 的 stsz 把 moov 撑爆。
            if sizes and len(set(sizes)) == 1:
                # 紧凑格式：version/flags(4) + sample_size(4) + sample_count(4)
                # 少写 version/flags 会让整个 stsz 少 4 字节、字段整体前移，
                # 相机就数不出正确的样本数（踩过）。
                pl = struct.pack(">III", 0, sizes[0], len(sizes))
            else:
                pl = struct.pack(">III", 0, 0, len(sizes)) + \
                     b"".join(struct.pack(">I", x) for x in sizes)
            parts.append(box(b"stsz", pl))
        elif t == b"stsc":
            pl = struct.pack(">I", 0) + struct.pack(">I", len(ent))
            for first, cnt, desc in ent:
                pl += struct.pack(">III", first, cnt, desc)
            parts.append(box(b"stsc", pl))
        elif t in (b"co64", b"stco"):
            parts.append(box(b"co64", struct.pack(">II", 0, len(chunks)) +
                             b"".join(struct.pack(">Q", x) for x in chunks)))
        elif t == b"stss":
            if stss is None:
                parts.append(bytes(d[o:o + sz]))
            else:
                parts.append(box(b"stss", struct.pack(">II", 0, len(stss)) +
                                 b"".join(struct.pack(">I", x) for x in stss)))
        elif t == b"stts":
            # 保留原来的行程编码结构，只把总样本数落到第一条上。
            # （音频 PCM 有几十万个样本，若展开成逐样本会写出 200 万字节的 stts，
            #   把 moov 撑爆。）
            old = d[o + 8:o + sz]
            n_ent = u32(old, 4)
            segs = [(u32(old, 8 + 8 * i), u32(old, 12 + 8 * i)) for i in range(n_ent)]
            if not segs:
                segs = [(len(sizes), 1)]
            elif len(segs) == 1:
                segs = [(len(sizes), segs[0][1])]
            else:
                rest = sum(c for c, _dd in segs[1:])
                segs = [(len(sizes) - rest, segs[0][1])] + segs[1:]
            pl = struct.pack(">II", 0, len(segs))
            for c, dd in segs:
                pl += struct.pack(">II", c, dd)
            parts.append(box(b"stts", pl))
        elif t == b"stsd" and codec_box is not None:
            # stsd 载荷 = version/flags(4) + entry_count(4) + 若干个 sample entry
            inner = o + 16                      # 第一个 entry 的起点
            esz = u32(d, inner)
            head = bytes(d[o + 8:inner - 4])    # version/flags + entry_count
            entry = bytearray(d[inner:inner + esz])
            new_esz = esz
            for tag in (b"avcC", b"hvcC"):
                p = entry.find(tag)
                if p >= 0:
                    old_len = struct.unpack(">I", bytes(entry[p - 4:p]))[0]
                    entry[p - 4:p - 4 + old_len] = codec_box
                    new_esz = esz + len(codec_box) - old_len
                    struct.pack_into(">I", entry, 0, new_esz)
                    break
            tail = bytes(d[inner + esz:o + sz])  # 该 entry 之后的其余 entry
            parts.append(box(b"stsd", head + struct.pack(">I", new_esz) +
                             bytes(entry) + tail))
        else:
            parts.append(bytes(d[o:o + sz]))
    return box(b"stbl", b"".join(parts))


def rebuild_trak(d, trak, new_stbl):
    parts = []
    for o, t, sz, hdr in children(d, trak):
        if t == b"mdia":
            mparts = []
            for o2, t2, sz2, h2 in children(d, (o, t, sz, hdr)):
                if t2 == b"minf":
                    iparts = []
                    for o3, t3, sz3, h3 in children(d, (o2, t2, sz2, h2)):
                        iparts.append(new_stbl if t3 == b"stbl"
                                      else bytes(d[o3:o3 + sz3]))
                    mparts.append(box(b"minf", b"".join(iparts)))
                else:
                    mparts.append(bytes(d[o2:o2 + sz2]))
            parts.append(box(b"mdia", b"".join(mparts)))
        else:
            parts.append(bytes(d[o:o + sz]))
    return box(b"trak", b"".join(parts))


def fix_mvhd_duration(mvhd_bytes, new_dur, timescale_old, timescale_new):
    b = bytearray(mvhd_bytes)
    ver = b[8]
    if ver == 0:
        old_ts = u32(b, 20)
        struct.pack_into(">I", b, 20, timescale_new)
        struct.pack_into(">I", b, 24, new_dur)
        # rate/volume 等不动
    else:
        old_ts = struct.unpack(">I", b[28:32])[0]
        struct.pack_into(">I", b, 28, timescale_new)
        struct.pack_into(">Q", b, 32, new_dur)
    return bytes(b)


def fix_mdhd_duration(mdhd_bytes, new_dur):
    b = bytearray(mdhd_bytes)
    ver = b[8]
    if ver == 0:
        struct.pack_into(">I", b, 24, new_dur)
    else:
        struct.pack_into(">Q", b, 32, new_dur)
    return bytes(b)


def fix_elst_duration(elst_bytes, seg_dur):
    """延长 edit list 的 segment_duration。

    相机的 elst 里 seg_dur 就是整段媒体时长；拼接后必须同步改大，
    否则播放器（含相机）按老时长截断，只能播前几秒。
    """
    b = bytearray(elst_bytes)
    ver = b[8]
    n = struct.unpack(">I", b[12:16])[0]
    if n < 1:
        return bytes(b)
    struct.pack_into(">I", b, 16, seg_dur)          # 第一条的 segment_duration
    return bytes(b)


def patch_box_bytes(blob, box_off, box_size, typ, new_bytes):
    """在独立缓冲 blob（从 0 开始）的某一层找第一个 typ 并替换，返回新的子 box 序列。

    用于在"已重建过的 trak 字节"上继续改字段（mdhd / elst），而不会丢掉样本表。
    """
    out, off, end = [], box_off, box_off + box_size
    while off + 8 <= end:
        sz = struct.unpack(">I", blob[off:off + 4])[0]
        t = bytes(blob[off + 4:off + 8])
        hdr = 8
        if sz == 1:
            sz = struct.unpack(">Q", blob[off + 8:off + 16])[0]
            hdr = 16
        elif sz == 0:
            sz = end - off
        if sz < 8 or off + sz > end:
            break
        if t == typ:
            out.append(new_bytes)
        elif t in (b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta"):
            inner = patch_box_bytes(blob, off + hdr, sz - hdr, typ, new_bytes)
            out.append(struct.pack(">I", 8 + len(inner)) + t + inner)
        else:
            out.append(bytes(blob[off:off + sz]))
        off += sz
    return b"".join(out)


def replace_in_trak(d, trak, typ, new_bytes, base_trak=None):
    """把 trak 子树里第一个 typ box 换成 new_bytes。

    base_trak 给定时以它为源（已重建过的完整 trak 字节，从 0 开始）。
    """
    if base_trak is None:
        blob = bytes(d[trak[0]:trak[0] + trak[2]])
    else:
        blob = bytes(base_trak)
    hdr = 16 if struct.unpack(">I", blob[0:4])[0] == 1 else 8
    inner = patch_box_bytes(blob, hdr, len(blob) - hdr, typ, new_bytes)
    return box(b"trak", inner)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mov", action="store_true", help="输出 MOV 布局（否则 MP4）")
    ap.add_argument("--target-mdat", type=int, default=None)
    a = ap.parse_args()

    ftyp = CAM_FTYP_MOV if a.mov else CAM_FTYP_MP4

    # --- 参考骨架（第一个文件）---
    # mdat 位置沿用参考文件自己的：相机按分辨率/格式用了不同对齐
    #   MP4 1080p -> 524,288 ; MOV 1080p -> 655,360 ; MOV 4K -> 1,966,080
    # 写死一个值会让 4K 或 MOV 误报"moov 超过目标位置"。
    d0 = Path(a.inputs[0]).read_bytes()
    moov0 = find(d0, 0, len(d0), b"moov")
    traks0 = track_list(d0)
    vtra, atra = traks0[0], traks0[1]
    ref_mdat = next(x[0] for x in walk(d0, 0, len(d0)) if x[1] == b"mdat")
    target_mdat = a.target_mdat or ref_mdat
    print(f"  参考 {Path(a.inputs[0]).name}: mdat@{ref_mdat:,} -> 输出沿用 {target_mdat:,}")

    # --- 读所有输入，校验参数集一致 ---
    v_sizes, v_blobs, v_stss = [], [], []
    au_sizes, au_blobs = [], []
    base = None
    codec_box = None
    off_v = 0
    ref_dur_v, ref_dur_a = 0, 0          # 各源文件自己声明时长的累加
    ts_v = ts_v_frame = ts_a = a_sub = ts_movie = 0
    for i, p in enumerate(a.inputs):
        f = Path(p)
        d = f.read_bytes()
        moov = find(d, 0, len(d), b"moov")
        traks = track_list(d)

        # 累加参考时长：相机的口径不统一（8947 是 帧数×1001，8951 是 帧数×1001+1001），
        # 所以不能自己算，只能沿用每个源文件自己的值相加。
        mv = find(d, 0, len(d), b"mvhd")
        ref_dur_v += u32(d, mv[0] + 24)
        ts_movie = u32(d, mv[0] + 20)
        mdia_a = find(d, traks[1][0], traks[1][0] + traks[1][2], b"mdia")
        mdhd_a = find(d, mdia_a[0], mdia_a[0] + mdia_a[2], b"mdhd")
        ref_dur_a += u32(d, mdhd_a[0] + 24)
        if len(traks) < 2:
            raise SystemExit(f"{f.name}: 只有 {len(traks)} 条 trak")
        tag, h, cb = codec_hash(d, traks[0])
        if base is None:
            base, codec_box = (tag, h), cb
        elif base != (tag, h):
            raise SystemExit(
                f"{f.name} 的参数集指纹是 ({tag},{h})，与第一个文件 {base} 不一致，"
                f"不能直接拼。请按参数集分组后再拼。")
        vs, vc, vp = plan_track(d, traks[0])
        vb = extract(d, vs, vc, vp)
        stss = stss_of(d, traks[0])
        if stss is not None:
            v_stss.extend(off_v + x for x in stss)
        else:
            v_stss.extend(off_v + k + 1 for k in range(len(vs)))
        off_v += len(vs)
        v_sizes.extend(len(b) for b in vb)
        v_blobs.extend(vb)
        asz, ac, ap = plan_track(d, traks[1])
        ab = extract(d, asz, ac, ap)
        au_sizes.extend(len(b) for b in ab)
        au_blobs.extend(ab)
        # 音频分块粒度沿用参考文件的第一个 chunk（相机 MOV 是 24024 个 PCM 帧/chunk）
        if i == 0 and ap:
            a_seg_ref = ap[0]
        # 参考文件的时基信息：视频每帧时长、音频 timescale、每个音频样本的采样点数
        if i == 0:
            mv_ = find(d, 0, len(d), b"mvhd")
            ts_v = u32(d, mv_[0] + 20)
            mdia_v_ = find(d, traks[0][0], traks[0][0] + traks[0][2], b"mdia")
            mdhd_v_ = find(d, mdia_v_[0], mdia_v_[0] + mdia_v_[2], b"mdhd")
            stbl_v_ = find(d, traks[0][0], traks[0][0] + traks[0][2], b"stbl")
            stts_v_ = find(d, stbl_v_[0], stbl_v_[0] + stbl_v_[2], b"stts")
            ts_v_frame = u32(d, stts_v_[0] + 20)          # 第一条 stts 的 delta
            mdia_a_ = find(d, traks[1][0], traks[1][0] + traks[1][2], b"mdia")
            mdhd_a_ = find(d, mdia_a_[0], mdia_a_[0] + mdia_a_[2], b"mdhd")
            ts_a = u32(d, mdhd_a_[0] + 20)
            stbl_a_ = find(d, traks[1][0], traks[1][0] + traks[1][2], b"stbl")
            stts_a_ = find(d, stbl_a_[0], stbl_a_[0] + stbl_a_[2], b"stts")
            a_sub = u32(d, stts_a_[0] + 20)               # 音频每样本采样点数
        print(f"  + {f.name}: 视频 {len(vs)} 帧 / 音频 {len(asz)} 样本")

    print(f"  参数集: {base[0]} {base[1]} 一致 ✓")
    print(f"  合计: 视频 {len(v_sizes)} 帧 ({sum(v_sizes):,} B)，"
          f"音频 {len(au_sizes)} 样本 ({sum(au_sizes):,} B)")

    # --- 分块：视频每 30 个样本一块，音频与其交错 ---
    VPC = 30
    v_per = [VPC] * (len(v_sizes) // VPC)
    if len(v_sizes) % VPC:
        v_per.append(len(v_sizes) % VPC)
    # --- 音频分块：按"每个视频 chunk 结束时刻应有的音频帧数"推 ---
    # 相机就是这么切的（实测 DSC_8947.MP4 得到 23,23,24,23,24,23,...；
    # DSC_8951.MOV 得到 24024,...）。均分或固定值都会与相机不一致。
    a_per = []
    if au_sizes:
        v_frame_dur = ts_v_frame                       # 每帧视频 tick
        a_frame_pts = 1024 if a_sub == 1024 else 1     # 每个音频样本占多少采样点
        a_ts = ts_a
        prev_cum = 0
        for k in range(len(v_per)):
            frames_done = sum(v_per[:k + 1])           # 视频累计帧数
            t_end = frames_done * v_frame_dur          # 60000 基
            cum = min(len(au_sizes),
                      int(t_end / ts_v * a_ts / a_frame_pts + 1e-9))
            a_per.append(max(0, cum - prev_cum))
            prev_cum = cum
        if prev_cum < len(au_sizes):                   # 余下的归最后一块
            a_per[-1] += len(au_sizes) - prev_cum
        a_per = [x for x in a_per if x > 0]

    # --- 计算 chunk 偏移（视频块与音频块按偏移顺序交错，两条轨的块数可以不同）---
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
            pos += sum(au_sizes[asi:asi + a_per[k]])
            asi += a_per[k]


    v_stbl = rebuild_stbl(d0, vtra, v_sizes, v_chunk, v_stss, v_per, None)
    # 参数顺序是 (d, trak, sizes, chunks, stss, per_chunk, codec_box)：
    # 音频没有 stss，第五个必须显式传 None，否则 chunk 偏移会被当成样本表（踩过）。
    a_stbl = rebuild_stbl(d0, atra, au_sizes, a_chunk, None, a_per, None)
    v_trak = rebuild_trak(d0, vtra, v_stbl)
    a_trak = rebuild_trak(d0, atra, a_stbl)

    # 时长
    mdia_v = find(d0, vtra[0], vtra[0] + vtra[2], b"mdia")
    mdhd_v = find(d0, mdia_v[0], mdia_v[0] + mdia_v[2], b"mdhd")
    ts_v = u32(d0, mdhd_v[0] + 20)
    mdia_a = find(d0, atra[0], atra[0] + atra[2], b"mdia")
    mdhd_a = find(d0, mdia_a[0], mdia_a[0] + mdia_a[2], b"mdhd")

    mdhd_v_box = bytes(d0[mdhd_v[0]:mdhd_v[0] + mdhd_v[2]])
    mdhd_a_box = bytes(d0[mdhd_a[0]:mdhd_a[0] + mdhd_a[2]])
    dur_v = ref_dur_v
    # 音频轨时长：相机写的是「音频总采样点数 ÷ 48000 × 音频 timescale」
    # （DSC_8947: 271360 采样 -> 271360/48000*60000 = 339,200 tick）。
    # 直接用 样本数×每样本采样点数 会小 25,600（踩过）。
    dur_a = (len(au_sizes) * a_sub) * ts_a // 48000
    # 注意：必须在上一步重建出的 v_trak/a_trak 上继续改 mdhd，
    # 若再从 d0 重建一次就会把新样本表覆盖掉（踩过一次）。
    v_trak = replace_in_trak(d0, vtra, b"mdhd", fix_mdhd_duration(mdhd_v_box, dur_v),
                             base_trak=v_trak)
    a_trak = replace_in_trak(d0, atra, b"mdhd", fix_mdhd_duration(mdhd_a_box, dur_a),
                             base_trak=a_trak)

    # edit list 也要延长，否则按老时长截断播放（注意要把结果接回 v_trak/a_trak）
    # elst 的 segment_duration 用 **movie timescale**（mvhd.timescale），
    # 不是轨道自己的 mdhd.timescale —— 音频两者不同（48000 vs 60000），
    # 用轨道值写会小成 271360（踩过）。
    elst_v = dur_v * ts_movie // ts_v if ts_v else dur_v
    elst_a = dur_a * ts_movie // ts_a if ts_a else dur_a
    for tr, seg, which in ((vtra, elst_v, 0), (atra, elst_a, 1)):
        edts = find(d0, tr[0], tr[0] + tr[2], b"edts")
        elst = find(d0, edts[0], edts[0] + edts[2], b"elst") if edts else None
        if not elst:
            continue
        new_elst = fix_elst_duration(bytes(d0[elst[0]:elst[0] + elst[2]]), seg)
        if which == 0:
            v_trak = replace_in_trak(d0, tr, b"elst", new_elst, base_trak=v_trak)
        else:
            a_trak = replace_in_trak(d0, tr, b"elst", new_elst, base_trak=a_trak)

    mvhd = find(d0, 0, len(d0), b"mvhd")
    # mvhd（movie header）时长取两条轨换算到 movie timescale 后的最大值，
    # 直接写视频轨值会偏小（踩过）。
    mv_dur = max(dur_v * ts_movie // ts_v if ts_v else dur_v,
                 dur_a * ts_movie // ts_a if ts_a else dur_a)
    mvhd_box = fix_mvhd_duration(bytes(d0[mvhd[0]:mvhd[0] + mvhd[2]]),
                                 mv_dur, None, ts_v)

    parts = []
    for o, t, sz, hdr in children(d0, moov0):
        if t == b"mvhd":
            parts.append(mvhd_box)
        elif t == b"trak" and o == vtra[0]:
            parts.append(v_trak)
        elif t == b"trak" and o == atra[0]:
            parts.append(a_trak)
        else:
            parts.append(bytes(d0[o:o + sz]))
    body = b"".join(parts)

    # 自检：组装后的 moov 里，时长相关字段是否都已经是新值
    chk_mvhd = find(body, 0, len(body), b"mvhd")
    chk_traks = [x for x in walk(body, 0, len(body)) if x[1] == b"trak"]
    chk_e = find(body, chk_traks[0][0], chk_traks[0][0] + chk_traks[0][2], b"elst")
    chk_m = find(body, chk_traks[0][0], chk_traks[0][0] + chk_traks[0][2], b"mdhd")
    print(f"  自检: mvhd.dur={u32(body, chk_mvhd[0] + 24)} "
          f"trak0.mdhd.dur={u32(body, chk_m[0] + 24)} "
          f"trak0.elst.seg_dur={u32(body, chk_e[0] + 16)} "
          f"（期望 {dur_v}）")

    out = bytearray()
    out += ftyp
    out += box(b"moov", body)
    pad = target_mdat - len(out)
    if pad < 8:
        raise SystemExit(f"moov 到 {len(out)}，超过 {target_mdat}")
    out += struct.pack(">I", pad) + b"free" + b"\x00" * (pad - 8)
    assert len(out) == target_mdat, len(out)

    # 载荷交错
    vchunks, i = [], 0
    for c in v_per:
        vchunks.append(b"".join(v_blobs[i:i + c]))
        i += c
    achunks, i = [], 0
    for c in a_per:
        achunks.append(b"".join(au_blobs[i:i + c]))
        i += c
    inter = []
    for k in range(max(len(vchunks), len(achunks))):
        if k < len(vchunks):
            inter.append(vchunks[k])
        if k < len(achunks):
            inter.append(achunks[k])
    payload = bytes(8) + b"".join(inter)
    out += struct.pack(">I", 8 + len(payload)) + b"mdat" + payload
    Path(a.out).write_bytes(bytes(out))
    print(f"  -> {a.out}: {len(out):,} B  mdat@{target_mdat:,}")

    r = subprocess.run([FFMPEG, "-v", "error", "-i", a.out, "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    errs = [l for l in ((r.stdout or "") + (r.stderr or "")).splitlines() if l.strip()]
    print(f"  ffmpeg -v error: {len(errs)} 行")
    for e in errs[:5]:
        print(f"    {e}")


if __name__ == "__main__":
    main()
