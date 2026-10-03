"""把 MOV 逐字节展开成 Markdown —— 用来把"真机能播的文件"完整记录下来。

设计目标：接手的人只看这份文档就能知道一个**能播**的文件每个字节是什么。

展开内容：
  * 顶层布局（每个盒的偏移与大小）
  * 完整盒树（递归，带偏移/大小）
  * ftyp / mvhd / tkhd / edts-elst / mdhd / hdlr / stsd / stts / stsc / stsz / stco / stss
    每个字段：字节偏移 + 原始值 + 解释
  * NCDT：子盒偏移 + NCTG 每条记录（tag/格式/数量/偏移/值）
  * hvcC：23 字节记录头逐字段 + 参数集十六进制
  * VPS/SPS/PPS：十六进制 + 解析出的关键字段
  * 样本内部 NAL 结构（含 IDR 位置）
  * mdat 分块交错布局（前若干块与末尾若干块）
"""
import hashlib
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from boxio import find, track_list, u32, children   # noqa: E402

from binpath import FFMPEG, FFPROBE

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf",
              b"udta", b"meta", b"ilst", b"moof", b"traf", b"mvex"}
UNIT = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 1}
NALNAME = {32: "VPS", 33: "SPS", 34: "PPS", 35: "AUD", 39: "PREFIX_SEI", 40: "SUFFIX_SEI",
           19: "IDR_W_RADL", 20: "IDR_N_LP", 21: "CRA", 1: "TRAIL_R", 0: "TRAIL_N"}


def hx(b, n=None):
    b = bytes(b)
    if n and len(b) > n:
        return " ".join(f"{x:02X}" for x in b[:n]) + f" …(+{len(b)-n}B)"
    return " ".join(f"{x:02X}" for x in b)


def walk(d, start, end, depth, out):
    p = start
    while p + 8 <= end:
        size = struct.unpack(">I", d[p:p + 4])[0]
        typ = bytes(d[p + 4:p + 8])
        hdr = 8
        if size == 1 and p + 16 <= end:
            size = struct.unpack(">Q", d[p + 8:p + 16])[0]
            hdr = 16
        if size < hdr or p + size > end:
            out.append((depth, p, "!!坏盒", size, typ))
            return
        out.append((depth, p, typ.decode("latin1"), size, None))
        if typ in CONTAINERS:
            walk(d, p + hdr, p + size, depth + 1, out)
        p += size


def track_of(d, idx):
    return track_list(d)[idx]


def dump_video_entry(d, stsd, L):
    inner = stsd[0] + 16
    esz = u32(d, inner)
    entry = bytes(d[inner:inner + esz])
    fmt = entry[4:8].decode("latin1")
    L.append(f"- sample entry `{fmt}` 大小 {esz}（偏移 {inner}）")
    L.append(f"  - 前 24 字节：`{hx(entry[:24])}`")
    L.append(f"  - width/height（偏移 {inner+24} / {inner+26}）："
             f"{struct.unpack('>H', d[inner+24:inner+26])[0]} x "
             f"{struct.unpack('>H', d[inner+26:inner+28])[0]}")
    hvi = entry.find(b"hvcC")
    if hvi < 0:
        L.append("  - **没有 hvcC**")
        return
    hv = entry[hvi + 4:]
    L.append(f"  - `hvcC` 在 entry 内偏移 {hvi}，全长 {len(hv)} B")
    L.append("")
    L.append("  **hvcC 23 字节记录头逐字段**（偏移相对 hvcC 体）：")
    L.append("")
    L.append("  | 偏移 | 字节 | 含义 |")
    L.append("  |---|---|---|")
    L.append(f"  | +0 | {hv[0]:02X} | configurationVersion |")
    L.append(f"  | +1 | {hv[1]:02X} | profile_space={(hv[1]>>6)&3} "
             f"tier_flag={(hv[1]>>5)&1} profile_idc={hv[1]&31} |")
    L.append(f"  | +2..5 | {hx(hv[2:6])} | general_profile_compatibility_flags |")
    L.append(f"  | +6..11 | {hx(hv[6:12])} | general_constraint_indicator_flags |")
    L.append(f"  | +12 | {hv[12]:02X} | general_level_idc = {hv[12]} |")
    L.append(f"  | +13..14 | {hx(hv[13:15])} | min_spatial_segmentation_idc |")
    L.append(f"  | +15 | {hv[15]:02X} | parallelismType |")
    L.append(f"  | +16 | {hv[16]:02X} | chromaFormat |")
    L.append(f"  | +17..18 | {hx(hv[17:19])} | bitDepthLuma/Chroma |")
    L.append(f"  | +19..20 | {hx(hv[19:21])} | avgFrameRate |")
    L.append(f"  | +21 | {hv[21]:02X} | constantFrameRate={(hv[21]>>6)&3} "
             f"numTemporalLayers={(hv[21]>>3)&7} temporalIdNested={(hv[21]>>2)&1} "
             f"lengthSizeMinusOne={hv[21]&3} |")
    L.append(f"  | +22 | {hv[22]:02X} | numOfArrays = {hv[22]} |")
    p = 23
    for _ in range(hv[22] if len(hv) > 22 else 0):
        if p + 3 > len(hv):
            break
        at = hv[p]
        n = struct.unpack(">H", hv[p + 1:p + 3])[0]
        p += 3
        L.append(f"  - 数组 `{(at & 0x3F)}`（{NALNAME.get(at & 0x3F, '?')}）"
                 f"array_completeness={(at>>7)&1} 条数={n}")
        for _ in range(n):
            ln = struct.unpack(">H", hv[p:p + 2])[0]
            p += 2
            nal = hv[p:p + ln]
            p += ln
            L.append(f"    - {ln} B：`{hx(nal)}`")
    L.append("")


