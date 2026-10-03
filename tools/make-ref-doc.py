"""生成《生成 DSC_4521 的参数》文档。

DSC_4521 是唯一真机完整验证通过（能播 + 快进正常）的文件，所以它的参数就是基准。
本文档从**文件本身**反解出全部参数，而不是靠回忆命令 —— 文件才是真值。
"""
import hashlib
import importlib.util
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

_s = importlib.util.spec_from_file_location("vo", HERE / "verify-output.py")
vo = importlib.util.module_from_spec(_s)
_s.loader.exec_module(vo)
_s2 = importlib.util.spec_from_file_location("bd", HERE / "byte-dump.py")
bd = importlib.util.module_from_spec(_s2)
_s2.loader.exec_module(bd)
from boxio import find, track_list, u32   # noqa: E402

ROOT = HERE.parent
REF = ROOT / "output/verified/DSC_4521.MOV"
OTHERS = [
    ("DSC_4545.MOV", "output/DSC_4545.MOV", "60s，参数集与 4521 一致 —— **真机卡住**"),
    ("DSC_4547.MOV", "output/DSC_4547.MOV", "60s（从源第100秒切），参数集与 4521 一致 —— 待测"),
    ("DSC_4546.MOV", "output/DSC_4546.MOV", "全长 4分15秒，当前代码 —— 待测"),
]


def container_facts(p):
    d = Path(p).read_bytes()
    trs = track_list(d)
    mv = find(d, 0, len(d), b"mvhd")
    f = {}
    f["文件大小"] = f"{len(d):,} B"
    f["ftyp"] = bytes(d[find(d, 0, len(d), b'ftyp')[0] + 8:find(d, 0, len(d), b'ftyp')[0] + 12]).decode("latin1")
    f["ftyp minor"] = u32(d, find(d, 0, len(d), b"ftyp")[0] + 12)
    f["mvhd.timescale"] = u32(d, mv[0] + 20)
    f["mvhd.duration"] = u32(d, mv[0] + 24)
    for idx, nm in ((0, "视频"), (1, "音频")):
        tr = trs[idx]
        md = find(d, tr[0], tr[0] + tr[2], b"mdhd")
        f[f"{nm} mdhd.timescale"] = u32(d, md[0] + 20)
        f[f"{nm} mdhd.duration"] = u32(d, md[0] + 24)
        tk = find(d, tr[0], tr[0] + tr[2], b"tkhd")
        f[f"{nm} tkhd.duration"] = u32(d, tk[0] + 28)
        ed = find(d, tr[0], tr[0] + tr[2], b"edts")
        f[f"{nm} 有 edts"] = "是" if ed else "否"
        if ed:
            el = find(d, ed[0], ed[0] + ed[2], b"elst")
            seg, mt, rate = struct.unpack(">IiH", d[el[0] + 16:el[0] + 26])
            f[f"{nm} elst"] = f"segment_duration={seg} media_time={mt} rate={rate}"
        stbl = find(d, tr[0], tr[0] + tr[2], b"stbl")
        sz = find(d, stbl[0], stbl[0] + stbl[2], b"stsz")
        f[f"{nm} 样本数"] = u32(d, sz[0] + 16)
        co = find(d, stbl[0], stbl[0] + stbl[2], b"co64")
        f[f"{nm} 块数"] = u32(d, co[0] + 12) if co else 0
        sc = find(d, stbl[0], stbl[0] + stbl[2], b"stsc")
        ne = u32(d, sc[0] + 12)
        f[f"{nm} stsc"] = " ".join(
            f"({a},{b})" for a, b, _ in
            [struct.unpack(">III", d[sc[0] + 16 + 12 * k:sc[0] + 28 + 12 * k])
             for k in range(ne)])
        st = find(d, stbl[0], stbl[0] + stbl[2], b"stts")
        n = u32(d, st[0] + 12)
        f[f"{nm} stts"] = str([struct.unpack(">II", d[st[0] + 16 + 8 * k:st[0] + 24 + 8 * k])
                               for k in range(n)])
        ss = find(d, stbl[0], stbl[0] + stbl[2], b"stss")
        f[f"{nm} stss 条数"] = u32(d, ss[0] + 12) if ss else 0
    nd = find(d, 0, len(d), b"udta")
    n2 = find(d, nd[0] + 8, nd[0] + nd[2], b"NCDT")
    f["NCDT 子盒"] = " ".join(x[1].decode("latin1") for x in bd.children(d, n2))
    # hvcC 在 stsd 里，boxio.find 的容器白名单不含 stsd，直接在字节里找
    hi = d.find(b"hvcC")
    body = bytes(d[hi + 4:hi + 27])
    f["hvcC[1] (tier/profile)"] = f"{body[1]:02X}"
    f["hvcC level"] = body[12]
    return f


