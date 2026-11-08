"""分片：把 DASH 分段（.m4s 视频 + .m4s 音频）合并成一个 MP4，再切成 N 段。

用途（界面里是「③ 分片」那张卡片，命令行也能单独跑）：
    1) B 站式的 `xxx-1-30064.m4s`（视频）+ `xxx-1-30232.m4s`（音频）先无损合并；
    2) 按 N 等分切段，**接缝处每段向前多留 --overlap 秒上一段的内容**（看的时候不漏接缝）；
    3) 切点一律对齐视频关键帧 —— 否则段头会花屏，所以实际重叠 >= --overlap；
    4) 切完逐段自检：视频/音频流齐全、时长对得上、首帧是关键帧、重叠够；
    5) 写一份 `分片清单.md`（表格，和 tools/merge-split-m4s.ps1 的格式一致）。

用法
    python tools/split-m4s.py 28.m4s --audio _28.m4s --pieces 6 --overlap 10
    python tools/split-m4s.py 某完整视频.mp4 --pieces 3 --overlap 5 --outdir .\\out
    python tools/split-m4s.py 28.m4s -a _28.m4s -n 6 -o 10 --name 28 --outdir .\\28_合并分片

参数
    video          视频文件（.m4s / .mp4 / .mkv 都行）
    -a/--audio     单独的音频文件（DASH 音频分片）；不给就是单文件直接切
    -n/--pieces    切几段，默认 6；1 = 只合并不切
    -o/--overlap   每段开头多留几秒上一段内容，默认 10
    -d/--outdir    输出目录，默认「<视频名>_合并分片」
    --name         输出文件名前缀，默认从视频名推导（去掉 -1-30064 这类后缀）
    --no-full      切完删掉合并出来的「_完整.mp4」（默认保留）
"""
import argparse
import bisect
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from binpath import FFMPEG, FFPROBE  # noqa: E402

# 中间文件 / 取消标志：与 tools/video-to-camera-mov.py、app/main.py 用同一套约定
PROBE = Path(os.environ.get("CAMMOV_WORK")
             or (Path(__file__).resolve().parent.parent / ".probe"))
CANCEL_FLAG = PROBE / "cancel.flag"

STAGES = ["合并", "关键帧", "切段", "自检", "清单"]


# ---- 界面协议（app/main.py 按行解析，不显示在日志里）-------------------------
def _emit(kind, **kw):
    print("##CAMMOV " + json.dumps({"k": kind, **kw}, ensure_ascii=False),
          flush=True)


def _stage(i, running):
    _emit("stage", step=i, total=len(STAGES), running=running)


def _prog(pct, done=None, total=None, eta=None):
    _emit("prog", pct=round(min(100.0, max(0.0, pct)), 1),
          done=round(done, 2) if done is not None else None,
          total=round(total, 2) if total is not None else None, eta=eta)


def _check_cancel(where=""):
    if CANCEL_FLAG.exists():
        CANCEL_FLAG.unlink(missing_ok=True)
        _emit("cancelled", at=where)
        raise SystemExit("已取消分片")


def run(args, label, quiet=False):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0 and not quiet:
        tail = "\n".join(((r.stderr or "") + (r.stdout or "")).splitlines()[-12:])
        raise SystemExit(f"{label} 失败（exit {r.returncode}）:\n{tail}")
    return r


def probe_duration(f):
    r = run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(f)], "读时长")
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        raise SystemExit(f"读不出时长：{f}")


