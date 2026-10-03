"""一键校验转换结果：容器结构、参数集、缩略图、时长自洽。

用法
    python tools/verify-output.py 输出.MOV --template cam-tests/DSC_8955.MOV

全部检查都基于实测结论（见 MOV转换指南.md 与 码流字段对齐-实测.md）。
"""
import argparse
import datetime
import importlib.util
import re
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

from boxio import find, track_list, u32, children          # noqa: E402

_s = importlib.util.spec_from_file_location("cj", HERE / "camera-jpeg.py")
cj = importlib.util.module_from_spec(_s)
_s.loader.exec_module(cj)

from binpath import FFMPEG, FFPROBE
FFPROBE = FFMPEG.replace("ffmpeg.exe", "ffprobe.exe")

OK, BAD = [], []


def check(cond, name, detail=""):
    (OK if cond else BAD).append(name)
    print(f"  [{'OK' if cond else '!!'}] {name}" + (f"  {detail}" if detail else ""))


def hvcc_ptl(d):
    """读 stsd/hvcC 里的 PTL，返回 (general_level_idc 字符串, tier_flag 字符串)。

    hvcC 记录体布局：
        configurationVersion(1)
        profile_space(2)|tier_flag(1)|profile_idc(5)   <- 体+1
        profile_compatibility_flags(4)
        constraint_indicator_flags(6)
        general_level_idc(1)                            <- 体+12
    ★ 这个必须与流内 VPS/SPS 的 PTL 一致，否则相机不认（踩过）。
    """
    # 注意：hvcC 在 stsd/stsd 里，boxio.find 的容器白名单不含 stsd，
    # 所以这里直接在原始字节里找类型标记。
    i = d.find(b"hvcC")
    if i < 0:
        return None
    body = i + 4
    tier = str((d[body + 1] >> 5) & 1)
    return str(d[body + 12]), tier


def decode_errors(path):
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return len([l for l in ((r.stdout or "") + (r.stderr or "")).splitlines() if l.strip()])


