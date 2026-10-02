"""逐字段对比两个 MOV/MP4 的 hvcC（HEVC 参数集记录）。

用法: python tools/hvcc-diff.py <文件A> <文件B>
"""
import struct
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from boxio import find, track_list   # noqa: E402

PROFILES = {1: "Main", 2: "Main10", 3: "MainStillPicture", 4: "RExt"}
SPACES = {0: "", 1: "A", 2: "B", 3: "C"}


def get_hvcc(d):
    tr = track_list(d)[0]
    stbl = find(d, tr[0], tr[0] + tr[2], b"stbl")
    stsd = find(d, stbl[0], stbl[0] + stbl[2], b"stsd")
    inner = stsd[0] + 16
    esz = struct.unpack(">I", d[inner:inner + 4])[0]
    entry = d[inner:inner + esz]
    p = entry.find(b"hvcC")
    ln = struct.unpack(">I", entry[p - 4:p])[0]
    return entry[p + 4:p - 4 + ln]


class BR:
    def __init__(self, b):
        self.b, self.p = b, 0

    def u(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.b[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v


def parse_sps_ptl(sps):
    """跳过 2 字节 NAL 头，读 profile_tier_level。"""
    r = BR(sps[2:])
    r.u(4)                       # sps_video_parameter_set_id
    max_sub = r.u(3)
    r.u(1)                       # sps_temporal_id_nesting_flag
    # profile_tier_level
    space = r.u(2)
    tier = r.u(1)
    pidc = r.u(5)
    compat = r.u(32)
    constraint = [r.u(1) for _ in range(48)]
    level = r.u(8)
    out = {
        "profile_space": SPACES.get(space, space),
        "tier": "High" if tier else "Main",
        "profile_idc": pidc,
        "profile_name": PROFILES.get(pidc, "?"),
        "compat_flags": f"{compat:08x}",
        "level_idc": level,
        "level": f"{level / 30:.1f}",
        "max_sub_layers": max_sub,
    }
    n = max_sub
    if n > 0:
        prof = []
        for _ in range(n):
            prof.append((r.u(1), r.u(1), r.u(1)))
        for _ in range(n, 8):
            r.u(2)
        for i in range(n):
            r.u(1)
            if prof[i][0]:
                r.u(88)
        for i in range(n, 8):
            r.u(2)
    return out, r


def scan_vui_timing(sps):
    """在 SPS 里粗暴搜 VUI 的 timing_info 特征位（vui_timing_info_present_flag 后
    跟 32 位 num_units_in_tick 和 32 位 time_scale）。"""
    return None


def main(a_path, b_path):
    A, B = Path(a_path).read_bytes(), Path(b_path).read_bytes()
    ha, hb = get_hvcc(A), get_hvcc(B)
    print(f"A = {Path(a_path).name}   hvcC {len(ha)} B")
    print(f"B = {Path(b_path).name}   hvcC {len(hb)} B\n")

    for label, h in (("A", ha), ("B", hb)):
        print(f"[{label}] 记录头: {h[:23].hex(' ')}")
    print()

    def rec(h):
        return {
            "configurationVersion": h[0],
            "profile_space/tier/profile_idc": h[1],
            "profile_space": SPACES.get(h[1] >> 6, "?"),
            "tier": "High" if (h[1] >> 5) & 1 else "Main",
            "profile_idc": h[1] & 0x1F,
            "profile_name": PROFILES.get(h[1] & 0x1F, "?"),
            "compat_flags": h[2:6].hex(),
            "constraint_flags": h[6:12].hex(),
            "level_idc": h[12],
            "level": f"{h[12] / 30:.1f}",
            "min_spatial_seg": struct.unpack(">H", h[13:15])[0] & 0x0FFF,
            "parallelismType": h[15] & 3,
            "chromaFormat": h[16] & 3,
            "bitDepthLuma": (h[17] & 7) + 8,
            "bitDepthChroma": (h[18] & 7) + 8,
            "avgFrameRate": struct.unpack(">H", h[19:21])[0],
            "constantFrameRate": h[21] >> 6,
            "numTemporalLayers": (h[21] >> 3) & 7,
            "temporalIdNested": (h[21] >> 2) & 1,
            "lengthSizeMinusOne": h[21] & 3,
            "numOfArrays": h[22],
        }

    ra, rb = rec(ha), rec(hb)
    print(f"{'字段':<32} {'A（相机）':<24} {'B（本工具）':<24} 相同")
    for k in ra:
        va, vb = ra[k], rb[k]
        mark = "✓" if va == vb else "✗"
        print(f"{k:<32} {str(va):<24} {str(vb):<24} {mark}")

    # NAL 数组
    print()
    for label, h in (("A", ha), ("B", hb)):
        p = 23
        arrs = []
        while p + 3 <= len(h):
            t = h[p] & 0x3F
            n = struct.unpack(">H", h[p + 1:p + 3])[0]
            p += 3
            lens = []
            for _ in range(n):
                ln = struct.unpack(">H", h[p:p + 2])[0]
                p += 2
                lens.append(ln)
                p += ln
            arrs.append((t, n, lens))
        print(f"[{label}] NAL 数组: {arrs}")

    # SPS profile_tier_level
    print()
    for label, h in (("A（相机）", ha), ("B（本工具）", hb)):
        p = 23
        sps = None
        while p + 3 <= len(h):
            t = h[p] & 0x3F
            n = struct.unpack(">H", h[p + 1:p + 3])[0]
            p += 3
            for _ in range(n):
                ln = struct.unpack(">H", h[p:p + 2])[0]
                p += 2
                if t == 33 and sps is None:
                    sps = h[p:p + ln]
                p += ln
        if sps:
            try:
                info, _ = parse_sps_ptl(sps)
                print(f"[{label}] SPS profile_tier_level: {info}")
            except Exception as e:
                print(f"[{label}] SPS 解析失败: {e}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
