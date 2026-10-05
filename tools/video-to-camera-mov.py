"""端到端：任意电脑视频 -> 相机可播的 1080p MOV。

一条命令完成：解码源视频 -> H.265 编码（按相机规格）-> 抽 PCM 音频 -> 装相机 MOV 容器。

相机 1080p MOV 规格（实测 DSC_8955.MOV）
    H.265 Main / 8-bit / 1920x1080 / 59.94fps / 全 I+P 无 B 帧 / GOP 30
    音频 PCM 24-bit 48 kHz 立体声
    容器 ftyp(qt+niko) | moov | free | mdat@655,360

用法
    python tools/video-to-camera-mov.py 输入视频 --out DSC_4500.MOV --template DSC_8955.MOV
      --template  相机实拍 MOV（提供容器骨架）。1080p 的给 1080p 模板最稳。
      --bitrate   视频码率，默认 40M
      --no-audio  不带音频（纯静音轨）
      --keep      保留中间文件（.probe/ 下的 .265 和 .pcm）
"""
import argparse
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

# 文件名带连字符，不能直接 import，用显式 spec 加载
_spec = importlib.util.spec_from_file_location("make_camera_mov", _HERE / "make-camera-mov.py")
_mcm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mcm)
build = _mcm.build
FFPROBE = _mcm.FFPROBE

_spec_cj = importlib.util.spec_from_file_location("camera_jpeg", _HERE / "camera-jpeg.py")
camjpeg = importlib.util.module_from_spec(_spec_cj)
_spec_cj.loader.exec_module(camjpeg)

# ffmpeg / ffprobe：允许用环境变量覆盖（打包成 exe 后 ffmpeg 会随包一起放）
from binpath import FFMPEG, FFPROBE
# 中间文件目录：环境变量优先（exe 里 _MEIPASS 是只读临时区，要写到别处）
PROBE = Path(os.environ.get("CAMMOV_WORK")
             or (Path(__file__).resolve().parent.parent / ".probe"))
# 界面点"取消转换"时往这里丢一个文件；编码循环每次读进度都看一眼。
CANCEL_FLAG = PROBE / "cancel.flag"


def run(args, label):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        tail = "\n".join(((r.stderr or "") + (r.stdout or "")).splitlines()[-12:])
        raise SystemExit(f"{label} 失败（exit {r.returncode}）:\n{tail}")
    return r


# ---- 给界面（app/main.py）用的机器可读进度行 ----------------------------------
# 协议：以 "##CAMMOV " 开头的一行 JSON；界面按行解析，不显示在日志里。
#   {"k":"stage","step":2,"total":6,"name":"视频编码（x265）"}   当前第几步
#   {"k":"prog","done":101.2,"total":241.6,"pct":41.9,"speed":0.14,"eta":1003}
#   {"k":"gate","ok":43,"bad":0}                                 门禁结果
#   {"k":"done","out":"...","size":123}                          转换完成
# 命令行单独跑时这些行混在日志里也无害（人看得懂）。
def _emit(kind, **kw):
    print("##CAMMOV " + json.dumps({"k": kind, **kw}, ensure_ascii=False),
          flush=True)


def _emit_prog(done, total, speed, elapsed=None):
    pct = round(min(100.0, done / total * 100), 1) if total else None
    eta = int((total - done) / speed) if (total and speed and speed > 0) else None
    _emit("prog", done=round(done, 2), total=round(total, 2) if total else None,
          pct=pct, speed=round(speed, 3) if speed else None, eta=eta,
          elapsed=int(elapsed) if elapsed else None)