def trace_fields(path, frames=3):
    """trace_headers 取字段。

    ★ frames=3：连 P 帧一起 trace，同名取**最后一次**出现。
      只 trace 第 0 帧（IDR）会漏掉 P 帧切片头与 PPS 级开关的差异
      —— GPU(NVENC) 那版就是这么漏过去的：57 个字段不同，其中有
      num_negative_pics=4、num_ref_idx_active_override_flag=1、
      cabac_init_present_flag=1、deblocking_filter_control_present_flag=1，
      还有整套 HRD 参数，相机全都不认。
    """
    r = subprocess.run([FFMPEG, "-v", "trace", "-i", str(path), "-c", "copy",
                        "-bsf:v", "trace_headers", "-frames:v", str(frames),
                        "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    got = {}
    pat = re.compile(r"^\s*(\d+)\s+(\S+?)\s+(\S+)\s*=\s*(.+?)\s*$")
    for raw in (r.stderr or "").splitlines():
        s = raw.split("] ", 1)[-1] if "] " in raw else raw
        m = pat.match(s)
        if m:
            got[m.group(2)] = m.group(4)          # 后出现的覆盖前面的
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("output")
    ap.add_argument("--template", required=True)
    a = ap.parse_args()
    out, tmpl = Path(a.output), Path(a.template)
    if not out.exists():
        raise SystemExit(f"找不到 {out}")

    d = out.read_bytes()
    t = tmpl.read_bytes()
    print(f"=== 校验 {out.name}（模板 {tmpl.name}）")

    # ---- 1) 顶层布局 ----
    print("\n[1] 顶层布局")
    ftyp = struct.unpack(">I", d[0:4])[0]
    check(d[4:8] == b"ftyp", "ftyp 在最前", f"type={d[4:8]!r}")
    check(d[8:12] == bytes(t[8:12]), "ftyp 品牌与模板一致", f"{d[8:12]!r}")
    moov = find(d, 0, len(d), b"moov")
    check(bool(moov), "moov 存在")
    mdat = d.find(b"mdat", 4) - 4
    t_mdat = t.find(b"mdat", 4) - 4
    check(mdat == t_mdat, "mdat 位置与模板一致", f"{mdat:,} vs {t_mdat:,}")

    # ---- 2) 轨道结构 ----
    print("\n[2] 轨道结构")
    trs = track_list(d)
    ttrs = track_list(t)
    check(len(trs) == len(ttrs), "轨道数与模板一致", f"{len(trs)} vs {len(ttrs)}")
    v = trs[0]
    stbl = find(d, v[0], v[0] + v[2], b"stbl")
    kinds = [x[1].decode("latin1") for x in children(d, stbl)]
    tkinds = [x[1].decode("latin1") for x in children(t, find(t, ttrs[0][0],
             ttrs[0][0] + ttrs[0][2], b"stbl"))]
    check(kinds == tkinds, "视频 stbl 子盒与模板一致", " ".join(kinds))
    szb = find(d, stbl[0], stbl[0] + stbl[2], b"stsz")
    n = u32(d, szb[0] + 16)
    tszb = find(t, 0, len(t), b"stsz")
    tn = u32(t, tszb[0] + 16)
    print(f"       帧数 {n:,}（模板 {tn:,}）")

    # ★ 分块规则：相机的规则是「每 0.5 秒一块」，不是「每 30 帧一块」。
    #   铁证：59.94 原片 30 帧/chunk、29.97 原片 15 帧/chunk，音频两种都是 24,024。
    #   两处都踩过坑：29.97 时视频块长了一倍、音频块算错 → 相机播到一半跳出。
    _vmd = find(d, v[0], v[0] + v[2], b"mdhd")
    _vts = u32(d, _vmd[0] + 20)
    _stts0 = find(d, stbl[0], stbl[0] + stbl[2], b"stts")
    _delta = u32(d, _stts0[0] + 20) or 1
    _fps = _vts / _delta
    _want_v = max(1, round(_fps / 2))          # 0.5 秒多少帧
    _want_a = int(_want_v * _delta / _vts * 48000 + 1e-9)   # 0.5 秒多少音频样本
    for _i, (_tr, _nm) in enumerate(((v, "视频"), (trs[1], "音频"))):
        _stbl = find(d, _tr[0], _tr[0] + _tr[2], b"stbl")
        _sc = find(d, _stbl[0], _stbl[0] + _stbl[2], b"stsc")
        _co = find(d, _stbl[0], _stbl[0] + _stbl[2], b"co64")
        _sz = find(d, _stbl[0], _stbl[0] + _stbl[2], b"stsz")
        _cnt = u32(d, _sz[0] + 16)
        _nch = u32(d, _co[0] + 12)
        _ne = u32(d, _sc[0] + 12)
        _ent = [struct.unpack(">III", d[_sc[0] + 16 + 12 * k:_sc[0] + 28 + 12 * k])
                for k in range(_ne)]
        _per = []
        for _k, (_f, _p, _s) in enumerate(_ent):
            _nx = _ent[_k + 1][0] if _k + 1 < len(_ent) else _nch + 1
            _per += [_p] * (_nx - _f)
        if _per and sum(_per) != _cnt:
            _per[-1] -= sum(_per) - _cnt
        if _per:
            _mx, _md = max(_per), sorted(_per)[len(_per) // 2]
            check(_mx <= max(2 * _md, _md + 1000),
                  f"{_nm}分块均匀（最大 {_mx:,} / 中位 {_md:,}）",
                  f"{_nch:,} 个 chunk")
            check(sum(_per) == _cnt, f"{_nm} stsc 展开样本数 == stsz 计数",
                  f"{sum(_per):,} vs {_cnt:,}")
            _want = _want_v if _i == 0 else _want_a
            check(_md == _want or (len(_per) > 1 and sorted(_per)[-2] == _want),
                  f"{_nm}分块尺寸 == 0.5 秒（应为 {_want:,}）",
                  f"中位 {_md:,}")

    # ---- 3) 时长自洽 ----
    print("\n[3] 时长自洽")
    mv = find(d, 0, len(d), b"mvhd")
    mts, mdur = u32(d, mv[0] + 20), u32(d, mv[0] + 24)
    # 注意：find 从 trak 范围开始递归即可；不要从 mdia 起点开始
    # （那会把 mdia 自己当成第一个同级盒，与类型不符就直接跳过去了）
    vmd = find(d, v[0], v[0] + v[2], b"mdhd")
    vts, vdur = u32(d, vmd[0] + 20), u32(d, vmd[0] + 24)
    a = trs[1]
    amd = find(d, a[0], a[0] + a[2], b"mdhd")
    ats, adur = u32(d, amd[0] + 20), u32(d, amd[0] + 24)
    sec_movie = mdur / mts
    sec_video = vdur / vts
    sec_audio = adur / ats
    # ★ mvhd.timescale 必须等于视频轨时基 —— 相机就是这么写的
    #   （59.94 原片 60000、30fps 原片 30000）。不一致时相机可能认错帧率。
    check(mts == vts, "mvhd.timescale == 视频轨时基", f"{mts} vs {vts}")
    # ★★ tkhd.duration 必须**保持模板原值**（不许改成"正确值"）★★
    #    真机证据：6 个能播的文件（4450/4470/4496/4500/4512/4521）的
    #    tkhd.duration 全都是模板 DSC_8955 的 228228 / 228220；
    #    所有把它改成正确值的文件，快进超过末尾时相机**不夹紧 → 卡死**。
    #    DSC_4521 vs DSC_4555 全文件只差这 6 个字节，行为随之翻转。
    for _i, (_tr, _nm) in enumerate(((v, "视频"), (a, "音频"))):
        _tk = find(d, _tr[0], _tr[0] + _tr[2], b"tkhd")
        _ttk = find(t, ttrs[_i][0], ttrs[_i][0] + ttrs[_i][2], b"tkhd")
        _tdu = u32(d, _tk[0] + 28) if d[_tk[0] + 8] == 0 else u32(d, _tk[0] + 36)
        _tpl = u32(t, _ttk[0] + 28) if t[_ttk[0] + 8] == 0 else u32(t, _ttk[0] + 36)
        check(_tdu == _tpl,
              f"tkhd.duration == 模板值（{_nm}，不许改）",
              f"{_tdu} vs 模板 {_tpl}")
    # mvhd 在影片时基里，且必须**覆盖最长的轨道**（音频常比视频略长）。
    # 早先写成视频时长，音频尾部会超出行程被切掉。
    check(sec_movie >= sec_video - 0.02, "mvhd 覆盖视频轨",
          f"{sec_movie:.4f} >= {sec_video:.4f} s")
    check(sec_movie >= sec_audio - 0.02, "mvhd 覆盖音频轨",
          f"{sec_movie:.4f} >= {sec_audio:.4f} s")
    check(abs(sec_movie - max(sec_video, sec_audio)) < 0.05,
          "mvhd 等于最长轨道时长",
          f"{sec_movie:.4f} vs max={max(sec_video, sec_audio):.4f} s")

    # ---- 4) NCDT ----
    print("\n[4] NCDT")
    nd = find(d, 0, len(d), b"NCDT")
    check(bool(nd), "NCDT 存在（缺了相机报『无法显示』）")
    if nd:
        names = [x[1].decode("latin1") for x in children(d, nd)]
        check(names == ["NCHD", "NCTG", "NCTH", "NCVW", "NCM1", "NCM2", "NCDB"],
              "NCDT 子盒与相机同序", " ".join(names))
        # 帧数
        nctg = find(d, nd[0] + 8, nd[0] + nd[2], b"NCTG")
        p, end, cnt13, time11, tz19 = nctg[0] + 8, nctg[0] + nctg[2], None, None, 0
        fps_num, fps_den = 60000, 1001
        UNIT = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4}
        while p + 8 <= end:
            tag = struct.unpack(">I", d[p:p + 4])[0]
            fmt = struct.unpack(">H", d[p + 4:p + 6])[0]
            c = struct.unpack(">H", d[p + 6:p + 8])[0]
            nb = UNIT.get(fmt, 1) * c
            if tag == 0 or p + 8 + nb > end:
                break
            if tag == 0x13 and fmt == 4:
                cnt13 = struct.unpack(">I", d[p + 8:p + 12])[0]
            if tag == 0x16 and fmt == 5 and nb >= 8:
                fps_num, fps_den = struct.unpack(">II", d[p + 8:p + 16])
            if tag == 0x11 and fmt == 2:
                time11 = bytes(d[p + 8:p + 27]).decode("latin1")
            if tag == 0x19 and fmt == 2:
                m = re.match(r"^([+-])(\d{2}):?(\d{2})",
                             bytes(d[p + 8:p + 8 + nb]).split(b"\x00")[0].decode("latin1"))
                if m:
                    tz19 = (1 if m.group(1) == "+" else -1) * (
                        int(m.group(2)) * 3600 + int(m.group(3)) * 60)
            p += 8 + nb
        check(cnt13 == n, "NCDT 帧数 == 实际帧数", f"{cnt13} vs {n}")
        # 帧率取自 NCDT 的 0x16（urational num/den）。不能把 59.94 写死
        # —— 输出改成 29.97 时，1800 帧 / 30fps = 60 s 才是对的。
        if cnt13 and n:
            expect = cnt13 * fps_den / fps_num
            check(abs(expect - sec_movie) < 0.1,
                  "NCDT 帧数推导的时长 ≈ 容器时长",
                  f"{cnt13}帧 x {fps_den}/{fps_num} = {expect:.4f} vs {sec_movie:.4f} s")
        # 视频轨时基也要与 NCDT 声明的帧率一致（604 帧/秒 写成 60000 才对得上）
        check(vts == fps_num, "视频 mdhd 时基 == NCDT 帧率分子", f"{vts} vs {fps_num}")
        # 时间两套：容器里是 UTC，NCDT 里是本地时间（相差 NCTG 0x19 的时区）
        mvt = u32(d, mv[0] + 12)
        utc = datetime.datetime(1904, 1, 1) + datetime.timedelta(seconds=mvt)
        local = utc + datetime.timedelta(seconds=tz19)
        check(bool(time11), "NCDT 拍摄时间存在", str(time11))
        if time11:
            check(time11[:19] == local.strftime("%Y:%m:%d %H:%M:%S"),
                  "NCDT 本地时间 == 容器 UTC + 时区",
                  f"NCDT={time11[:19]}  UTC{tz19 // 3600:+d}={local:%Y:%m:%d %H:%M:%S}")
        # 缩略图
        jp = cj.ncdt_jpegs(d)
        cam_jp = cj.ncdt_jpegs(t)
        # jpeg_info 返回 (高, 宽, 组件)，SOF 里先存高度
        want = {"NCTH": (160, 120), "NCM1": (640, 360), "NCVW": None}
        for k, wh in want.items():
            if k not in jp:
                check(False, f"缩略图 {k} 存在")
                continue
            marks, sof, ndq = cj.jpeg_info(jp[k])
            cm, cs, cq = cj.jpeg_info(cam_jp[k])
            check(marks[:5] == cm[:5], f"缩略图 {k} 标记序列同相机", " ".join(marks[:5]))
            check(sof[2] == cs[2], f"缩略图 {k} 采样同相机", str(sof[2][0][1]))
            check(ndq == cq, f"缩略图 {k} 量化表段数同相机", f"{ndq} vs {cq}")
            if wh:
                check((sof[1], sof[0]) == wh,
                      f"缩略图 {k} 尺寸 {wh[0]}x{wh[1]}", f"{sof[1]}x{sof[0]}")
            tmp = Path(".probe") / f"verify-{k}.jpg"
            tmp.parent.mkdir(exist_ok=True)
            tmp.write_bytes(jp[k])
            e = decode_errors(tmp)
            check(e == 0, f"缩略图 {k} 解码 0 错误", f"{e} 条")

    # ---- 5) 码流参数集 vs 相机 ----
    print("\n[5] 参数集与切片头 vs 相机")
    ours, theirs = trace_fields(out), trace_fields(tmpl)
    # ★ 必须全中的字段。SPS/PPS 级 + **切片级**（依赖 trace_fields 连 P 帧一起看）。
    KEYS = [
        # 尺寸 / 裁剪
        "pic_height_in_luma_samples", "conformance_window_flag", "conf_win_bottom_offset",
        # 色彩
        "colour_primaries", "transfer_characteristics", "video_full_range_flag",
        # 编码块结构（改了切片数据就废，必须一致）
        "log2_min_luma_coding_block_size_minus3",
        "log2_diff_max_min_luma_coding_block_size",
        "max_transform_hierarchy_depth_inter", "max_transform_hierarchy_depth_intra",
        "sample_adaptive_offset_enabled_flag", "amp_enabled_flag",
        "scaling_list_enabled_flag",
        # 切片头会多出语法的 PPS 开关
        "cabac_init_present_flag", "deblocking_filter_control_present_flag",
        "dependent_slice_segments_enabled_flag", "five_minus_max_num_merge_cand",
        "entropy_coding_sync_enabled_flag", "sign_data_hiding_enabled_flag",
        "weighted_pred_flag",
        # 参考帧结构（切片级）
        "num_ref_idx_active_override_flag",
        # 注意：**不要**把 num_negative_pics 放进必须一致清单 —— 实测能播的
        # DSC_4496 是 2、相机是 1，它随编码器实现自然波动，不是相机的判据。
        # HRD：相机没有
        "nal_hrd_parameters_present_flag", "vcl_hrd_parameters_present_flag",
        # VPS / SPS 声明
        # 注意：vps_num_units_in_tick / vps_time_scale **不**与相机比，
        # 它们是"输出帧率"的声明；输出 29.97 时应当是 1/30，相机那份是 1001/60000。
        # 单独按本文件的帧率核对，见下面 explicit 检查。
        "vps_timing_info_present_flag",
        "sps_max_dec_pic_buffering_minus1[0]", "vps_max_dec_pic_buffering_minus1[0]",
        "sps_max_latency_increase_plus1[0]", "vps_max_latency_increase_plus1[0]",
        "aspect_ratio_info_present_flag",
        "general_frame_only_constraint_flag", "general_progressive_source_flag",
    ]
    same = [k for k in KEYS if ours.get(k) == theirs.get(k)]
    for k in KEYS:
        if ours.get(k) != theirs.get(k):
            print(f"       XX {k}: 相机={theirs.get(k)} 本文件={ours.get(k)}")
    # ★ PTL 的 level 必须与相机一致：1080p60 = 5.0(150)、1080p30 = 4.1(123)。
    #   （踩过：29.97 沿用了模板的 5.0，相机不认。）
    #
    #   ⚠️ tier 与 hvcC 一致性**不作为判据** —— 实测反例：
    #     DSC_4521（真机能播、快进正常）流内 tier=0、hvcC tier=1，两者就不一致；
    #     而相机自己的原片流内 tier=1。所以这两个字段不是相机判据，
    #     只打印出来供参考，不要拿来卡门禁。
    _lv_expect = "150" if vts >= 50000 else "123"
    check(ours.get("general_level_idc") == _lv_expect,
          f"general_level_idc == {_lv_expect}（按视频时基 {vts}）",
          f"本文件={ours.get('general_level_idc')}")
    _hv = hvcc_ptl(d)
    print(f"       （参考）流内 tier={ours.get('general_tier_flag')}"
          f" level={ours.get('general_level_idc')}；"
          f"hvcC tier={_hv[1] if _hv else '?'} level={_hv[0] if _hv else '?'}")
    # VPS 时序必须与本文件声明的输出帧率一致
    check(ours.get("vps_num_units_in_tick") == str(fps_den)
          and ours.get("vps_time_scale") == str(fps_num),
          "VPS 时序 == 输出帧率",
          f"声明 {ours.get('vps_num_units_in_tick')}/{ours.get('vps_time_scale')}"
          f" vs 应为 {fps_den}/{fps_num}")
    # 故意不比对：sps_scaling_list_data_present_flag、num_short_term_ref_pic_sets、
    # short_term_ref_pic_set_sps_flag、lists_modification_present_flag
    # —— 这 4 项实测不影响相机播放（DSC_4484/4496 真机验证过）。
    check(not [k for k in KEYS if ours.get(k) != theirs.get(k)],
          f"参数集与切片头与相机一致 {len(same)}/{len(KEYS)}")

    # ---- 6) 解码 ----
    print("\n[6] 整体解码")
    e = decode_errors(out)
    check(e == 0, "ffmpeg 解码 0 错误", f"{e} 条")

    print(f"\n=== 结论：{len(OK)} 项通过，{len(BAD)} 项未通过")
    for b in BAD:
        print(f"  未通过: {b}")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
