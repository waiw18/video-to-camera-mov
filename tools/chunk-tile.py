"""严格检查交错分块：视频块/音频块交替排列时，偏移必须严丝合缝铺满 mdat。

之前的 continuity 检查没考虑交错，报了一堆假"不连续"。这里按真实交织顺序复原：
  视频块0, 音频块0, 视频块1, 音频块1, ...
然后要求每一块的起点 == 上一块的终点，且最后一块正好落在 mdat 尾部。
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from boxio import find, track_list, u32   # noqa: E402


def track_tables(d, tr):
    stbl = find(d, tr[0], tr[0] + tr[2], b"stbl")
    g = lambda t: find(d, stbl[0], stbl[0] + stbl[2], t)   # noqa: E731

    sz, sc, co = g(b"stsz"), g(b"stsc"), g(b"co64")
    vf, ssz, cnt = struct.unpack(">III", d[sz[0] + 8:sz[0] + 20])
    sizes = [ssz] * cnt if ssz else list(struct.unpack(f">{cnt}I", d[sz[0] + 20:sz[0] + 20 + 4 * cnt]))
    ne = u32(d, sc[0] + 12)
    ent = [struct.unpack(">III", d[sc[0] + 16 + 12 * k:sc[0] + 28 + 12 * k]) for k in range(ne)]
    nch = u32(d, co[0] + 12)
    offs = list(struct.unpack(f">{nch}Q", d[co[0] + 16:co[0] + 16 + 8 * nch]))
    per = []
    for k, (f, p, s) in enumerate(ent):
        nx = ent[k + 1][0] if k + 1 < len(ent) else nch + 1
        per += [p] * (nx - f)
    if per and sum(per) != cnt:
        per[-1] -= sum(per) - cnt
    ext = []
    pos = 0
    for c in per:
        q = pos
        pos += c
        ext.append((offs[len(ext)], sum(sizes[q:pos]), c))
    return ext


def main(path):
    d = Path(path).read_bytes()
    md = d.find(b"mdat", 4) - 4
    mend = md + struct.unpack(">I", d[md:md + 4])[0]
    trs = track_list(d)
    vt = track_tables(d, trs[0])
    at = track_tables(d, trs[1]) if len(trs) > 1 else []
    print(f"--- {Path(path).name}  mdat [{md:,}, {mend:,})  视频块 {len(vt)} 音频块 {len(at)}")

    # 按 视频块k, 音频块k 的顺序复原
    seq = []
    for k in range(max(len(vt), len(at))):
        if k < len(vt):
            seq.append(("视频", k, *vt[k]))
        if k < len(at):
            seq.append(("音频", k, *at[k]))
    bad, cur = [], md
    for nm, k, off, nbytes, nsmp in seq:
        if off != cur:
            bad.append((nm, k, off, cur, off - cur))
        cur = off + nbytes
    print(f"    铺满检查：{'✓ 严丝合缝（无缝隙无重叠）' if not bad else f'!! {len(bad)} 处不吻合'}")
    for b in bad[:6]:
        print(f"        {b[0]}块#{b[1]}: 实际偏移 {b[2]:,}，应为 {b[3]:,}（差 {b[4]:+,}）")
    print(f"    尾部：最后一块结束于 {cur:,}，mdat 尾 {mend:,}（差 {mend - cur:+,}）")

    # 各块在 ~50s/结尾附近的情况
    n = len(vt)
    for k in [int(n * 0.80), int(n * 0.90), n - 3, n - 2, n - 1]:
        if 0 <= k < n:
            off, nbytes, nsmp = vt[k]
            print(f"    视频块#{k:>5} ({k * 30 / (n * 30 / (n and 1)) if False else ''}"
                  f"{100 * k / n:5.1f}%)  偏移 {off:,}  {nbytes:,} B  {nsmp} 样本")


if __name__ == "__main__":
    for a in sys.argv[1:]:
        main(a)
