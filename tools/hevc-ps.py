"""按相机实测值改写 HEVC 参数集（VPS / SPS / PPS）的位级字段。

为什么需要
    相机对 VPS/SPS/PPS 里的声明性字段很挑，而 x265 没有对应开关。
    这些字段都是"声明性"的，不改变切片语法，因此可以安全地重写。

做的三件事
    VPS : vps_max_dec_pic_buffering_minus1[0] -> 1 (DPB 2，相机值)
          vps_max_latency_increase_plus1[0]   -> 0
          插入 vps_timing_info（1001 / 60000，相机值）
    SPS : sps_max_dec_pic_buffering_minus1[0] -> 1
          sps_max_latency_increase_plus1[0]   -> 0
    PPS : lists_modification_present_flag     -> 1
          （安全：只有 NumPicTotalCurr > 1 时切片头才会出现
            ref_pic_lists_modification()，本工具 ref=1，不会出现。）

注意：dependent_slice_segments_enabled_flag 不能这样改——它会给**每个切片头**
增加 dependent_slice_segment_flag 位，改了会让切片解析错位。
"""
import sys

sys.dont_write_bytecode = True


# ---------------------------------------------------------------- 位读写

class BR:
    def __init__(self, b):
        self.b = b
        self.p = 0

    def u(self, n):
        v = 0
        for _ in range(n):
            byte = self.b[self.p >> 3] if (self.p >> 3) < len(self.b) else 0
            v = (v << 1) | ((byte >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 32:
                raise ValueError("ue(v) 越界")
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


def ue_bits(v):
    """ue(v) 的比特串。"""
    n = v + 1
    s = bin(n)[2:]
    return "0" * (len(s) - 1) + s


def to_bits(b):
    return "".join(f"{x:08b}" for x in b)


def from_bits(s):
    if len(s) % 8:
        s = s + "0" * (8 - len(s) % 8)
    return bytes(int(s[i:i + 8], 2) for i in range(0, len(s), 8))


def normalise_trailing(bits):
    """把尾部的 rbsp_trailing_bits 规范化：保留到最后一个 1 位，再补 0 对齐字节。

    必须做这一步：x265 的 Annex-B 里 NAL 末尾常带一个多余的 00 字节
    （trailing_zero_8bits），改写中间字段后按字节补零会让停止位位置错乱，
    产出的参数集无法解析（表现为 alignment_bit_equal_to_one=0 之类的报错）。
    SPS/VPS/PPS 的语法都以 rbsp_stop_one_bit 结尾，其后全是 0，所以
    "最后一个 1 位"就是停止位。
    """
    i = bits.rfind("1")
    if i < 0:
        return bits
    b = bits[:i + 1]
    if len(b) % 8:
        b += "0" * (8 - len(b) % 8)
    return b


def unescape(b):
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


def escape(b):
    out, zeros = bytearray(), 0
    for x in b:
        if zeros >= 2 and x <= 3:
            out.append(3)
            zeros = 0
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out)


def apply_edits(bits, edits):
    """edits: [(start_bit, end_bit, 替换串)]，按位置升序，互不重叠。"""
    out, pos = [], 0
    for s, e, rep in sorted(edits):
        out.append(bits[pos:s])
        out.append(rep)
        pos = e
    out.append(bits[pos:])
    return "".join(out)


# ---------------------------------------------------------------- PTL

def skip_ptl(r, max_sub):
    r.u(2); r.u(1); r.u(5)
    r.u(32)
    r.u(48)
    r.u(8)                      # general_level_idc（原先漏了这一项）
    sub_prof, sub_lev = [], []
    for _ in range(max_sub):
        sub_prof.append(r.u(1))
        sub_lev.append(r.u(1))
    if max_sub > 0:
        for _ in range(max_sub, 8):
            r.u(2)
    for i in range(max_sub):
        if sub_prof[i]:
            r.u(2); r.u(1); r.u(5); r.u(32); r.u(48)
        if sub_lev[i]:
            r.u(8)


# ---------------------------------------------------------------- VPS

def patch_vps_full(nal, dpb=1, latency=0, tick=1001, scale=60000):
    raw = unescape(nal[2:])
    bits = to_bits(raw)
    r = BR(raw)
    r.u(4); r.u(1); r.u(1); r.u(6)
    max_sub = r.u(3)
    r.u(1)
    r.u(16)                      # vps_reserved_0xffff_16bits（漏读会导致后续全部错位）
    skip_ptl(r, max_sub)
    flag = r.u(1)
    start = 0 if flag else max_sub
    edits = []
    for i in range(start, max_sub + 1):
        s = r.p; r.ue(); e = r.p
        if i == 0:
            edits.append((s, e, ue_bits(dpb)))
        r.ue()
        s = r.p; r.ue(); e = r.p
        if i == 0:
            edits.append((s, e, ue_bits(latency)))
    max_layer_id = r.u(6)
    nls = r.ue()
    for _ in range(nls):
        for _ in range(max_layer_id + 1):
            r.u(1)
    ins = r.p
    # 注意：这里是**替换**原有的 vps_timing_info_present_flag 那一位（0 -> 整块时序），
    # 不是插在它前面。插在前面的会留下一个多余的 0 位；parser 在 vps_extension_flag
    # 之后本应直接读到 rbsp_stop_one_bit，那个残留 0 会让整个 VPS 解析失败
    # （现象：VPS 之后 SPS/PPS 全都不再被解析）。
    #
    # ★ 幂等：如果该 flag 已经是 1（参数集已经对齐过一次），就要**整块覆盖**，
    #   否则会把时序信息插第二遍 —— VPS 从 33 B 涨到 42 B，相机不认。
    #   （踩过：把 DSC_4521 原样截断时又打了一次补丁。）
    timing = "1" + f"{tick:032b}" + f"{scale:032b}" + "0" + ue_bits(0)
    if bits[ins] == "1":
        # 已有整块 = flag(1) + 32 + 32 + poc(1) + ue(0)=1  -> 共 67 位
        edits.append((ins, ins + 67, timing))
    else:
        edits.append((ins, ins + 1, timing))
    return nal[:2] + escape(from_bits(normalise_trailing(apply_edits(bits, edits))))


# ---------------------------------------------------------------- SPS

def patch_sps(nal, dpb=1, latency=0):
    raw = unescape(nal[2:])
    bits = to_bits(raw)
    r = BR(raw)
    r.u(4)
    max_sub = r.u(3)
    r.u(1)
    skip_ptl(r, max_sub)
    r.ue()                       # sps_seq_parameter_set_id
    chroma = r.ue()
    if chroma == 3:
        r.u(1)
    r.ue(); r.ue()               # width / height
    if r.u(1):                   # conformance_window_flag
        r.ue(); r.ue(); r.ue(); r.ue()
    r.ue(); r.ue()               # bit depths
    r.ue()                       # log2_max_pic_order_cnt_lsb_minus4
    flag = r.u(1)
    start = 0 if flag else max_sub
    edits = []
    for i in range(start, max_sub + 1):
        s = r.p; r.ue(); e = r.p
        if i == 0:
            edits.append((s, e, ue_bits(dpb)))
        r.ue()
        s = r.p; r.ue(); e = r.p
        if i == 0:
            edits.append((s, e, ue_bits(latency)))
    return nal[:2] + escape(from_bits(normalise_trailing(apply_edits(bits, edits))))


# ---------------------------------------------------------------- PPS

def patch_pps(nal, lists_mod=1):
    raw = unescape(nal[2:])
    bits = to_bits(raw)
    r = BR(raw)
    r.ue(); r.ue()               # pps_id, sps_id
    r.u(1)                       # dependent_slice_segments_enabled_flag
    r.u(1)                       # output_flag_present_flag
    r.u(3)                       # num_extra_slice_header_bits
    r.u(1)                       # sign_data_hiding_enabled_flag
    r.u(1)                       # cabac_init_present_flag
    r.ue(); r.ue()               # num_ref_idx defaults
    r.se()                       # init_qp_minus26
    r.u(1)                       # constrained_intra_pred_flag
    r.u(1)                       # transform_skip_enabled_flag
    if r.u(1):                   # cu_qp_delta_enabled_flag
        r.ue()
    r.se(); r.se()               # cb/cr qp offsets
    r.u(1)                       # pps_slice_chroma_qp_offsets_present_flag
    r.u(1)                       # weighted_pred_flag
    r.u(1)                       # weighted_bipred_flag
    r.u(1)                       # transquant_bypass_enabled_flag
    if r.u(1):                   # tiles_enabled_flag
        raise RuntimeError("PPS 带 tiles，未处理")
    r.u(1)                       # entropy_coding_sync_enabled_flag
    r.u(1)                       # pps_loop_filter_across_slices_enabled_flag
    if r.u(1):                   # deblocking_filter_control_present_flag
        r.u(1)
        if r.u(1):
            r.se(); r.se()
    if r.u(1):                   # pps_scaling_list_data_present_flag
        raise RuntimeError("PPS 带 scaling list，未处理")
    s, e = r.p, r.p + 1          # lists_modification_present_flag
    newbits = apply_edits(bits, [(s, e, str(lists_mod & 1))])
    return nal[:2] + escape(from_bits(normalise_trailing(newbits)))


def set_pps_dependent_slices(nal):
    """把 PPS 的 dependent_slice_segments_enabled_flag 置 1（相机的值是 1）。

    只在"非首切片"上才会多出 dependent_slice_segment_flag 位，而本工具的每个图像
    都是单切片（first_slice_segment_in_pic_flag=1），所以置 1 不会改变任何切片头的
    解析——但仍须实测确认。
    """
    raw = unescape(nal[2:])
    bits = to_bits(raw)
    r = BR(raw)
    r.ue(); r.ue()
    s = r.p                      # dependent_slice_segments_enabled_flag 就在这里
    newbits = apply_edits(bits, [(s, s + 1, "1")])
    return nal[:2] + escape(from_bits(normalise_trailing(newbits)))


def sps_add_conformance_window(nal, bottom=4):
    """给 SPS 加 conformance_window（相机的编码画面是 1920x1088，裁 8 行显示 1080）。

    相机的 SPS：pic_height_in_luma_samples=1088，conformance_window_flag=1，
    conf_win_bottom_offset=4（4 x 2 = 8 行）。本工具若按 1080 编码就没有这个裁剪。

    必须配合"真的编 1088 行"（底部补 8 行黑边），否则声明与实际不符。
    """
    raw = unescape(nal[2:])
    bits = to_bits(raw)
    r = BR(raw)
    r.u(4)
    ms = r.u(3)
    r.u(1)
    skip_ptl(r, ms)
    r.ue()                       # sps_seq_parameter_set_id
    c = r.ue()                   # chroma_format_idc
    if c == 3:
        r.u(1)
    r.ue()                       # width
    r.ue()                       # height
    s = r.p                      # conformance_window_flag
    if r.u(1):
        return nal               # 已有裁剪窗口，不动
    rep = "1" + ue_bits(0) + ue_bits(0) + ue_bits(0) + ue_bits(bottom)
    newbits = apply_edits(bits, [(s, s + 1, rep)])
    return nal[:2] + escape(from_bits(normalise_trailing(newbits)))


def st_ref_pic_set(r, idx, num_delta_pocs):
    """跳过 st_ref_pic_set(idx) 的语法，返回该集合的 NumDeltaPocs。

    HEVC 7.3.7。x265 默认 num_short_term_ref_pic_sets=0（循环不执行）；
    NVENC 会给 1 个，不解析就会卡在 SPS 中间，VUI / aspect_ratio 全都读不到，
    整个参数集对齐会失败（踩过）。
    """
    inter = r.u(1) if idx != 0 else 0
    if inter:
        r.u(1)                          # delta_rps_sign
        r.ue()                          # abs_delta_rps_minus1
        ref = idx - 1
        cnt = 0
        for _ in range((num_delta_pocs[ref] if ref < len(num_delta_pocs) else 0) + 1):
            used = r.u(1)               # used_by_curr_pic_flag
            if not used:
                r.u(1)                  # use_delta_flag
            if used:
                cnt += 1
        return cnt
    nn = r.ue()                         # num_negative_pics
    npos = r.ue()                       # num_positive_pics
    for _ in range(nn):
        r.ue()                          # delta_poc_s0_minus1
        r.u(1)                          # used_by_curr_pic_s0_flag
    for _ in range(npos):
        r.ue()                          # delta_poc_s1_minus1
        r.u(1)                          # used_by_curr_pic_s1_flag
    return nn + npos


def ptl_constraint_bit(nal):
    """返回 profile_tier_level 里 general_constraint_indicator_flags 的起始位。

    不再靠"在字节里找 60 00 00 00"这种特征匹配 —— 那只能撞上 x265
    （它把通用兼容标志写成 0x60000000，置了 Baseline+Main 两位），
    NVENC 写的是 0x40000000（只置 Main），于是 NVENC 的 constraint flags
    一直没被清零，相机那边看到 general_progressive_source_flag=1、
    general_frame_only_constraint_flag=1（相机全是 0）。
    这里按语法真正走一遍 PTL 前缀。
    """
    t = (nal[0] >> 1) & 0x3F
    if t not in (32, 33):
        return None
    r = BR(unescape(nal[2:]))
    if t == 32:                      # VPS 到 PTL 之前的字段
        r.u(4); r.u(1); r.u(1); r.u(6); r.u(3); r.u(1); r.u(16)
    else:                            # SPS 到 PTL 之前的字段
        r.u(4); r.u(3); r.u(1)
    r.u(2); r.u(1); r.u(5)           # profile_space / tier_flag / profile_idc
    r.u(32)                          # general_profile_compatibility_flag[32]
    return r.p


def set_constraint_flags(nal, value=0):
    """把 PTL 的 48 位 constraint flags 全清零（相机实测就是全 0）。

    hvcC 的 23 字节记录头里写的也是 0；码流里若还是 0x90
    （progressive + frame_only），文件就自相矛盾。
    """
    bit = ptl_constraint_bit(nal)
    if bit is None or bit % 8:
        return nal
    b = bytearray(unescape(nal[2:]))
    i = bit // 8
    if i + 6 > len(b) or all(x == value for x in b[i:i + 6]):
        return nal
    b[i:i + 6] = bytes([value]) * 6
    return nal[:2] + escape(bytes(b))


def set_ptl(nal, level_idc=None, tier=None):
    """直接改写 PTL 的 general_tier_flag 与 general_level_idc。

    ★ 为什么必须自己写、不能靠编码器：
      x265 的 `high-tier=1` **不保证生效** —— 实测 640x360 时写出 tier=1，
      但 1280x720、同样的参数就写出 tier=0（它按码率/VBV 自己判断 tier）。
      相机两个原片（1080p60 / 1080p30）都是 **High tier = 1**，
      所以这里按语法定位后直接写死，不依赖编码器。

    位布局（从 PTL 起始算）：
        +0    profile_space(2) | tier_flag(1) | profile_idc(5)   <- 1 字节
        +8    general_profile_compatibility_flags(32)
        +40   general_constraint_indicator_flags(48)
        +88   general_level_idc(8)
    """
    bit = ptl_constraint_bit(nal)
    if bit is None or bit % 8:
        return nal
    b = bytearray(unescape(nal[2:]))
    pb = (bit - 40) // 8      # 上面那个 profile/tier/profile 字节
    lb = (bit + 48) // 8      # general_level_idc
    if pb < 0 or lb >= len(b):
        return nal
    if tier is not None:
        b[pb] = (b[pb] & 0x3F) | ((tier & 1) << 5)
    if level_idc is not None:
        b[lb] = level_idc
    return nal[:2] + escape(bytes(b))


def sps_reach_vui(r):
    """把 SPS 读到 vui_parameters_present_flag 之后，返回 VUI 起始位。"""
    r.u(4)
    ms = r.u(3)
    r.u(1)
    skip_ptl(r, ms)
    r.ue()                       # sps_seq_parameter_set_id
    c = r.ue()                   # chroma_format_idc
    if c == 3:
        r.u(1)
    r.ue(); r.ue()               # width / height
    if r.u(1):                   # conformance_window_flag
        for _ in range(4):
            r.ue()
    r.ue(); r.ue()               # bit depths
    r.ue()                       # log2_max_pic_order_cnt_lsb_minus4
    flag = r.u(1)
    st = 0 if flag else ms
    for _ in range(st, ms + 1):
        r.ue(); r.ue(); r.ue()
    r.ue(); r.ue(); r.ue(); r.ue()      # log2_min_cb / diff_cb / log2_min_tb / diff_tb
    r.ue(); r.ue()                      # max_transform_hierarchy_depth_inter/intra
    if r.u(1):                          # scaling_list_enabled_flag
        if r.u(1):                      # sps_scaling_list_data_present_flag
            raise RuntimeError("SPS 带 scaling list data，暂未处理")
    r.u(1)                              # amp_enabled_flag
    r.u(1)                              # sample_adaptive_offset_enabled_flag
    if r.u(1):                          # pcm_enabled_flag
        raise RuntimeError("SPS 带 PCM，暂未处理")
    n_rps = r.ue()
    ndp = []
    for i in range(n_rps):
        ndp.append(st_ref_pic_set(r, i, ndp))
    if r.u(1):                          # long_term_ref_pics_present_flag
        raise RuntimeError("SPS 带长期参考，暂未处理")
    r.u(1)                              # sps_temporal_mvp_enabled_flag
    r.u(1)                              # strong_intra_smoothing_enabled_flag
    if not r.u(1):                      # vui_parameters_present_flag
        return None
    return r.p


def strip_aspect_ratio(nal):
    """删掉 SPS VUI 里的 aspect_ratio_info（相机没有这一项）。

    相机的 aspect_ratio_info_present_flag=0；本工具默认会写 SAR 1:1，
    于是多出 flag(1) + aspect_ratio_idc(8) 共 9 位。
    """
    raw = unescape(nal[2:])
    bits = to_bits(raw)
    r = BR(raw)
    vui = sps_reach_vui(r)
    if vui is None:
        return nal
    s = vui
    present = r.u(1)
    if not present:
        return nal
    idc = r.u(8)
    n = 9
    if idc == 255:
        n += 32
    newbits = apply_edits(bits, [(s, s + n, "0")])
    return nal[:2] + escape(from_bits(normalise_trailing(newbits)))



# ---------------------------------------------------------------- 入口

def align(nal, dpb=1, latency=0, drop_aspect=False, dependent_slices=False,
          lists_mod=False, conf_bottom=None, tick=1001, scale=60000):
    """按 NAL 类型分发参数集改写。

    drop_aspect      : 删掉 SPS VUI 的 aspect_ratio_info（相机是 0）
    dependent_slices : PPS 的 dependent_slice_segments_enabled_flag 置 1（相机是 1）
    lists_mod        : PPS 的 lists_modification_present_flag 置 1。
                       默认关闭：实测单独翻这一位会让切片头解析错位
                       （12 帧出现 10 个 alignment_bit_equal_to_one=0），
                       它必须与切片头里的 ref_pic_lists_modification() 同时存在。
    conf_bottom      : 给 SPS 加 conformance_window（相机编码高度 1088、裁 8 行）。
                       必须配合真的编 1088 行，否则声明与实际不符。
    tick / scale     : VPS 里的时序信息。相机 1080p60 是 1001/60000；
                       输出改成 29.97 时要相应写成 1001/30000，否则声明与实际不符。
    """
    t = (nal[0] >> 1) & 0x3F
    if t == 32:
        return patch_vps_full(nal, dpb, latency, tick=tick, scale=scale)
    if t == 33:
        out = patch_sps(nal, dpb, latency)
        if conf_bottom:
            out = sps_add_conformance_window(out, conf_bottom)
        if drop_aspect:
            out = strip_aspect_ratio(out)
        return out
    if t == 34:
        out = nal
        if dependent_slices:
            out = set_pps_dependent_slices(out)
        if lists_mod:
            out = patch_pps(out)
        return out
    return nal


if __name__ == "__main__":
    # 自测：拿一段真实码流跑一遍
    from pathlib import Path
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mcm", Path(__file__).resolve().parent / "make-camera-mov.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    es = sys.argv[1] if len(sys.argv) > 1 else ".probe/enc.265"
    if not Path(es).exists():
        print(f"找不到 {es}，先跑一次 --keep 转换")
        sys.exit(1)
    nals = m.split_annexb(Path(es).read_bytes())
    for t, name in ((32, "VPS"), (33, "SPS"), (34, "PPS")):
        n = next(x for x in nals if ((x[0] >> 1) & 0x3F) == t)
        p = align(n)
        print(f"{name}: {len(n)} -> {len(p)} B")
        Path(f".probe/aligned-{name}.bin").write_bytes(p)