def dump_ncdt(d, nd, L):
    L.append("### NCDT")
    L.append("")
    L.append(f"NCDT 盒偏移 {nd[0]}，大小 {nd[2]}，体 {nd[0]+8}..{nd[0]+nd[2]}")
    L.append("")
    L.append("| 子盒 | 偏移 | 大小 |")
    L.append("|---|---|---|")
    for c in children(d, nd):
        L.append(f"| `{c[1].decode('latin1')}` | {c[0]} | {c[2]} |")
    L.append("")
    nctg = find(d, nd[0] + 8, nd[0] + nd[2], b"NCTG")
    if not nctg:
        L.append("（没有 NCTG）")
        return
    L.append(f"#### NCTG 记录（体 {nctg[0]+8}..{nctg[0]+nctg[2]}，共 "
             f"{nctg[2]-8} B）")
    L.append("")
    L.append("| tag | fmt | count | 值偏移 | 原始值 | 解释 |")
    L.append("|---|---|---|---|---|---|")
    p, end = nctg[0] + 8, nctg[0] + nctg[2]
    while p + 8 <= end:
        tag = struct.unpack(">I", d[p:p + 4])[0]
        fmt = struct.unpack(">H", d[p + 4:p + 6])[0]
        cnt = struct.unpack(">H", d[p + 6:p + 8])[0]
        nb = UNIT.get(fmt, 1) * cnt
        if tag == 0 or p + 8 + nb > end:
            L.append(f"| 0x000000 | | | {p} | （结束标记） | |")
            break
        raw = bytes(d[p + 8:p + 8 + nb])
        if fmt == 2:
            txt = raw.split(b"\x00")[0].decode("latin1", "replace")
            val, exp = f'"{txt}"', "ASCII 字符串"
        elif fmt == 4:
            v = [struct.unpack(">I", raw[4 * k:4 * k + 4])[0] for k in range(cnt)]
            val = str(v)
            exp = {"0x13": "帧数（时长 = 0x13 / 0x16）", "0x101": "[160,120,1920,1080] 缩略图尺寸",
                   "0x102": "NCM1 相关尺寸"}.get(hex(tag), "")
        elif fmt == 3:
            v = [struct.unpack(">H", raw[2 * k:2 * k + 2])[0] for k in range(cnt)]
            val, exp = str(v), ""
        elif fmt == 5:
            v = [f"{struct.unpack('>I', raw[8*k:8*k+4])[0]}/"
                 f"{struct.unpack('>I', raw[8*k+4:8*k+8])[0]}" for k in range(cnt)]
            val = str(v)
            exp = "帧率 rational" if tag in (0x16, 0x17) else ""
        else:
            val, exp = f"{nb} B: {hx(raw, 16)}", ""
        L.append(f"| `{hex(tag)}` | {fmt} | {cnt} | {p} | {val} | {exp} |")
        p += 8 + nb
    L.append("")
    # 缩略图
    for name in (b"NCTH", b"NCVW", b"NCM1", b"NCM2"):
        b = find(d, nd[0] + 8, nd[0] + nd[2], name)
        if b:
            body = bytes(d[b[0] + 8:b[0] + b[2]])
            if len(body) < 4:
                L.append(f"- `{name.decode()}`：空体（{b[2]} B）")
                continue
            marks = []
            i = 0
            while i + 3 < len(body):
                if body[i] == 0xFF and body[i + 1] != 0:
                    marks.append(hex(body[i + 1]))
                    i += 2
                else:
                    i += 1
            # SOF0 结构：FF C0 | len(2) | precision(1) | height(2) | width(2)
            #            | ncomp(1) | 每分量: id(1) sampling(1) qtable(1)
            sub = "?"
            j = body.find(b"\xff\xc0")
            if j >= 0 and j + 19 < len(body):
                ncomp = body[j + 9]
                parts = []
                for c in range(ncomp):
                    s = body[j + 11 + 3 * c]
                    parts.append(f"{s>>4}:{s&15}")
                sub = " ".join(parts)
            L.append(f"- `{name.decode()}`：{b[2]} B，JPEG 标记 "
                     f"{marks[:8]}，SOF0 采样 {sub}")
    L.append("")


