"""附加审查：按「尼康固件自己的 ISO BMFF 解析器」所接受的子集，独立读一遍输出 MOV。

约束来源（外部研究，非本仓库实测）
    WonderLantern/z9-qemu 的 tools/isobmff_raw_index.py（GPL-2.0-or-later）
    —— 它专门索引**固件自己写出来的**电影，它的拒绝条件就是固件那份解析器的口味：
        · 顶层盒必须自洽；恰好一个 moov，且 moov <= 16 MiB
        · 每个 trak 的 stco / co64 二选一（不能都有、不能都没有）
        · stsc 首项必须从 chunk 1 开始、samples-per-chunk != 0、描述索引 == 1、块号严格递增
        · 各表必须 version0 且字节长度与计数严格自洽（不许多、不许少）
        · 由 stsc 展开的样本必须落在 mdat 内、位置单调、不重叠
        · **只要出现非零 ctts（帧重排）就判不合格**
    docs/shared221.md 还给了对照方法：把自家索引与 ffprobe 的逐包清单对齐
    （他们 4443 条 packet 条目、含 >4 GB 偏移全部对上）。本工具照做。

为什么要单独一个工具，而不是塞进 verify-output.py
    1) 这是**独立实现**：只用 struct 手撕 MP4，不 import 仓库里的 boxio，
       所以它能反过来验证我们的工具（同源 bug 抓不出来，异源可以）。
    2) 它**不动** tools/verify-output.py 的 43 项门禁语义 —— 这是附加项，
       默认单独运行；`verify-output.py --extra` 会调用它并只加一块附加报告。

用法
    python tools/nikon-box-audit.py 输出.MOV [--no-ffprobe] [--quiet]
退出码：0 全部通过；1 有未通过项；并打印 `##CAMMOV {.."k":"extra"..}` 供脚本读取。
"""
import argparse
import json
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

from binpath import FFPROBE                                 # noqa: E402
# 注意：**不要**用 FFMPEG.replace("ffmpeg.exe", "ffprobe.exe") —— binpath 给的是
# "...\ffmpeg.EXE"（大写扩展名），replace 匹配不上，结果会拿 ffmpeg 当 ffprobe 跑，
# 输出为空、对照静默失败。verify-output.py 里原来那行完全相同写法的死代码已在
# 2026-10-17 删掉（它 import 了 FFPROBE 又被这行覆盖，且从未被使用）。

HANDLER = {"vide": "视频", "soun": "音频", "meta": "元数据"}
TABLE_ROW_CAP = 1000000          # 尼康解析器对可展开表的行数上限（超过即拒绝）


# --------------------------------------------------------------------------- #
# 纯 struct 的盒子读取（独立实现，刻意不复用 boxio）
# --------------------------------------------------------------------------- #
def iter_boxes(d, start, end):
    """产出 (kind, box_start, body_start, body_len)；边界不合法直接抛 ValueError。"""
    at = start
    while at + 8 <= end:
        size = struct.unpack_from(">I", d, at)[0]
        kind = bytes(d[at + 4:at + 8])
        head = 8
        if size == 1:
            if at + 16 > end:
                raise ValueError(f"{kind!r} 扩展盒头越界 @{at:,}")
            size = struct.unpack_from(">Q", d, at + 8)[0]
            head = 16
        elif size == 0:
            size = end - at
        if size < head or at + size > end:
            raise ValueError(f"{kind!r} 盒长越界 size={size:,} @{at:,}")
        yield kind, at, at + head, size - head
        at += size
    if at != end:
        raise ValueError(f"盒链在 {at:,} 处提前结束（还差 {end - at:,} 字节）")


def children(d, box):
    """box 的直接子盒，按类型分组（保留重复）。"""
    got = {}
    for b in iter_boxes(d, box[2], box[2] + box[3]):
        got.setdefault(b[0], []).append(b)
    return got


def one(d, start, end, kind):
    got = [b for b in iter_boxes(d, start, end) if b[0] == kind]
    if len(got) != 1:
        raise ValueError(f"{kind!r} 需要恰好一个，实际 {len(got)} 个")
    return got[0]


def walk(d, box, *kinds):
    cur = box
    for k in kinds:
        cur = one(d, cur[2], cur[2] + cur[3], k)
    return cur