def probe_streams(f):
    """返回 ({'video': dict|None, 'audio': dict|None}, 时长)"""
    r = run([FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(f)], "读流信息")
    try:
        info = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        raise SystemExit(f"ffprobe 输出无法解析：{f}")
    out = {"video": None, "audio": None}
    for s in info.get("streams", []):
        ct = s.get("codec_type")
        if ct in out and out[ct] is None:
            out[ct] = s
    try:
        dur = float(info.get("format", {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    return out, dur


def keyframes(f):
    """视频关键帧时间戳（升序）。`-skip_frame nokey` 只解析不解码，长片也就十几秒。"""
    r = run([FFPROBE, "-v", "error", "-select_streams", "v", "-skip_frame", "nokey",
             "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(f)],
            "读关键帧")
    kf = []
    for line in (r.stdout or "").splitlines():
        s = line.replace(",", "").strip()
        if not s:
            continue
        try:
            kf.append(float(s))
        except ValueError:
            pass
    kf.sort()
    if not kf:
        raise SystemExit("关键帧表为空（源视频可能没有关键帧索引）")
    return kf


def snap(kf, t):
    """取 <= t 的最后一个关键帧；比第一个还小就用第一个。"""
    i = bisect.bisect_right(kf, t + 1e-6)
    if i <= 0:
        return kf[0]
    return kf[i - 1]


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("video")
    ap.add_argument("-a", "--audio", default=None)
    ap.add_argument("-n", "--pieces", type=int, default=6)
    ap.add_argument("-o", "--overlap", type=float, default=10.0)
    ap.add_argument("-d", "--outdir", default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--no-full", action="store_true")
    a = ap.parse_args()

    video = Path(a.video)
    if not video.exists():
        raise SystemExit(f"找不到视频：{video}")
    audio = Path(a.audio) if a.audio else None
    if audio is not None and not audio.exists():
        raise SystemExit(f"找不到音频：{audio}")
    pieces = max(1, a.pieces)
    overlap = max(0.0, a.overlap)

    name = a.name
    if not name:
        name = video.stem
        for suf in ("-1-30064", "-1-30232", "-30064", "-30232", "-video", "-audio"):
            if name.endswith(suf):
                name = name[: -len(suf)]
                break
        if name.endswith("_28") is False and name.startswith("_"):
            name = name[1:]
    outdir = Path(a.outdir) if a.outdir else video.parent / f"{name}_合并分片"
    outdir.mkdir(parents=True, exist_ok=True)
    full = outdir / f"{name}_完整.mp4"

    t_all = time.time()
    print(f"== 分片：{video.name}" + (f" + {audio.name}" if audio else "")
          + f" -> {outdir}（{pieces} 段 / 重叠 >= {overlap:g} s）==")

    # ---- 1) 合并 ----
    _stage(0, "合并" + ("（-c copy）" if audio else "（单文件，跳过）"))
    if audio is None:
        full = video
        print(f"[1/5] 没有单独的音频文件，直接把 {video.name} 当作完整文件")
        total = probe_duration(full)
    else:
        if full.exists() and full.stat().st_size > 0:
            print(f"[1/5] 合并文件已存在，跳过：{full.name}")
        else:
            run([FFMPEG, "-y", "-hide_banner", "-loglevel", "warning",
                 "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                 "-c", "copy", "-movflags", "+faststart", str(full)], "合并")
        total = probe_duration(full)
    src_streams, _ = probe_streams(full)
    has_audio = src_streams["audio"] is not None
    print(f"      {full.name}  {full.stat().st_size:,} B  时长 {total:.3f} s"
          f"（{total / 60:.2f} 分钟）"
          + ("  + 音频" if has_audio else "  （无音频）"))
    _prog(5, 0, total)
    _check_cancel("合并后")

    # ---- 2) 关键帧 ----
    _stage(1, "关键帧")
    kf = keyframes(full)
    print(f"[2/5] 关键帧 {len(kf):,} 个，平均间隔 "
          f"{(kf[-1] - kf[0]) / max(1, len(kf) - 1):.2f} s")
    _prog(12, 0, total)
    _check_cancel("关键帧")

    # ---- 3) 切段 ----
    seg = total / pieces
    plan = []
    for i in range(1, pieces + 1):
        end = total if i == pieces else seg * i
        start = 0.0 if i == 1 else snap(kf, seg * (i - 1) - overlap)
        if start >= end:                       # 极端参数下别切出 0 长度的段
            start = max(0.0, end - 1.0)
        dur = end - start
        out = outdir / f"{name}_片{i}.mp4"
        plan.append(dict(i=i, start=start, end=end, dur=dur, out=out))
    _stage(2, "切段")
    print(f"[3/5] 切段：每段基准 {seg:.3f} s，切点对齐关键帧")
    for n, p in enumerate(plan, 1):
        _check_cancel(f"第 {n} 段前")
        if p["out"].exists() and p["out"].stat().st_size > 0:
            print(f"      片{n} 已存在，跳过")
        else:
            cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "warning",
                   "-ss", f"{p['start']:.6f}", "-i", str(full),
                   "-t", f"{p['dur']:.6f}", "-map", "0:v:0"]
            if has_audio:
                cmd += ["-map", "0:a:0"]
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero",
                    "-movflags", "+faststart", str(p["out"])]
            run(cmd, f"切第 {n} 段")
        print(f"      片{n}  {p['start']:9.3f} -> {p['end']:9.3f} s  "
              f"时长 {p['dur']:8.3f} s  {p['out'].stat().st_size:,} B")
        _prog(12 + 66 * n / len(plan), p["end"], total)

    # ---- 4) 自检 ----
    _stage(3, "自检")
    print("[4/5] 逐段自检")
    bad = 0
    for n, p in enumerate(plan, 1):
        _check_cancel(f"自检第 {n} 段")
        st, dur = probe_streams(p["out"])
        r = run([FFPROBE, "-v", "error", "-select_streams", "v",
                 "-read_intervals", "%+#1", "-show_entries", "frame=key_frame",
                 "-of", "csv=p=0", str(p["out"])], "读首帧", quiet=True)
        first_key = (r.stdout or "").strip().startswith("1")
        # 真实重叠要按「上一段实际切到的位置」算：-c copy 只能切在关键帧上，
        # 实际时长会比计划多出最多一个 GOP，所以重叠只会变多、不会变少。
        prev_real_end = 0.0 if n == 1 else plan[n - 2].get("real_end", 0.0)
        ov = max(0.0, prev_real_end - p["start"])
        ov_ok = True if n == 1 else ov >= overlap - 0.05
        ok = (st["video"] is not None) and (st["audio"] is not None or not has_audio) \
            and first_key and dur >= p["dur"] - 1.0 and ov_ok
        p.update(video_ok=st["video"] is not None,
                 audio_ok=st["audio"] is not None, first_key=first_key,
                 real_dur=dur, real_end=p["start"] + dur, overlap=ov, ok=ok)
        if not ok:
            bad += 1
        print(f"      片{n}  视频 {'✓' if p['video_ok'] else '✗'}  "
              f"音频 {'✓' if p['audio_ok'] else ('—' if not has_audio else '✗')}  "
              f"首帧关键帧 {'✓' if first_key else '✗'}  "
              f"时长 {dur:.3f}s（计划 {p['dur']:.3f}）  "
              f"重叠 {ov:.3f}s" + ("" if ok else "   ✗ 不合格"))
        _prog(78 + 18 * n / len(plan), p["end"], total)

    # ---- 5) 清单 ----
    _stage(4, "清单")
    md = outdir / "分片清单.md"
    lines = [f"# {name} 合并与分片清单", "",
             f"- 合并源：`{video.name}`（视频）"
             + (f" + `{audio.name}`（音频）" if audio else "（单文件）"),
             f"- 合并产物：`{full.name}`  {full.stat().st_size} B  时长 {total:.3f} s",
             "- 规格：" + _spec_text(src_streams) + "（全程 -c copy，无重编码）",
             f"- 切分：{pieces} 等分，每段开头多留至少 {overlap:g} s 上一段内容；"
             "切点对齐视频关键帧", "",
             "| 片 | 文件 | 大小(MB) | 起点(s) | 终点(s) | 时长(s) | 与上一段重叠(s) | 时长:mm:ss |",
             "|---|---|---|---|---|---|---|---|"]
    for p in plan:
        mb = p["out"].stat().st_size / 1048576
        d = p.get("real_dur", p["dur"])
        end = p.get("real_end", p["end"])
        lines.append(
            f"| {p['i']} | `{p['out'].name}` | {mb:.1f} | {p['start']:.3f} | "
            f"{end:.3f} | {d:.3f} | {p.get('overlap', 0.0):.3f} | "
            f"{int(d // 60):02d}:{int(d % 60):02d} |")
    lines += ["", f"（每段时长 = 该段内容长度；相邻两段在接缝处有 {overlap:g} 秒以上重复内容，"
                  "播放时可以互相接上。表中时长/终点是**实际切出来的**值：-c copy 只能切在"
                  "关键帧上，所以会比等分值多出最多一个 GOP，重叠只会更多、不会更少。）"]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _prog(100, total, total)
    print(f"[5/5] 清单：{md}")
    print(f"== 完成：{pieces} 段，用时 {time.time() - t_all:.1f} s"
          + ("" if not bad else f"，**{bad} 段自检不合格**") + " ==")
    _emit("done", out=str(outdir), size=sum(p["out"].stat().st_size for p in plan))
    if bad:
        raise SystemExit(f"{bad} 段自检不合格")
    return 0


def _spec_text(streams):
    v, au = streams.get("video"), streams.get("audio")
    parts = []
    if v:
        w, h = v.get("width"), v.get("height")
        fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or "?"
        if fr and "/" in str(fr):
            try:
                n, d = str(fr).split("/")
                fr = f"{float(n) / float(d):g}"
            except (ValueError, ZeroDivisionError):
                pass
        parts.append(f"{w}x{h} / {fr}fps / {(v.get('codec_name') or '?').upper()}")
    if au:
        parts.append(f"{(au.get('codec_name') or '?').upper()} "
                     f"{int(au.get('sample_rate') or 0) / 1000:g}kHz "
                     f"{au.get('channels') or '?'}ch")
    return " + ".join(parts) or "未知"


if __name__ == "__main__":
    sys.exit(main() or 0)