def dump_ps(d, L):
    """VPS/SPS/PPS：从 stsd 的 hvcC 里取，展开十六进制 + 解析字段。"""
    hv = find(d, 0, len(d), b"hvcC")
    L.append("### 参数集（来自 hvcC）")
    L.append("")
    if not hv:
        L.append("（找不到 hvcC）")
        return
    body = bytes(d[hv[0] + 8:])
    p = 23
    arrays = []
    for _ in range(body[22]):
        at = body[p]
        n = struct.unpack(">H", body[p + 1:p + 3])[0]
        p += 3
        for _ in range(n):
            ln = struct.unpack(">H", body[p:p + 2])[0]
            p += 2
            arrays.append((at & 0x3F, bytes(body[p:p + ln])))
            p += ln
    for t, nal in arrays:
        L.append(f"**{NALNAME.get(t, t)}**（{len(nal)} B）")
        L.append("")
        L.append(f"```\n{hx(nal)}\n```")
        L.append("")


def dump_samples(d, trak, L, n_show=3):
    stbl = find(d, trak[0], trak[0] + trak[2], b"stbl")
    sz = find(d, stbl[0], stbl[0] + stbl[2], b"stsz")
    co = find(d, stbl[0], stbl[0] + stbl[2], b"co64")
    vf, ssz, cnt = struct.unpack(">III", d[sz[0] + 8:sz[0] + 20])
    sizes = [ssz] * cnt if ssz else list(
        struct.unpack(f">{cnt}I", d[sz[0] + 20:sz[0] + 20 + 4 * cnt]))
    nch = u32(d, co[0] + 12)
    offs = list(struct.unpack(f">{nch}Q", d[co[0] + 16:co[0] + 16 + 8 * nch]))
    L.append(f"### 样本内部 NAL 结构（共 {cnt:,} 个样本）")
    L.append("")
    shown = list(range(min(n_show, cnt)))
    for i in shown:
        b = bytes(d[offs[0] + sum(sizes[:i]):offs[0] + sum(sizes[:i]) + sizes[i]])
        types, p = [], 0
        while p + 4 <= len(b):
            ln = struct.unpack(">I", b[p:p + 4])[0]
            if p + 4 + ln > len(b):
                break
            types.append(NALNAME.get((b[p + 4] >> 1) & 0x3F, (b[p + 4] >> 1) & 0x3F))
            p += 4 + ln
        L.append(f"- 样本 #{i}（{sizes[i]:,} B）：`{' + '.join(map(str, types))}`")
    # IDR 位置
    idr = [i + 1 for i in range(min(cnt, 4000))
           if any(((bytes(d[offs[0]+sum(sizes[:i]):offs[0]+sum(sizes[:i])+sizes[i]])[q+4] >> 1) & 0x3F) in (19, 20, 21)
                  for q in [0])]
    L.append("")
    L.append(f"- 首个 IDR 位置（1-based，前 4000 个样本内）：{idr[:6]} … 共 {len(idr)}")
    L.append("")