def read_table(d, box, cols, width=4):
    """读 version0 表：返回 (rows_flat, count, 长度是否自洽)。"""
    body, blen = box[2], box[3]
    if blen < 8:
        raise ValueError(f"{box[0]!r} 表头不足 8 字节")
    if d[body:body + 4] != b"\0\0\0\0":
        raise ValueError(f"{box[0]!r} 不是 version0 表")
    count = struct.unpack_from(">I", d, body + 4)[0]
    exact = (blen == 8 + count * cols * width)
    if count > TABLE_ROW_CAP:
        raise ValueError(f"{box[0]!r} 行数 {count:,} 超过尼康解析器上限 {TABLE_ROW_CAP:,}")
    fmt = ">" + ("Q" if width == 8 else "I") * (count * cols)
    rows = list(struct.unpack_from(fmt, d, body + 8)) if count else []
    return rows, count, exact


# --------------------------------------------------------------------------- #
def audit(path, do_ffprobe=True, quiet=False):
    p = Path(path)
    size = p.stat().st_size
    if size > (4 << 30):
        # 已知局限：目前整份读入（为了顶层盒链与表都随手可取）。4 GiB 以上文件先拒绝，
        # 而不是把内存吃光；要支持就在 read 层改成按需分页（记在 docs 参考文档里）。
        raise SystemExit(f"{p.name}: {size:,} 字节，本工具目前整份读入内存，暂限 4 GiB")
    d = p.read_bytes()
    res = []
    notes = []

    def add(ok, name, detail=""):
        res.append((name, bool(ok), detail))
        if not quiet:
            print(f"  [{'OK' if ok else '!!'}] {name}" + (f"  {detail}" if detail else ""))
        return bool(ok)

    if not quiet:
        print(f"=== 尼康子集审查 {p.name}（{len(d):,} 字节）")

    # ---- A1 顶层盒链 ----
    try:
        top = list(iter_boxes(d, 0, len(d)))
    except ValueError as e:
        add(False, "A1 顶层盒链自洽（无空洞、无越界）", str(e))
        return res, notes
    chain = " ".join(f"{k.decode('latin1')}@{s:,}+{bs - s + bl:,}" for k, s, bs, bl in top)
    add(True, "A1 顶层盒链自洽（无空洞、无越界）", chain[:200])

    moovs = [b for b in top if b[0] == b"moov"]
    mdats = [b for b in top if b[0] == b"mdat"]

    # ---- A2 moov ----
    if len(moovs) == 1:
        msz = 8 + moovs[0][3]
        add(msz <= 16 * 1024 * 1024, "A2 恰好一个 moov 且 <= 16 MiB（尼康解析器硬上限）",
            f"1 个，{msz:,} 字节 @{moovs[0][1]:,}")
    else:
        add(False, "A2 恰好一个 moov 且 <= 16 MiB（尼康解析器硬上限）", f"{len(moovs)} 个")
    if len(moovs) != 1 or len(mdats) != 1:
        notes.append("moov/mdat 数量异常，样本范围检查跳过")
        return res, notes

    mdat_d0 = mdats[0][2]                       # mdat 数据区起点
    mdat_end = mdats[0][2] + mdats[0][3]
    msz = 8 + moovs[0][3]

    # ---- 逐 trak ----
    traks = [b for b in iter_boxes(d, moovs[0][2], moovs[0][2] + moovs[0][3]) if b[0] == b"trak"]
    add(bool(traks), "A3 moov 里至少有一个 trak", f"{len(traks)} 个")
    ff_video_positions = None
    intervals = []              # 各轨的 chunk 覆盖区间，用来查 mdat 里的空洞

    for tk in traks:
        try:
            mdia = walk(d, tk, b"mdia")
            hdlr = one(d, mdia[2], mdia[2] + mdia[3], b"hdlr")
            htype = bytes(d[hdlr[2] + 8:hdlr[2] + 12]).decode("latin1", "replace")
            nm = HANDLER.get(htype, htype)
            stbl = walk(d, tk, b"mdia", b"minf", b"stbl")
            ch = children(d, stbl)
        except ValueError as e:
            add(False, f"结构 {htype}: mdia/minf/stbl/hdlr 链完整", str(e))
            continue

        # ---- stco / co64 二选一 ----
        has_stco, has_co64 = b"stco" in ch, b"co64" in ch
        add(has_stco != has_co64, f"{nm}轨 stco/co64 恰好一个（不能都有/都没有）",
            f"stco={int(has_stco)} co64={int(has_co64)}")
        if has_stco == has_co64:
            continue
        offbox = (ch[b"co64"] if has_co64 else ch[b"stco"])[0]
        width = 8 if has_co64 else 4

        # ---- stsc 形态 ----
        try:
            stsc_rows, _n, stsc_exact = read_table(d, ch[b"stsc"][0], 3)
        except (ValueError, KeyError) as e:
            add(False, f"{nm}轨 stsc 可读", str(e))
            continue
        rows = [tuple(stsc_rows[i:i + 3]) for i in range(0, len(stsc_rows), 3)]
        form_ok = (bool(rows) and rows[0][0] == 1
                   and all(r[1] != 0 for r in rows) and all(r[2] == 1 for r in rows)
                   and all(rows[i][0] < rows[i + 1][0] for i in range(len(rows) - 1)))
        add(form_ok, f"{nm}轨 stsc 合格（首块 1 / 每块样本数!=0 / 描述索引 1 / 块号严格递增）",
            f"{len(rows)} 条规则，首块={rows[0][0] if rows else '?'}")
        if not form_ok:
            continue

        # ---- 表长度自洽（version0，字节数精确）----
        bad_tables = []
        for kind, cols, w in ((b"stsz", 0, 4), (b"stsc", 3, 4), (b"stts", 2, 4),
                              (b"stss", 1, 4), (b"ctts", 2, 4),
                              (b"co64" if has_co64 else b"stco", 1, width)):
            box = ch.get(kind, [None])[0]
            if box is None:
                continue
            if kind == b"stsz":
                constant, cnt = struct.unpack_from(">II", d, box[2] + 4)
                want = 12 if constant else 12 + 4 * cnt
                if box[3] != want:
                    bad_tables.append(f"stsz {box[3]:,}!={want:,}")
                continue
            try:
                _r, _c, exact = read_table(d, box, cols, w)
            except ValueError as e:
                bad_tables.append(str(e))
                continue
            if not exact:
                bad_tables.append(f"{kind.decode()} 长度 {box[3]:,} != {8 + _c * cols * w:,}")
        add(not bad_tables, f"{nm}轨索引表长度自洽（stsz/stsc/stts/stss/偏移表）",
            "、".join(bad_tables) if bad_tables else "全部精确")

        # ---- 样本展开：范围、单调、计数 ----
        stsz = ch[b"stsz"][0]
        constant, count = struct.unpack_from(">II", d, stsz[2] + 4)
        lengths = None
        if not constant:
            lengths = list(struct.unpack_from(">" + "I" * count, d, stsz[2] + 12))
        chunks, nch, _e = read_table(d, offbox, 1, width)

        per = []
        for i, (first, spc, _sdi) in enumerate(rows):
            nxt = rows[i + 1][0] if i + 1 < len(rows) else nch + 1
            per += [spc] * (nxt - first)

        total = sum(per)
        bad_range = bad_over = 0
        prev_end = -1
        sample = 0
        pos_list = [] if (htype == "vide" and not constant) else None
        for ci, coff in enumerate(chunks, 1):
            spc = per[ci - 1] if ci - 1 < len(per) else 0
            if constant:
                # 等长样本（PCM 音频）：stsz 用固定 sample_size、样本数可达数千万，
                # 绝不逐样本展开；按块做范围/连续性检查（数学等价）。
                span = spc * constant
                if coff < prev_end:
                    bad_over += 1
                if coff < mdat_d0 or coff + span > mdat_end:
                    bad_range += 1
                prev_end = coff + span
                intervals.append((coff, coff + span))
            else:
                off = coff
                if coff < prev_end:
                    bad_over += 1
                for _k in range(spc):
                    if sample >= len(lengths):
                        bad_range += 1
                        break
                    sz = lengths[sample]
                    if sz == 0 or off < mdat_d0 or off + sz > mdat_end:
                        bad_range += 1
                    if off < prev_end:
                        bad_over += 1
                    if pos_list is not None:
                        pos_list.append((off, sz))
                    prev_end = off + sz
                    off += sz
                    sample += 1
                intervals.append((coff, off))
        add(total == count, f"{nm}轨 stsc 展开样本数 == stsz 计数",
            f"{total:,} vs {count:,}")
        add(bad_range == 0, f"{nm}轨样本全部落在 mdat 数据区内",
            f"{bad_range} 个越界样本（mdat 数据区 {mdat_d0:,}..{mdat_end:,}）")
        add(bad_over == 0, f"{nm}轨样本位置单调、不重叠", f"{bad_over} 处逆序")

        # ---- 无重排 ----
        if b"ctts" in ch:
            rows_c, _c, _e = read_table(d, ch[b"ctts"][0], 2)
            nz = [v for i, v in enumerate(rows_c) if i % 2 == 1 and v]
            add(not nz, f"{nm}轨无重排（ctts 全 0）",
                f"非零合成偏移 {len(nz):,} 个，最大 {max(nz) if nz else 0}")
        else:
            add(True, f"{nm}轨无重排（没有 ctts 盒）", "PTS==DTS")

        if pos_list is not None:
            ff_video_positions = pos_list

    # ---- mdat 覆盖：样本是否恰好铺满数据区（空洞 = 写盘索引漏段/偏移被挪动的信号）----
    if intervals:
        iv = sorted(intervals)
        holes, cur, overlaps = [], mdat_d0, 0
        for s, e in iv:
            if s < cur:
                overlaps += 1
            elif s > cur:
                holes.append((cur, s))
            cur = max(cur, e)
        if cur < mdat_end:
            holes.append((cur, mdat_end))
        # 相机原片在 mdat 数据区首部保留了 8 字节全 0 前缀（8 个原片 + 我们所有产物实测一致：
        # 首个 sample 总在 d0+8 开始），它不是索引漏段，单独承认；其余任何空洞都判失败。
        lead = b""
        if holes and holes[0][0] == mdat_d0 and holes[0][1] - holes[0][0] == 8:
            lead = bytes(d[mdat_d0:mdat_d0 + 8])
            if lead == b"\0" * 8:
                holes = holes[1:]
                notes.append("mdat 首部 8 字节全 0 前缀（与相机原片同款，已承认）")
            else:
                notes.append(f"mdat 首部 8 字节不是全 0：{lead.hex(' ')}")
        gap_bytes = sum(e - hs for hs, e in holes)
        detail = (f"{len(holes)} 处空洞、共 {gap_bytes:,} 字节："
                  + "、".join(f"{hs:,}..{e:,}" for hs, e in holes[:4])) if holes \
            else (f"0 处空洞（首部前缀 {lead.hex(' ') if lead else '无'}）")
        add(not holes, "mdat 数据区被样本铺满（无空洞；相机同款的 8 字节首部前缀除外）", detail)
        # 8/8 相机原片 + 我们全部产物：首个样本都紧接那 8 字节前缀。这条是「与相机一致」，
        # 不是固件解析器的硬规则（所以措辞里写明依据）。
        first_off = min(hs for hs, _e in intervals)
        add(first_off == mdat_d0 + 8, "首个样本紧接 mdat 首部 8 字节前缀（相机同款）",
            f"{first_off:,}，相机同款期望 {mdat_d0 + 8:,}")
        if overlaps:
            notes.append(f"mdat 内有 {overlaps} 处跨轨样本区间交叠")

    # ---- A-ffprobe：独立核对（他们的 shared221 也是这么对齐的）----
    if do_ffprobe:
        if ff_video_positions is None:
            notes.append("没有可对照的视频样本表，跳过 ffprobe 对照")
        else:
            r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "packet=pos,size", "-of", "json", str(p)],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
            try:
                pk = json.loads(r.stdout or "{}").get("packets", [])
            except json.JSONDecodeError:
                pk = []
            n_cmp = min(len(pk), len(ff_video_positions))
            diff = [i for i in range(n_cmp)
                    if int(pk[i].get("pos", -1)) != ff_video_positions[i][0]
                    or int(pk[i].get("size", -1)) != ff_video_positions[i][1]]
            add(len(pk) == len(ff_video_positions) and not diff,
                "ffprobe 逐包核对样本偏移与长度（独立读法）",
                f"ffprobe {len(pk):,} 包 vs 索引 {len(ff_video_positions):,} 个样本，"
                f"不一致 {len(diff)} 处" + (f"，首个 @#{diff[0]}" if diff else ""))

    return res, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("output")
    ap.add_argument("--no-ffprobe", action="store_true", help="跳过 ffprobe 逐包对照")
    ap.add_argument("--quiet", action="store_true", help="只打印结论")
    a = ap.parse_args()
    path = Path(a.output)
    if not path.exists():
        raise SystemExit(f"找不到 {path}")
    res, notes = audit(path, do_ffprobe=not a.no_ffprobe, quiet=a.quiet)
    ok = [n for n, o, _x in res if o]
    bad = [n for n, o, _x in res if not o]
    for n in notes:
        print(f"  -- {n}")
    print(f"\n=== 附加（尼康子集）结论：{len(ok)} 项通过，{len(bad)} 项未通过")
    for n in bad:
        print(f"  未通过: {n}")
    print("##CAMMOV " + json.dumps(
        {"k": "extra", "ok": len(ok), "bad": len(bad), "total": len(res),
         "failed": bad}, ensure_ascii=False), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