def run_prog(args, label, total=None, throttle=0.5):
    """跑 ffmpeg（命令里必须带 -progress pipe:1），把进度转成 ##CAMMOV 行。

    编码占总耗时的 97%，没有它界面就只剩"转圈"——所以这一步必须读进度。
    进度用 ffmpeg 的 speed（相对实时的倍数）算剩余时间，再做指数平滑，
    免得读数每秒乱跳。
    """
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    err_box = []
    th = threading.Thread(target=lambda: err_box.append(p.stderr.read() or ""),
                          daemon=True)
    th.start()
    done, speed, smooth = 0.0, None, None
    t0, t_last, frames = time.time(), 0.0, 0
    for raw in p.stdout:
        k, _, v = raw.strip().partition("=")
        if k == "out_time_us":
            try:
                done = int(v) / 1_000_000
            except ValueError:
                pass
        elif k == "frame":
            try:
                frames = int(v)
            except ValueError:
                pass
        elif k == "speed":
            try:
                speed = float(v.rstrip("x"))
            except ValueError:
                speed = None
        elif k == "progress" and v in ("continue", "end"):
            if v != "end" and CANCEL_FLAG.exists():
                # 界面点了"取消转换"：杀掉 ffmpeg，把半成品留给上层清理
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                th.join(timeout=5)
                CANCEL_FLAG.unlink(missing_ok=True)
                _emit("cancelled", at=round(done, 2))
                raise SystemExit("已取消转换")
            now = time.time()
            if v == "end" or now - t_last >= throttle:
                t_last = now
                if speed and speed > 0:
                    smooth = speed if smooth is None else smooth * 0.7 + speed * 0.3
                _emit_prog(done, total, smooth, now - t0)
    p.wait()
    th.join(timeout=5)
    err = "".join(err_box)
    if p.returncode != 0:
        tail = "\n".join((err or "").splitlines()[-12:])
        raise SystemExit(f"{label} 失败（exit {p.returncode}）:\n{tail}")
    return done, frames


# 流水线的 6 段。界面靠它显示"第 N/6 步"，名字由工具给出（界面不硬编码步骤名）
STAGES = ["探测", "视频编码", "音频", "缩略图", "打包容器", "收尾"]


class Stage:
    """分段计时：找出真正的瓶颈（全 GPU 链路实测反而比 CPU 滤镜慢，
    所以瓶颈不在编码，必须量出来）。"""

    def __init__(self):
        self.t = time.time()
        self.last = self.t
        self.n = 0

    def mark(self, name):
        now = time.time()
        self.n += 1
        print(f"  [用时] {name}: {now - self.last:.1f}s（累计 {now - self.t:.1f}s）")
        _emit("stage", step=self.n, total=len(STAGES), done=name,
              running=STAGES[self.n] if self.n < len(STAGES) else None)
        self.last = now


def count_aus(es_path):
    """粗略数 Annex-B 码流里的访问单元数：每个图像一个 AUD（type 35）。

    比解封装快得多，用来做"帧数 vs 时长"的自检。
    """
    data = Path(es_path).read_bytes()
    n, i = 0, 0
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0:
            break
        p = i + 3
        if p < len(data) and ((data[p] >> 1) & 0x3F) == 35:
            n += 1
        i = p
    return n


