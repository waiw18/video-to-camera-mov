"""生成《可用文件逐字节展开》文档。

把**真机验证过能播**的文件逐个逐字节展开，并在开头给出
「所有能播文件都相同的字段」清单（= 相机真正接受的公共不变量）。
"""
import hashlib
import importlib.util
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

_s = importlib.util.spec_from_file_location("bd", HERE / "byte-dump.py")
bd = importlib.util.module_from_spec(_s)
_s.loader.exec_module(bd)
from boxio import find, track_list, u32   # noqa: E402

ROOT = HERE.parent

# 真机验证过能播的（用户确认）
VERIFIED = [
    ("DSC_4450.MOV", "output/verified/DSC_4450.MOV",
     "相机原片逐字节复制、只改文件名 —— 验证「外部文件被接受」这一步"),
    ("DSC_4470.MOV", "output/verified/DSC_4470.MOV",
     "**相机原生码流 + 本工具重建的容器** —— 验证容器重建正确"),
    ("DSC_4496.MOV", "output/verified/DSC_4496.MOV",
     "720p29.97 彩条 10s → 1080p59.94（早期成功案例）"),
    ("DSC_4500.MOV", "output/verified/DSC_4500.MOV",
     "1080p25 SMPTE 8s（不同帧率也能播）"),
    ("DSC_4512.MOV", "output/verified/DSC_4512.MOV",
     "**720p30 → 1080p59.94 全长 4分15秒**（40 Mbps）"),
    ("DSC_4521.MOV", "output/verified/DSC_4521.MOV",
     "**720p30 → 1080p59.94 全长，15 Mbps** —— 真机确认能播且**快进正常**"),
]

# 相机自己拍的（对照标准答案）
CAMERA = [
    ("DSC_8955.MOV", "cam-tests/DSC_8955.MOV", "1080p60（59.94）—— 主模板"),
    ("DSC_8956.MOV", "cam-tests/DSC_8956.MOV", "1080p60（59.94）"),
    ("DSC_8960.MOV", "cam-tests/DSC_8960.MOV", "**1080p30（29.97）** —— 30fps 的标准答案"),
    ("DSC_8951.MOV", "cam-tests/DSC_8951.MOV", "4K"),
]


def facts(path):
    """抽取用于横向对比的关键事实。"""
    d = Path(path).read_bytes()
    trs = track_list(d)
    f = {}
    f["大小"] = len(d)
    ft = find(d, 0, len(d), b"ftyp")
    f["ftyp"] = bytes(d[ft[0] + 8:ft[0] + 12]).decode("latin1")
    f["ftyp minor"] = u32(d, ft[0] + 12)
    mv = find(d, 0, len(d), b"mvhd")
    f["mvhd.timescale"] = u32(d, mv[0] + 20)
    f["mvhd.duration"] = u32(d, mv[0] + 24)
    for idx, nm in ((0, "视频"), (1, "音频")):
        if idx >= len(trs):
            continue
        tr = trs[idx]
        md = find(d, tr[0], tr[0] + tr[2], b"mdhd")
        f[f"{nm}mdhd.timescale"] = u32(d, md[0] + 20)
        f[f"{nm}mdhd.duration"] = u32(d, md[0] + 24)
        tk = find(d, tr[0], tr[0] + tr[2], b"tkhd")
        f[f"{nm}tkhd.duration"] = u32(d, tk[0] + 28)
        stbl = find(d, tr[0], tr[0] + tr[2], b"stbl")
        sz = find(d, stbl[0], stbl[0] + stbl[2], b"stsz")
        f[f"{nm}样本数"] = u32(d, sz[0] + 16)
        co = find(d, stbl[0], stbl[0] + stbl[2], b"co64")
        f[f"{nm}块数"] = u32(d, co[0] + 12) if co else None
        sc = find(d, stbl[0], stbl[0] + stbl[2], b"stsc")
        ne = u32(d, sc[0] + 12)
        f[f"{nm}stsc"] = " ".join(
            f"({a},{b})" for a, b, _ in
            [struct.unpack(">III", d[sc[0] + 16 + 12 * k:sc[0] + 28 + 12 * k])
             for k in range(ne)])
        st = find(d, stbl[0], stbl[0] + stbl[2], b"stts")
        n = u32(d, st[0] + 12)
        f[f"{nm}stts"] = str([struct.unpack(">II", d[st[0] + 16 + 8 * k:st[0] + 24 + 8 * k])
                              for k in range(n)])
        ss = find(d, stbl[0], stbl[0] + stbl[2], b"stss")
        f[f"{nm}stss条数"] = u32(d, ss[0] + 12) if ss else None
        ed = find(d, tr[0], tr[0] + tr[2], b"edts")
        f[f"{nm}有edts"] = bool(ed)
    nd = find(d, 0, len(d), b"udta")
    if nd:
        n2 = find(d, nd[0] + 8, nd[0] + nd[2], b"NCDT")
        if n2:
            f["NCDT子盒"] = " ".join(x[1].decode("latin1") for x in bd.children(d, n2))
            nctg = find(d, n2[0] + 8, n2[0] + n2[2], b"NCTG")
            p, end = nctg[0] + 8, nctg[0] + nctg[2]
            while p + 8 <= end:
                tag = struct.unpack(">I", d[p:p + 4])[0]
                fmt = struct.unpack(">H", d[p + 4:p + 6])[0]
                cnt = struct.unpack(">H", d[p + 6:p + 8])[0]
                nb = bd.UNIT.get(fmt, 1) * cnt
                if tag == 0 or p + 8 + nb > end:
                    break
                if tag in (0x16, 0x17) and fmt == 5:
                    f[f"NCTG {hex(tag)}"] = (
                        f"{struct.unpack('>I', d[p+8:p+12])[0]}/"
                        f"{struct.unpack('>I', d[p+12:p+16])[0]}")
                if tag == 0x101:
                    # 值是 16 位序列（160,120,W,H），不是 32 位 —— 按 16 位解
                    f["NCTG 0x101"] = str([
                        struct.unpack(">H", d[p + 8 + 2 * k:p + 10 + 2 * k])[0]
                        for k in range(min(cnt * 2, 8))])
                p += 8 + nb
    hv = find(d, 0, len(d), b"hvcC")
    if hv:
        body = bytes(d[hv[0] + 8:hv[0] + 8 + 23])
        f["hvcC[1]"] = f"{body[1]:02X}"
        f["hvcC level"] = body[12]
        f["hvcC numOfArrays"] = body[22]
    return f


