"""分析分段并行拼接出来的 Annex-B 码流：各段参数集是否一致、边界处结构是否正常。"""
import hashlib
import sys
from pathlib import Path

NAMES = {32: "VPS", 33: "SPS", 34: "PPS", 35: "AUD", 39: "PREFIX_SEI", 40: "SUFFIX_SEI",
         19: "IDR_W_RADL", 20: "IDR_N_LP", 21: "CRA", 1: "TRAIL_R", 0: "TRAIL_N",
         2: "TSA_R", 3: "TSA_N", 4: "STSA_R", 5: "STSA_N", 6: "RADL_R", 7: "RADL_N",
         8: "RASL_R", 9: "RASL_N"}


def nals(data):
    """切出 (type, bytes) 列表（Annex-B）。"""
    out, i, n = [], 0, len(data)
    starts = []
    while i < n - 3:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                starts.append((i, 3))
                i += 3
                continue
            if i < n - 4 and data[i + 2] == 0 and data[i + 3] == 1:
                starts.append((i, 4))
                i += 4
                continue
        i += 1
    for k, (s, sl) in enumerate(starts):
        e = starts[k + 1][0] if k + 1 < len(starts) else n
        body = data[s + sl:e]
        if body:
            out.append(((body[0] >> 1) & 0x3F, body))
    return out


def main(path):
    d = Path(path).read_bytes()
    ns = nals(d)
    print(f"=== {Path(path).name}  {len(d):,} B  NAL {len(ns):,} 个")

    # 参数集出现情况
    ps = [(i, t, b) for i, (t, b) in enumerate(ns) if t in (32, 33, 34)]
    print(f"    参数集 NAL 共 {len(ps)} 个："
          + " ".join(f"#{i}:{NAMES[t]}({len(b)}B)" for i, t, b in ps[:16]))
    groups = {}
    for i, t, b in ps:
        groups.setdefault(t, []).append((i, hashlib.sha256(b).hexdigest()[:12], len(b)))
    for t, lst in groups.items():
        uniq = {h for _, h, _ in lst}
        print(f"    {NAMES[t]}: {len(lst)} 份，去重后 {len(uniq)} 种"
              f"{'  ★ 各段内容不一致！' if len(uniq) > 1 else '  ✓ 各段完全一致'}")
        if len(uniq) > 1:
            for i, h, ln in lst:
                print(f"        NAL#{i:>6}  {h}  {ln}B")

    # 每个段的第一帧：参数集之后紧跟的 NAL
    print("    各段起点（参数集 VPS 之后 3 个 NAL）：")
    vps_idx = [i for i, t, b in ps if t == 32]
    for k, vi in enumerate(vps_idx):
        nxt = [f"{NAMES[ns[j][0]]}" for j in range(vi, min(vi + 5, len(ns)))]
        print(f"        段{k + 1}: NAL#{vi} -> {' '.join(nxt)}")

    # NAL 类型分布
    from collections import Counter
    c = Counter(NAMES.get(t, str(t)) for t, _ in ns)
    print("    NAL 类型分布:", dict(c))

    # 检查 AU 分隔：每个 AUD 之后到下一个 AUD 之间应当恰好 1 个 VCL NAL
    auds = [i for i, (t, _) in enumerate(ns) if t == 35]
    bad = []
    for k, a in enumerate(auds):
        e = auds[k + 1] if k + 1 < len(auds) else len(ns)
        vcl = [ns[j][0] for j in range(a + 1, e) if ns[j][0] < 32]
        extra = [ns[j][0] for j in range(a + 1, e) if ns[j][0] >= 32]
        if len(vcl) != 1 or extra:
            bad.append((k, len(vcl), [NAMES.get(x, x) for x in extra]))
    print(f"    AUD 分隔检查：{len(auds)} 个 AUD，异常 {len(bad)} 处")
    for b in bad[:6]:
        print(f"        AU#{b[0]}: VCL {b[1]} 个，非 VCL 多余 {b[2]}")


if __name__ == "__main__":
    for a in sys.argv[1:]:
        main(a)