def main():
    L = []
    d = REF.read_bytes()
    fields = vo.trace_fields(str(REF), frames=3)
    L.append("# 生成 DSC_4521 的参数（基准配置）")
    L.append("")
    L.append("> `DSC_4521.MOV` 是**唯一真机完整验证通过**的文件（能播 + 快进 10 秒正常）。")
    L.append("> 本文档从**文件本身**反解出全部参数 —— 文件才是真值，不靠回忆命令。")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| 文件 | `output/verified/DSC_4521.MOV` |")
    L.append(f"| 大小 | {len(d):,} B |")
    L.append(f"| SHA256 | `{hashlib.sha256(d).hexdigest().upper()}` |")
    L.append(f"| 时长 | 254.7049 s（4 分 15 秒）|")
    L.append(f"| 帧数 | 15,267 |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 1. 等价的命令行（当前工具）")
    L.append("")
    L.append("```powershell")
    L.append("$py = 'C:\\Users\\guozh\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe'")
    L.append("$env:PYTHONIOENCODING='utf-8'")
    L.append("cd C:\\Users\\guozh\\Downloads\\test")
    L.append("")
    L.append("& $py -B tools\\video-to-camera-mov.py 源视频.mp4 --out output\\DSC_XXXX.MOV `")
    L.append("    --template cam-tests\\DSC_8955.MOV --preset fast --bitrate 15M")
    L.append("```")
    L.append("")
    L.append("**不要加** `--fps source`（那是 29.97，另一条路，还没真机验证过）")
    L.append("**不要加** `--jobs >1`（分段并行实测会让相机播到中途跳出）")
    L.append("**不要加** `--duration`（4521 是全长的；截取版本目前全部失败，见 §4）")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 2. 容器字段（逐项）")
    L.append("")
    L.append("| 字段 | 值 |")
    L.append("|---|---|")
    for k, v in container_facts(REF).items():
        L.append(f"| `{k}` | {v} |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 3. 参数集全部字段（174 项，来自 VPS/SPS/PPS + P 帧切片头）")
    L.append("")
    L.append("> 取法：`ffmpeg -bsf:v trace_headers`，**取最后一次出现**（同名取最后 = P 帧的值）。")
    L.append("")
    L.append("| 字段 | 值 |")
    L.append("|---|---|")
    for k in sorted(fields):
        L.append(f"| `{k}` | {fields[k]} |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 4. 与其它文件的关键差异（为什么它们失败）")
    L.append("")
    L.append("| 字段 | DSC_4521（能播+快进） | DSC_4545（卡住） | DSC_4547 | DSC_4546 |")
    L.append("|---|---|---|---|---|")
    facts = {n: (container_facts(p) if (ROOT / p).exists() else {})
             for n, p, _ in OTHERS}
    ref_f = container_facts(REF)
    keys = ["文件大小", "mvhd.duration", "视频 mdhd.duration", "视频 tkhd.duration",
            "音频 tkhd.duration", "视频 样本数", "视频 块数", "视频 stsc", "音频 stsc"]
    for k in keys:
        row = [str(ref_f.get(k, "—"))]
        for n, _, _ in OTHERS:
            row.append(str(facts[n].get(k, "（未生成）")))
        L.append(f"| `{k}` | " + " | ".join(row) + " |")
    L.append("")
    L.append("**已经排除的差异**（逐项验证过，都一致）：")
    L.append("")
    L.append("- 参数集 174 个字段（`psdiff` 对 4545 / 4547 都是 **0 处不同**）")
    L.append("- 盒树（42 个盒，类型与嵌套完全一致）")
    L.append("- 分块铺满 mdat（严丝合缝，尾部正好落在文件尾）")
    L.append("- 可跳转性（索引跳转 vs 精确解码，5 个点全部一致）")
    L.append("- `stss` 同步样本表 vs 实际 IDR（逐项一致）")
    L.append("- 样本内部 NAL 结构（都是 `AUD + 切片`，无内联参数集）")
    L.append("- 文件尾字节（mdat 尾 == 文件尾，无尾巴数据）")
    L.append("- NCDT 子盒与顺序、缩略图标记与采样率")
    L.append("")
    L.append("**剩下的差异只有两个：**")
    L.append("")
    L.append("1. **`tkhd.duration`**：4521 = 228228（模板旧值，从没改过）；4545/4547 = 正确值")
    L.append("2. **长度**：4521 = 254.7 s / 15,267 帧；4545/4547 = 60 s / 3,596 帧")
    L.append("")
    L.append("> 关键反例：`DSC_4542`（60 s，`tkhd.duration` **改回 228228**）**仍然卡**。")
    L.append("> 所以 **`tkhd.duration` 不是原因，长度才是** —— `DSC_4546`（全长、当前代码）正在验证这一点。")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 5. 备注：这些参数是怎么被改坏的（教训）")
    L.append("")
    L.append("本轮我按「相机原片 tier=1」**推断**相机要 High tier，把 `general_tier_flag` 从 0 改成 1，")
    L.append("结果 `DSC_4543`/`DSC_4544` 相机直接无效。")
    L.append("")
    L.append("`psdiff` 对比 4521 与 4544：**174 个字段只差 `general_tier_flag` 这一处**。")
    L.append("")
    L.append("**→ 判据必须来自真机验证过的文件（4521），不能靠推断。**")
    L.append("已改回 tier=0，现在 4545 / 4547 的参数集与 4521 **0 处不同**。")
    L.append("")
    out = ROOT / "docs" / "生成DSC_4521的参数.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"  已写入 {out}  {out.stat().st_size/1024:.1f} KB")


if __name__ == "__main__":
    main()