def probe(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,width,height,r_frame_rate",
                        "-of", "default=nw=1", str(path)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    dur, has_audio = None, False
    for line in (r.stdout or "").splitlines():
        k, _, v = line.partition("=")
        if k == "duration":
            try:
                dur = float(v)
            except ValueError:
                pass
        elif k == "codec_type" and v == "audio":
            has_audio = True
    # ★ 还要单独取**视频轨**的时长。B 站这类 DASH 合流常见视频 254.70 s、
    #   音频 254.86 s，容器 duration 给的是较大的那个。如果按容器时长去算
    #   帧数、去抽音频，视频就会比音频短一大截，mvhd 取视频时长 → 整个文件
    #   时长不自洽（实测 255 秒的片子差 0.16 s）。
    r2 = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1",
                         str(path)],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace")
    vid = None
    try:
        vid = float((r2.stdout or "").strip())
    except ValueError:
        pass
    if not vid:
        vid = dur
    return dur, vid, has_audio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--out", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--bitrate", default="40M")
    ap.add_argument("--encoder", default="x265", choices=["x265", "nvenc"],
                    help="x265（默认，SPS 与相机高度一致）；nvenc（快但 CTU 32）")
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", default="60000/1001", help="输出帧率；写 source 表示跟随源（源 30fps 时省一半时间与体积）")
    ap.add_argument("--mdat", type=int, default=None,
                    help="默认沿用模板；1080p 相机 MOV 是 655360")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--time", default="now",
                    help="拍摄时间：now（默认，用当前时间）或 "
                         "'YYYY:MM:DD HH:MM:SS'（本地时间）；keep = 沿用模板的时间")
    ap.add_argument("--duration", type=float, default=None,
                    help="只取前 N 秒（默认整段）。相机的时长由 NCDT 帧数 ÷ 帧率 得出，"
                         "帧数会自动改对")
    ap.add_argument("--start", type=float, default=0.0,
                    help="从第 N 秒开始截取，配合 --duration")
    ap.add_argument("--no-thumbs", action="store_true",
                    help="不重建缩略图（默认会用视频首帧重建 NCDT 里的 3 张 JPEG）")
    ap.add_argument("--thumb-at", type=float, default=0.0,
                    help="缩略图取第 N 秒的画面（默认 0 = 首帧，与相机原生行为一致）。"
                         "N 是相对截取起点的偏移")
    ap.add_argument("--thumb-image", default=None,
                    help="直接拿这张图片当缩略图（跳过抽帧；会按 3 种尺寸缩放后"
                         "编成相机规格 JPEG）")
    ap.add_argument("--jobs", type=int, default=1,
                    help="分段并行编码的段数。**默认 1（单进程）**——真机验证过。"
                         ">1 会快约 2 倍，但实测相机播到中途就跳出"
                         "（DSC_4534/DSC_4535：进度 20-35 s 处退出），**暂不可用**；"
                         "0 = 自动（当前等同 1）")
    ap.add_argument("--preset", default="medium",
                    help="x265 预设（medium 默认；fast/faster 快 2-3 倍，"
                         "实测关键 SPS 字段仍与相机一致）")
    a = ap.parse_args()

    # 截取范围（-ss / -t 放在 -i 之后保证帧精确）
    trim = []
    if a.start:
        trim += ["-ss", f"{a.start}"]
    if a.duration:
        trim += ["-t", f"{a.duration}"]
        print(f"  只取 {a.start:g}~{a.start + a.duration:g} 秒"
              if a.start else f"  只取前 {a.duration:g} 秒")

    src = Path(a.input).resolve()
    if not src.exists():
        raise SystemExit(f"找不到输入文件: {src}")
    PROBE.mkdir(exist_ok=True)
    # 临时文件按输出名区分：并发转多个文件时不会互相踩
    tag = Path(a.out).stem
    es = PROBE / f"enc-{tag}.265"
    pcm = PROBE / f"enc-{tag}.pcm"

    st = Stage()
    _emit("stage", step=0, total=len(STAGES), done=None, running=STAGES[0])
    dur, vid_dur, has_audio = probe(src)
    st.mark("探测")
    print(f"输入: {src.name}  容器时长={dur}s  视频轨={vid_dur}s  含音频={has_audio}")

    # 帧率：--fps source 表示跟随源。源是 30fps 时不必复制成 59.94
    #   （B 站 30fps 源若强行转 59.94，帧数翻倍 → 编码时间与体积都翻倍）。
    if a.fps.lower() == "source":
        r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=r_frame_rate",
                            "-of", "default=nw=1:nk=1", str(src)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        a.fps = (r.stdout or "").strip() or "60000/1001"
        print(f"  帧率跟随源: {a.fps}")
    fn, fd = (int(x) for x in a.fps.split("/"))
    # ★ 必须先把帧率归一到相机的时基约定，再拿它算帧数。
    #   否则 30/1 -> 1800 帧，而实际按 29.97 播就是 60.06 s，比音频长 0.06 s（踩过）。
    nfn, nfd = _mcm.normalize_fps(fn, fd)
    if (nfn, nfd) != (fn, fd):
        print(f"  帧率归一到相机时基: {fn}/{fd} -> {nfn}/{nfd}")
    fn, fd = nfn, nfd
    a.fps = f"{fn}/{fd}"

    # ---- 1) 视频：按相机规格编码 ----
    # 相机特征：Main 档、无 B 帧、GOP 30、AUD 每个样本一个、参数集只在容器里。
    # 关键编码工具必须与相机一致（实测 DSC_8955.MOV 的 SPS 逐字段比对）：
    #   CTU 64 (log2_diff_max_min_luma_coding_block_size=3)
    #   最小编码块 8x8 (log2_min_luma_coding_block_size_minus3=0)
    #   变换块不做递归 (max_transform_hierarchy_depth_inter/intra=0)
    #   scaling list 打开、AMP 关、SAO 关、强帧内平滑开
    # NVENC 给的是 CTU 32 + TU 深度 3，与相机差得远；x265 能精确对上。
    try:
        sw, sh = (int(x) for x in a.size.lower().split("x"))
    except ValueError:
        raise SystemExit(f"--size 要用 WxH 形式，收到 {a.size!r}")

    # ★ 最关键的一条：相机编的高度是"显示高度向上取到 16 的倍数"，再用
    #   conformance_window 裁掉多出来的行。
    #   实测 DSC_8955.MOV：pic_height_in_luma_samples=1088、
    #   conformance_window_flag=1、conf_win_bottom_offset=4（裁 8 行）→ 显示 1080。
    #   本工具早先按 1080 直接编、没有裁剪窗口，相机就不认（能识别但读取无限加载）。
    #   1080 -> 1088 正好就是相机的取值。
    coded_h = (sh + 15) // 16 * 16
    conf_bottom = (coded_h - sh) // 2 if coded_h != sh else None
    if conf_bottom:
        print(f"  编码高度 {coded_h}（显示 {sh}），conformance_window 裁 "
              f"{conf_bottom * 2} 行")

    # pad 的语法是 pad=W:H:x:y，不能写 pad=1920x1080（踩过）
    vf = (f"scale={sw}:{sh}:force_original_aspect_ratio=decrease,"
          f"pad={sw}:{sh}:(ow-iw)/2:(oh-ih)/2:color=black")
    if coded_h != sh:
        vf += f",pad={sw}:{coded_h}:0:0:color=black"
    vf += ",setsar=1"
    if a.encoder == "x265":
        # 必须与相机的 PPS/切片头逐字段对齐（实测 DSC_8955.MOV）：
        #   wpp=0       -> entropy_coding_sync_enabled_flag=0，且切片头不再带
        #                  num_entry_point_offsets。x265 默认开 WPP，会往每个
        #                  切片头塞十几个 entry point 偏移，相机的硬件解码器不认，
        #                  表现就是"能识别但读取无限加载"。
        #   signhide=0  -> sign_data_hiding_enabled_flag 0（相机是 0）
        #   weightp=0   -> weighted_pred_flag 0（相机是 0）
        #   open-gop=0  -> 每个关键帧都是真 IDR(19)，而不是 CRA(21)。
        #                   x265 默认开放 GOP，关键帧用 CRA；相机全程用 IDR_W_RADL，
        #                   相机的硬件解码器不认 CRA，表现就是卡在加载。
        #   max-merge=2 -> five_minus_max_num_merge_cand 3（相机是 3）
        #   ref=1       -> 只需 1 个参考帧，与相机的 DPB=2 自洽
        #                   （配合 tools/hevc-ps.py 把 DPB 声明改成相机值）
        # 注意：不要再加 tu-inter-depth=0 / tu-intra-depth=0 —— x265 要求 >=1，
        # 加了会直接编码失败；而默认配置本来就在 SPS 里写成 0（与相机一致）。
        # ★★ PTL（profile/tier/level）必须和相机一致，而且**流内 VPS/SPS 与 hvcC 必须相同**
        #    —— 实测相机两个原片：
        #        1080p60  DSC_8955: general_level_idc=150 (5.0) / general_tier_flag=1 (High)
        #        1080p30  DSC_8960: general_level_idc=123 (4.1) / general_tier_flag=1 (High)
        #    踩过的坑：x265 那边写死 level-idc=5.0 且没开 high-tier（默认 Main tier），
        #    于是流内是 150/Main、hvcC 写的是 150/High，两者矛盾 → 相机不认。
        #    29.97 还必须降到 4.1（写成 5.0 也不行）。
        lv_f, lv_idc = (5.0, 150) if fn / fd > 40 else (4.1, 123)
        xp = ("ref=1:bframes=0:keyint=30:min-keyint=30:aud=1:repeat-headers=1"
              f":ctu=64:min-cu-size=8:scaling-list=default:sao=0"
              f":level-idc={lv_f}:high-tier=1"
              ":wpp=0:signhide=0:weightp=0:max-merge=2:open-gop=0"
              # 色彩必须用 x265 原生参数名：ffmpeg 的 -color_primaries/-color_trc
              # 经 libx265 包装后写成的是"未指定"(2)，而相机是 bt709(1)。
              #   range=full -> video_full_range_flag=1
              #   colorprim/transfer -> 1/1（与相机一致）
              ":range=full:colorprim=bt709:transfer=bt709:colormatrix=bt709")
        enc = ["-c:v", "libx265", "-preset", a.preset, "-profile:v", "main",
               "-pix_fmt", "yuv420p", "-b:v", a.bitrate, "-x265-params", xp]
    else:
        # GPU 路径。相机实测是 High tier / level 5.0 / 关键帧全 IDR_W_RADL，
        # 且**不开 WPP**；NVENC 的默认正好不开 WPP、也不写 entry_points ✓。
        # 注意 nvenc 的 -level 用的是 NVENC 的 HEVC 等级值：
        #   123 = 4.1（1080p30）、150 = 5.0（与相机相同）、186 = 6.2。
        # 注意 NVENC 改不了的：CTU 尺寸（它固定 32，相机 64）、
        #   TU 递归深度（它 3，相机 0）、SAO/AMP 开关、scaling list。
        enc = ["-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq",
               "-rc", "vbr", "-b:v", a.bitrate,
               "-profile:v", "main", "-tier", "high", "-level", "150",
               "-pix_fmt", "yuv420p",
               # NVENC 分支必须显式给色彩，否则 VUI 是"未指定/有限范围"，
               # 与相机（bt709 + 全范围）不一致。x265 那边是靠 x265-params 里的
               # range/colorprim/transfer/colormatrix。
               "-color_range", "pc", "-color_primaries", "bt709",
               "-color_trc", "bt709", "-colorspace", "bt709",
               "-bf", "0", "-g", "30", "-no-scenecut", "1", "-forced-idr", "1",
               "-aud", "1"]
    cmd = [FFMPEG, "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
           "-i", str(src)] + trim + [
           "-map", "0:v:0", "-vf", vf, "-r", a.fps] + enc
    # ★ 帧数必须按时长算准，否则视频会比音频短（踩过：255 秒的片子少了 9 帧，
    #   视频 254.70 s 而音频 254.86 s，mvhd 取视频时长 → 整个文件时长不自洽）。
    want = None
    if dur:
        try:
            # 注意别用 fn/fd 这个名字：它们是函数级的整数帧率参数，
            # 这里若重名会把后面 build(fps=(fn, fd)) 覆盖成浮点，导致 struct 报错（踩过）
            rf_num, rf_den = (float(x) for x in a.fps.split("/"))
            want = int(round((min(dur, a.duration) if a.duration else dur)
                             * rf_num / rf_den))
            cmd += ["-frames:v", str(want)]
        except (ValueError, ZeroDivisionError):
            pass

    # ★ 分段并行：wpp=0 是为相机要求的（entropy_coding_sync_enabled_flag=0），
    #   但它把 CTU 行内并行度掐掉了，单进程 1080p 只有约 12 fps（实测）。
    #   切成 N 段各起一个 x265 进程并行，实测 4 段 = 2.14x，拼起来 ffmpeg 解码 0 错误。
    #
    #   ★★ 但真机不行：2026-10-02 实测 DSC_4534 / DSC_4535（都是 --jobs 4）相机
    #      播到 20-35 s 就跳出；同样参数下 --jobs 1 的 DSC_4537 完全正常。
    #      分段并行时每个 x265 进程会重复写参数集（repeat-headers），拼接后
    #      的参数集/SEI 落位与单进程不同 —— 相机接受不了。
    #      **在查明原因前，默认 --jobs 1；>1 属于实验特性，别拷卡。**
    jobs = a.jobs
    if jobs <= 0:
        jobs = 1
    if jobs > 1:
        print(f"  ⚠ --jobs {jobs} 是实验特性：实测相机会播到中途跳出，"
              f"除非你确认相机能播，否则请用 --jobs 1")
    if want and jobs > 1 and want >= jobs * 8:
        per = want // jobs
        pools = max(2, (os.cpu_count() or 4) // jobs)
        xp_seg = xp + f":pools={pools}" if a.encoder == "x265" else None
        enc_seg = list(enc)
        if xp_seg:
            enc_seg[enc_seg.index("-x265-params") + 1] = xp_seg
        segs, procs = [], []
        print(f"  分段并行：{jobs} 段，每段 {per} 帧，每段 {pools} 线程")
        for i in range(jobs):
            n_i = want - per * i if i == jobs - 1 else per
            ss_i = a.start + (i * per) * fd / fn
            out_i = PROBE / f"seg-{tag}-{i:02d}.265"
            segs.append(out_i)
            c_i = ([FFMPEG, "-y", "-v", "error", "-i", str(src), "-ss", f"{ss_i:.6f}"]
                   + ["-map", "0:v:0", "-vf", vf, "-r", a.fps] + enc_seg
                   + ["-frames:v", str(n_i), "-f", "hevc", str(out_i)])
            procs.append((subprocess.Popen(c_i, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.PIPE), i))
        bad = []
        for p, i in procs:
            _, err = p.communicate()
            if p.returncode != 0:
                bad.append((i, (err or b"").decode("utf-8", "replace")[-400:]))
        if bad:
            for i, e in bad:
                print(f"  段 {i} 失败: {e}")
            raise SystemExit("分段编码失败")
        with es.open("wb") as w:
            for f in segs:
                w.write(f.read_bytes())
        for f in segs:
            f.unlink(missing_ok=True)
        print(f"  已拼接 {jobs} 段 -> {es.stat().st_size:,} B")
    else:
        cmd += ["-f", "hevc", str(es)]
        # 编码占整个转换 97% 的耗时，界面的"进度 + 预计剩余"全靠这一路进度
        tot = min(dur, a.duration) if a.duration else dur
        run_prog(cmd, "视频编码", total=tot)
    st.mark(f"视频编码（{a.encoder}）")
    print(f"  已编码 HEVC（{a.encoder}）: {es.stat().st_size:,} B")

    # ★ 帧数自检：视频帧数必须 ≈ 时长 x 帧率。
    #   之前踩过：视频被截短（9824 帧 vs 应有的 15276 帧），结果视频 163.9 s
    #   而音频 254.9 s，整个文件时长不自洽。宁可这里就报错。
    num, den = (int(x) for x in a.fps.split("/"))
    ref = vid_dur
    if a.duration:
        ref = min(ref or a.duration, a.duration)
    want = int(ref * num / den) if ref else 0
    got = count_aus(es)
    print(f"  视频帧数 {got:,}（按视频轨 {ref:.3f}s x {num/den:.3f}fps 应为 {want:,}）"
          if want else f"  视频帧数 {got:,}")
    if want and abs(got - want) > max(2, want * 0.02):
        raise SystemExit(
            f"帧数不自洽：编出 {got:,} 帧，应为 {want:,} 帧。"
            f"这样视频与音频时长会对不上，先别打包。")

    # 实际时长（截取后要用它生成等长静音、并决定帧数）
    if a.duration:
        dur = min(dur, a.duration) if dur else a.duration

    # ★ 按**实际编出的帧数**算视频轨的确切秒数，音频裁到同一长度。
    #   否则源里两轨不等长时（B 站分片视频 254.70 s / 音频 254.86 s），
    #   mvhd 取视频时长、音频 mdhd 却更长，整个文件时长不自洽。
    aud_t = got * den / num if got else (vid_dur or dur)

    # ---- 2) 音频：PCM 24-bit 48k 立体声 ----
    if a.no_audio or not has_audio:
        if not has_audio:
            print("  源无音频，生成等长静音")
        else:
            print("  按要求不带音频，生成等长静音")
        run([FFMPEG, "-y", "-v", "error",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-t", f"{aud_t if aud_t else 1}", "-c:a", "pcm_s24le",
             "-f", "s24le", str(pcm)], "静音生成")
    else:
        # ★ 把音频裁到视频轨的长度：源里两轨常常不等长（B 站分片差 0.16 s），
        #   不裁的话音频比视频长，mvhd 取视频时长 → 时长不自洽。
        acmd = [FFMPEG, "-y", "-v", "error", "-i", str(src)] + trim
        if aud_t:
            acmd += ["-t", f"{aud_t}"]
        acmd += ["-map", "0:a:0", "-ac", "2", "-ar", "48000",
                 "-c:a", "pcm_s24le", "-f", "s24le", str(pcm)]
        run(acmd, "音频抽取")
    print(f"  已准备 PCM: {pcm.stat().st_size:,} B（{pcm.stat().st_size // 6:,} 帧）")
    st.mark("音频")

    # ---- 2.5) 用视频首帧重建缩略图 ----
    # NCDT 里嵌着 3 张 JPEG（实测相机尺寸）：NCTH 160x120、NCM1 640x360、NCVW 全分辨率。
    # 模板那 3 张是**模板那条片子**的画面，不换的话相机会拿别人的画面当缩略图。
    #
    # ★ 必须写成"相机风格"的 JPEG，否则相机会拒绝**整个文件**（报"无法显示"）。
    #   实测差异：相机 4:2:2（Y=2:1）+ 2 张量化表 + 只有 DQT/DHT/SOF0/SOS；
    #   ffmpeg 默认 4:2:0 + 1 张 + JFIF APP0 + COM。
    #   而且**不能**用 ffmpeg 编完再改 SOF 采样率：ffmpeg 给 yuvj422p 时写的其实还是
    #   `2:2/1:2/1:2`，改了 SOF 熵数据就废了（实测每张 2 个解码错误）。
    #   所以走 Pillow(libjpeg)：subsampling=1 正好是 `2:1/1:1/1:1`，
    #   量化表直接借相机的，再把标记重排成相机的顺序。
    thumbs = {}
    if not a.no_thumbs:
        from PIL import Image
        tpl_jpegs = camjpeg.ncdt_jpegs(Path(a.template).read_bytes())
        # 取帧位置：默认首帧（与相机原生行为一致）。--thumb-at 给秒数（相对截取起点），
        # --thumb-image 直接拿现成图片，跳过抽帧。越界会夹住，免得抽不到帧直接失败。
        t_at = max(0.0, a.thumb_at or 0.0)
        if a.duration:
            t_at = min(t_at, max(0.0, a.duration - 0.05))
        end_ref = min([x for x in (dur, vid_dur) if x] or [0]) or None
        if end_ref:
            t_at = min(t_at, max(0.0, end_ref - a.start - 0.05))
        t_ss = a.start + t_at
        src_img = None
        if a.thumb_image:
            with Image.open(a.thumb_image) as im:
                src_img = im.convert("RGB")
            print(f"  缩略图用指定图片: {a.thumb_image}")
        elif t_ss:
            print(f"  缩略图取 {t_ss:g}s 处的画面（默认取首帧）")
        for name, (tw, th) in (("NCTH", (160, 120)),
                               ("NCM1", (640, 360)),
                               ("NCVW", (sw, sh))):
            if src_img is not None:
                thumbs[name] = camjpeg.pillow_camera_jpeg(
                    src_img.resize((tw, th), Image.LANCZOS), tpl_jpegs[name])
            else:
                png = PROBE / f"th-{tag}-{name}.png"
                run([FFMPEG, "-y", "-v", "error", "-i", str(src)]
                    + (["-ss", f"{t_ss:g}"] if t_ss else [])
                    + ["-map", "0:v:0",
                       "-vf", vf + f",scale={tw}:{th}",
                       "-frames:v", "1", "-f", "image2", str(png)],
                    f"缩略图帧 {name}")
                with Image.open(png) as im:
                    thumbs[name] = camjpeg.pillow_camera_jpeg(im, tpl_jpegs[name])
            marks, sof, ndq = camjpeg.jpeg_info(thumbs[name])
            print(f"  {name} {tw}x{th}: {len(thumbs[name]):,} B JPEG  "
                  f"采样={sof[2][0][1]} 标记={marks[:5]}")

    st.mark("缩略图")

    # ---- 3) 装相机 MOV 容器 ----
    # 参数集按相机实测值对齐（详见 tools/hevc-ps.py 与 码流字段对齐-实测.md）：
    #   drop_aspect      -> 删掉 SPS VUI 的 aspect_ratio_info（相机是 0）
    #   conf_bottom      -> 加 conformance_window（相机编 1088 裁到 1080）★关键
    #   dependent_slices -> PPS 的 dependent_slice_segments_enabled_flag=1（相机是 1）
    # 不传 lists_mod=True：单独翻那一位会让切片头解析错位（实测 574 个解码错误）。
    flags = {"drop_aspect": True, "dependent_slices": True}
    if conf_bottom:
        flags["conf_bottom"] = conf_bottom
    # VPS 里的时序信息与容器时基都要跟着输出帧率走
    flags["tick"], flags["scale"] = fd, fn
    # 时间：默认用当前时间（否则相机里会显示模板那条片子的拍摄时间）。
    # NCDT 里存本地时间字符串，容器头里存同刻 UTC，两者必须一起改。
    tl = a.time
    if tl and tl.lower() == "now":
        tl = datetime.datetime.now().strftime("%Y:%m:%d %H:%M:%S")
        print(f"  拍摄时间设为现在: {tl}")
    elif tl and tl.lower() == "keep":
        tl = None
    build(str(es), a.out, a.template, mdat=a.mdat, drop_udta=False,
          audio_pcm=str(pcm), align_flags=flags, display_size=(sw, sh),
          time_local=tl, thumbs=thumbs or None, fps=(fn, fd), level_idc=lv_idc)

    st.mark("打包容器")

    if not a.keep:
        for f in list(PROBE.glob(f"th-{tag}-*.png")) + [es, pcm]:
            f.unlink(missing_ok=True)
        print("  中间文件已清理")

    st.mark("收尾")
    _emit("done", out=str(Path(a.out).resolve()),
          size=Path(a.out).stat().st_size,
          seconds=round(time.time() - st.t, 1))


if __name__ == "__main__":
    main()