def main():
    L = []
    L.append("# 可用文件逐字节展开")
    L.append("")
    L.append("> 本文档把**真机（Nikon Z5II）验证过能播**的文件逐个逐字节展开。")
    L.append("> 接手的人只需看这份文档，就能知道一个能播的文件每个字节是什么。")
    L.append("")
    L.append("**判据来源**：这些文件是用户拿相机实测确认能播的。")
    L.append("**不要用推断代替真机结论** —— 本轮就吃过亏：")
    L.append("我按「相机原片 tier=1」推断「相机要 High tier」，结果把能播的配置改成不能播的。")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 1. 清单")
    L.append("")
    L.append("### 1.1 本工具产出、真机验证能播")
    L.append("")
    L.append("| 文件 | 说明 | 大小 | SHA256(前16) |")
    L.append("|---|---|---|---|")
    for n, p, desc in VERIFIED:
        f = ROOT / p
        if not f.exists():
            L.append(f"| `{n}` | {desc} | （本机缺失） | |")
            continue
        h = hashlib.sha256(f.read_bytes()).hexdigest().upper()[:16]
        L.append(f"| `{n}` | {desc} | {f.stat().st_size:,} B | `{h}` |")
    L.append("")
    L.append("### 1.2 相机自己拍的（对照标准答案）")
    L.append("")
    L.append("| 文件 | 说明 | 大小 | SHA256(前16) |")
    L.append("|---|---|---|---|")
    for n, p, desc in CAMERA:
        f = ROOT / p
        if not f.exists():
            L.append(f"| `{n}` | {desc} | （本机缺失） | |")
            continue
        h = hashlib.sha256(f.read_bytes()).hexdigest().upper()[:16]
        L.append(f"| `{n}` | {desc} | {f.stat().st_size:,} B | `{h}` |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 2. 横向对比（找出「所有能播文件都相同」的字段）")
    L.append("")
    L.append("★ 这一节最有价值：**能播的文件之间相同的字段，就是相机接受的不变量。**")
    L.append("")

    rows = []
    for n, p, _ in VERIFIED + CAMERA:
        f = ROOT / p
        if f.exists():
            rows.append((n, facts(f)))
    keys = []
    for _, f in rows:
        for k in f:
            if k not in keys:
                keys.append(k)
    L.append("| 字段 | " + " | ".join(n.replace(".MOV", "") for n, _ in rows) + " | 一致? |")
    L.append("|---" * (len(rows) + 2) + "|")
    for k in keys:
        vals = [str(f.get(k, "—")) for _, f in rows]
        uniq = set(vals)
        mark = "✅" if len(uniq) == 1 else ("—" if k in ("大小", "mvhd.duration", "视频mdhd.duration",
              "音频mdhd.duration", "视频tkhd.duration", "音频tkhd.duration",
              "视频样本数", "音频样本数", "视频块数", "音频块数", "视频stts", "音频stts",
              "视频stsc", "音频stsc", "视频stss条数", "mvhd.timescale", "视频mdhd.timescale",
              "音频mdhd.timescale") else "❌")
        L.append(f"| `{k}` | " + " | ".join(vals) + f" | {mark} |")
    L.append("")
    L.append("> ✅ = 所有能播文件都相同（**不变量**）；❌ = 有差异，需注意；")
    L.append("> — = 本来就该随内容变化（大小/时长/样本数/分块表）。")
    L.append("")
    L.append("### 2.1 从这张表里能直接读出来的结论")
    L.append("")
    L.append("**（a）硬不变量（10 个文件全一致，改了就废）**")
    L.append("")
    L.append("```")
    L.append("ftyp major_brand = \"qt  \"        ftyp minor_version = 538315008")
    L.append("视频轨有 edts/elst，音频轨没有 edts")
    L.append("音频 mdhd.timescale = 48000，音频没有 stss")
    L.append("音频每块样本数 = 24024（= 0.5 秒 @48kHz）")
    L.append("NCDT 子盒顺序 = NCHD NCTG NCTH NCVW NCM1 NCM2 NCDB")
    L.append("```")
    L.append("")
    L.append("**（b）分块规则 = 每 0.5 秒一块（看 `视频stsc` / `音频stsc` 两行）**")
    L.append("")
    L.append("```")
    L.append("59.94 帧率（时基 60000）: 视频每块 30 帧  音频每块 24024 样本")
    L.append("29.97 帧率（时基 30000）: 视频每块 15 帧  音频每块 24024 样本   <- 相机 DSC_8960 铁证")
    L.append("```")
    L.append("")
    L.append("**（c）★ 值得注意的模式：6 个能播的工具产出，`tkhd.duration` 全是 228228 / 228220**")
    L.append("")
    L.append("也就是**模板 DSC_8955 的原始值，从来没被改过**：")
    L.append("")
    L.append("```")
    L.append("DSC_4470  视频时长 10.20 s   tkhd.duration = 228228 (= 3.80 s)  能播")
    L.append("DSC_4496  视频时长 10.01 s   tkhd.duration = 228228            能播")
    L.append("DSC_4500  视频时长  8.01 s   tkhd.duration = 228228            能播")
    L.append("DSC_4512  视频时长 254.70 s  tkhd.duration = 228228            能播")
    L.append("DSC_4521  视频时长 254.70 s  tkhd.duration = 228228            能播且快进正常")
    L.append("相机原片  各自正确值                                            能播")
    L.append("```")
    L.append("")
    L.append("**相机自己的文件写的是正确值，我的能播文件写的都是错的** —— 两边都能播，")
    L.append("所以 `tkhd.duration` **不是硬判据**；但它是我所有能播文件唯一的共同点，")
    L.append("而后续改动（写成正确值）的文件全部失败。**这条要单独做实验验证，不要凭它下结论。**")
    L.append("")
    L.append("**（d）帧率相关的字段（随输出帧率变）**")
    L.append("")
    L.append("```")
    L.append("mvhd.timescale = 视频 mdhd.timescale = NCDT 0x16/0x17 的分子")
    L.append("   59.94 -> 60000 / 60000/1001")
    L.append("   29.97 -> 30000 / 30000/1001")
    L.append("stts delta 一律 1001")
    L.append("```")
    L.append("")
    L.append("---")
    L.append("")

    L.append("## 3. 逐文件逐字节展开")
    L.append("")
    for n, p, desc in VERIFIED:
        f = ROOT / p
        if not f.exists():
            continue
        L.append(f"> **{desc}**")
        L.append("")
        L.append(bd.dump(str(f)))
        L.append("")
        L.append("---")
        L.append("")

    L.append("## 4. 相机原片（标准答案）")
    L.append("")
    for n, p, desc in CAMERA:
        f = ROOT / p
        if not f.exists():
            continue
        L.append(f"> **{desc}**")
        L.append("")
        L.append(bd.dump(str(f)))
        L.append("")
        L.append("---")
        L.append("")

    out = ROOT / "docs" / "可用文件逐字节展开.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"  已写入 {out}  {out.stat().st_size/1024:.1f} KB，{len(L)} 行")


if __name__ == "__main__":
    main()