def dump_chunks(d, trs, L, head=4, tail=3):
    L.append("### mdat 分块交错布局")
    L.append("")
    md = d.find(b"mdat", 4) - 4
    mend = md + struct.unpack(">I", d[md:md + 4])[0]
    L.append(f"mdat 盒偏移 {md}，大小 {mend-md}，体 {md+8}..{mend}")
    L.append("")
    info = []
    for idx, nm in ((0, "视频"), (1, "音频")):
        tr = trs[idx]
        stbl = find(d, tr[0], tr[0] + tr[2], b"stbl")
        sc = find(d, stbl[0], stbl[0] + stbl[2], b"stsc")
        co = find(d, stbl[0], stbl[0] + stbl[2], b"co64")
        sz = find(d, stbl[0], stbl[0] + stbl[2], b"stsz")
        cnt = u32(d, sz[0] + 16)
        nch = u32(d, co[0] + 12)
        ne = u32(d, sc[0] + 12)
        ent = [struct.unpack(">III", d[sc[0] + 16 + 12 * k:sc[0] + 28 + 12 * k])
               for k in range(ne)]
        per = []
        for k, (f, pp, s) in enumerate(ent):
            nx = ent[k + 1][0] if k + 1 < len(ent) else nch + 1
            per += [pp] * (nx - f)
        if per and sum(per) != cnt:
            per[-1] -= sum(per) - cnt
        offs = list(struct.unpack(f">{nch}Q", d[co[0] + 16:co[0] + 16 + 8 * nch]))
        info.append((nm, cnt, nch, per, offs, ent))
    L.append(f"| 轨道 | 样本数 | 块数 | stsc | 每块样本 |")
    L.append(f"|---|---|---|---|---|")
    for nm, cnt, nch, per, offs, ent in info:
        L.append(f"| {nm} | {cnt:,} | {nch:,} | "
                 f"{' '.join(f'({a},{b})' for a,b,_ in ent)} | "
                 f"{sorted(set(per))} |")
    L.append("")
    L.append("| 顺序 | 块 | 偏移 | 字节数 | 累计结束 |")
    L.append("|---|---|---|---|---|")
    seq = []
    for k in range(max(len(info[0][3]), len(info[1][3]))):
        for nm, cnt, nch, per, offs, ent in info:
            if k < len(per):
                s0 = sum(per[:k])
                nb = (sum(offs[k:k+1]) and 0) or None
                seq.append((nm, k, offs[k], per[k], s0))
    L.append(f"（共 {len(seq)} 块，下面只列前 {head} 与后 {tail}）")
    L.append("")
    for nm, k, off, ns, s0 in seq[:head] + seq[-tail:]:
        L.append(f"| | {nm}块#{k} | {off:,} | {ns} 样本 | |")
    L.append("")


