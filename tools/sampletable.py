"""检查视频样本表的**连续性**与结构：能定位"播一半就跳出"这类问题。

检查项：
  * 样本数 / 分块数 / stsc 段
  * co64 每个 chunk 的偏移是否严格递进、且与样本大小之和完全吻合（有无空隙/重叠）
  * stss（IDR 位置）分布，段间是否异常
  * mdat 范围是否覆盖所有 chunk
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from boxio import find, track_list, u32   # noqa: E402


def parse(d, idx):
    tr = track_list(d)[idx]
    stbl = find(d, tr[0], tr[0] + tr[2], b"stbl")

    def box(t):
        r = find(d, stbl[0], stbl[0] + stbl[2], t)
        return r

    sz = box(b"stsz")
    ver_flags, sample_size, count = struct.unpack(">III", d[sz[0] + 8:sz[0] + 20])
    if sample_size:
        sizes = [sample_size] * count
    else:
        sizes = list(struct.unpack(f">{count}I", d[sz[0] + 20:sz[0] + 20 + 4 * count]))

    sc = box(b"stsc")
    n = u32(d, sc[0] + 12)
    stsc = [struct.unpack(">III", d[sc[0] + 16 + 12 * i:sc[0] + 28 + 12 * i]) for i in range(n)]

    co = box(b"co64")
    m = u32(d, co[0] + 12)
    offs = list(struct.unpack(f">{m}Q", d[co[0] + 16:co[0] + 16 + 8 * m]))

    ss = box(b"stss")
    idr = []
    if ss:
        k = u32(d, ss[0] + 12)
        idr = list(struct.unpack(f">{k}I", d[ss[0] + 16:ss[0] + 16 + 4 * k]))
    return {"n": count, "sizes": sizes, "stsc": stsc, "offs": offs, "idr": idr, "trak": tr}


def chunk_sizes(stsc, total_chunks, total_samples):
    """按 stsc 展开每个 chunk 的样本数。"""
    out = []
    for i, (first, per, sdi) in enumerate(stsc):
        nxt = stsc[i + 1][0] if i + 1 < len(stsc) else total_chunks + 1
        for _ in range(first, nxt):
            out.append(per)
    # 修正：最后一个 chunk 可能不足 per
    got = sum(out)
    if got != total_samples and out:
        out[-1] -= got - total_samples
    return out


def dump(path):
    d = Path(path).read_bytes()
    md = d.find(b"mdat", 4) - 4
    msz = struct.unpack(">I", d[md:md + 4])[0]
    mend = md + msz
    print(f"--- {Path(path).name}  ({len(d):,} B)   mdat @{md:,} 大小 {msz:,}  -> 尾 {mend:,}")
    for idx, name in ((0, "视频"), (1, "音频")):
        p = parse(d, idx)
        cs = chunk_sizes(p["stsc"], len(p["offs"]), p["n"])
        print(f"    {name}: 样本 {p['n']:,}  chunk {len(p['offs']):,}  "
              f"stsc 段 {len(p['stsc'])}  每 chunk 样本 {sorted(set(cs))[:6]}")
        # 连续性：offset[i+1] == offset[i] + sum(sizes of chunk i)
        bad, pos = [], 0
        for i, c in enumerate(cs):
            want_end = p["offs"][i] + sum(p["sizes"][pos:pos + c])
            pos += c
            if i + 1 < len(p["offs"]) and p["offs"][i + 1] != want_end:
                bad.append((i, p["offs"][i + 1], want_end))
        print(f"         偏移递进: {'连续' if not bad else f'!! {len(bad)} 处不连续'}")
        for b in bad[:3]:
            print(f"           chunk {b[0]}: 下一个偏移 {b[1]:,}，应为 {b[2]:,}"
                  f"（差 {b[1]-b[2]:+,}）")
        oob = [o for o in p["offs"] if not (md <= o < mend)]
        print(f"          chunk 落在 mdat 内: {'是' if not oob else f'!! {len(oob)} 个越界'}")
        last_end = p["offs"][-1] + sum(p["sizes"][sum(cs[:-1]):])
        print(f"          最后一个 chunk 结束于 {last_end:,}（mdat 尾 {mend:,}）")
        if p["idr"]:
            gaps = sorted({p["idr"][i + 1] - p["idr"][i] for i in range(len(p["idr"]) - 1)})
            print(f"          IDR {len(p['idr']):,} 个，间隔分布 {gaps[:8]}，"
                  f"首个 {p['idr'][:4]}，末个 {p['idr'][-3:]}")


if __name__ == "__main__":
    for a in sys.argv[1:]:
        dump(a)