def dump(path):
    d = Path(path).read_bytes()
    L = []
    L.append(f"## {Path(path).name}")
    L.append("")
    L.append(f"- 大小：**{len(d):,} B**")
    L.append(f"- SHA256：`{hashlib.sha256(d).hexdigest().upper()}`")
    L.append("")

    # 顶层
    L.append("### 顶层布局")
    L.append("")
    L.append("| 偏移 | 类型 | 大小 |")
    L.append("|---|---|---|")
    tree = []
    walk(d, 0, len(d), 0, tree)
    for depth, off, typ, size, extra in tree:
        if depth == 0:
            L.append(f"| {off:,} | `{typ}` | {size:,} |")
    L.append("")

    L.append("### 完整盒树")
    L.append("")
    L.append("```")
    for depth, off, typ, size, extra in tree:
        L.append(f"{'  '*depth}{typ:<10} @{off:<12,} {size:>12,}")
    L.append("```")
    L.append("")

    # ftyp
    ft = find(d, 0, len(d), b"ftyp")
    L.append("### ftyp")
    L.append("")
    L.append(f"- 偏移 {ft[0]}，大小 {ft[2]}")
    L.append(f"- major_brand = `{bytes(d[ft[0]+8:ft[0]+12]).decode('latin1')}`")
    L.append(f"- minor_version = {u32(d, ft[0]+12)}")
    comp = [bytes(d[ft[0]+16+4*k:ft[0]+20+4*k]).decode('latin1')
            for k in range((ft[2] - 16) // 4)]
    L.append(f"- compatible = {comp}")
    L.append("")

    # mvhd
    mv = find(d, 0, len(d), b"mvhd")
    L.append("### mvhd（影片头）")
    L.append("")
    ver = d[mv[0] + 8]
    L.append(f"- 偏移 {mv[0]}，大小 {mv[2]}，version={ver}")
    L.append(f"- creation_time = {u32(d, mv[0]+12)}，modification_time = {u32(d, mv[0]+16)}")
    L.append(f"- **timescale = {u32(d, mv[0]+20)}**")
    L.append(f"- **duration = {u32(d, mv[0]+24)}** → {u32(d, mv[0]+24)/u32(d, mv[0]+20):.4f} s")
    L.append(f"- rate = {struct.unpack('>I', d[mv[0]+28:mv[0]+32])[0]}")
    L.append(f"- next_track_ID = {u32(d, mv[0]+mv[2]-4)}")
    L.append("")

    trs = track_list(d)
    for idx, nm in ((0, "视频轨"), (1, "音频轨")):
        if idx >= len(trs):
            continue
        tr = trs[idx]
        L.append(f"### {nm}")
        L.append("")
        tk = find(d, tr[0], tr[0] + tr[2], b"tkhd")
        L.append(f"- `tkhd` 偏移 {tk[0]}，大小 {tk[2]}，version={d[tk[0]+8]}")
        L.append(f"  - track_ID = {u32(d, tk[0]+20)}")
        L.append(f"  - **duration = {u32(d, tk[0]+28)}**（影片时基）")
        L.append(f"  - width/height = {u32(d, tk[0]+tk[2]-8)>>16} x "
                 f"{u32(d, tk[0]+tk[2]-4)>>16}")
        ed = find(d, tr[0], tr[0] + tr[2], b"edts")
        if ed:
            el = find(d, ed[0], ed[0] + ed[2], b"elst")
            n = u32(d, el[0] + 12)
            L.append(f"- `edts/elst` 偏移 {el[0]}，{n} 条")
            for k in range(n):
                p = el[0] + 16 + 12 * k
                seg, mt, rate = struct.unpack(">IiH", d[p:p + 10])
                L.append(f"  - 段{k+1}: segment_duration={seg} media_time={mt} rate={rate}")
        else:
            L.append("- 无 `edts`")
        md = find(d, tr[0], tr[0] + tr[2], b"mdhd")
        L.append(f"- `mdhd` 偏移 {md[0]}，大小 {md[2]}")
        L.append(f"  - **timescale = {u32(d, md[0]+20)}**，"
                 f"**duration = {u32(d, md[0]+24)}** → "
                 f"{u32(d, md[0]+24)/u32(d, md[0]+20):.4f} s")
        L.append(f"  - language = {struct.unpack('>H', d[md[0]+28:md[0]+30])[0]}")
        stbl = find(d, tr[0], tr[0] + tr[2], b"stbl")
        stsd = find(d, stbl[0], stbl[0] + stbl[2], b"stsd")
        if idx == 0:
            dump_video_entry(d, stsd, L)
        else:
            inner = stsd[0] + 16
            esz = u32(d, inner)
            entry = bytes(d[inner:inner + esz])
            L.append(f"- `stsd` 偏移 {stsd[0]}，entry `{entry[4:8].decode('latin1')}` "
                     f"大小 {esz}")
            L.append(f"  - 前 36 字节：`{hx(entry[:36])}`")
        stts = find(d, stbl[0], stbl[0] + stbl[2], b"stts")
        n = u32(d, stts[0] + 12)
        ent = [struct.unpack(">II", d[stts[0] + 16 + 8 * k:stts[0] + 24 + 8 * k])
               for k in range(n)]
        L.append(f"- `stts` 偏移 {stts[0]}，{n} 条：{ent[:4]}")
        sc = find(d, stbl[0], stbl[0] + stbl[2], b"stsc")
        ne = u32(d, sc[0] + 12)
        L.append(f"- `stsc` 偏移 {sc[0]}，{ne} 条：" + str(
            [struct.unpack(">III", d[sc[0] + 16 + 12 * k:sc[0] + 28 + 12 * k])
             for k in range(ne)]))
        co = find(d, stbl[0], stbl[0] + stbl[2], b"co64")
        if co:
            L.append(f"- `co64` 偏移 {co[0]}，块数 {u32(d, co[0]+12)}")
        ss = find(d, stbl[0], stbl[0] + stbl[2], b"stss")
        if ss:
            k = u32(d, ss[0] + 12)
            lst = list(struct.unpack(f">{min(k,8)}I", d[ss[0] + 16:ss[0] + 16 + 4 * min(k,8)]))
            L.append(f"- `stss` 偏移 {ss[0]}，{k} 条，前 8 个：{lst}")
        L.append("")

    # NCDT
    nd = find(d, 0, len(d), b"udta")
    if nd:
        n2 = find(d, nd[0] + 8, nd[0] + nd[2], b"NCDT")
        if n2:
            dump_ncdt(d, n2, L)

    dump_ps(d, L)
    dump_samples(d, trs[0], L)
    dump_chunks(d, trs, L)
    return "\n".join(L)


if __name__ == "__main__":
    for a in sys.argv[1:]:
        print(dump(a))
        print("\n---\n")
