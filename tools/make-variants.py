"""一次生成多版候选：同一份码流，参数集对齐程度不同。

做法：编一次码流（1920x1088，底部补 8 行黑边）+ 抽一次 PCM，然后每一版只改
"参数集对齐开关"，逐版检查解码错误，干净的就报到卡上。

为什么编 1088：实测相机 DSC_8955.MOV 的 SPS 是
    pic_height_in_luma_samples = 1088
    conformance_window_flag = 1，conf_win_bottom_offset = 4（裁 8 行 -> 显示 1080）
本工具原先按 1080 直接编，没有这个裁剪，属于真实的编码结构差异。

用法
    python tools/make-variants.py --copy
"""
import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent

spec = importlib.util.spec_from_file_location("mcm", HERE / "make-camera-mov.py")
mcm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcm)
FFMPEG = mcm.FFMPEG

CODED_H = 1088          # 相机编码高度
DISP_H = 1080           # 容器/显示高度
CONF_BOTTOM = 4         # 4 x 2 = 8 行

# (输出文件, 对齐开关, 说明)
VARIANTS = [
    ("DSC_4481.MOV", dict(drop_aspect=True),
     "色彩对齐 + 去 aspect_ratio"),
    ("DSC_4482.MOV", dict(drop_aspect=True, dependent_slices=True),
     "+ PPS dependent_slice_segments=1"),
    ("DSC_4483.MOV", dict(drop_aspect=True, conf_bottom=CONF_BOTTOM),
     "编 1088 + conformance_window（裁 8 行）"),
    ("DSC_4484.MOV", dict(drop_aspect=True, dependent_slices=True,
                          conf_bottom=CONF_BOTTOM),
     "全部：1088+裁剪 + dependent_slices + 去 aspect"),
]


def sh(args, check=True):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if check and r.returncode != 0:
        tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()[-6:]
        raise SystemExit(f"失败: {' '.join(str(x) for x in args[:4])}...\n" +
                         "\n".join(tail))
    return r


def decode_errors(path):
    r = sh([FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"], check=False)
    return [l for l in ((r.stdout or "") + (r.stderr or "")).splitlines() if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--copy", action="store_true")
    ap.add_argument("--src", default=".probe/src.mp4")
    ap.add_argument("--template", default="cam-tests/DSC_8955.MOV")
    args = ap.parse_args()

    es = ROOT / ".probe/var.265"
    pcm = ROOT / ".probe/var.pcm"
    src = ROOT / args.src
    if not src.exists():
        raise SystemExit(f"找不到源视频 {src}")

    xp = ("ref=1:bframes=0:keyint=30:min-keyint=30:aud=1:repeat-headers=1"
          ":ctu=64:min-cu-size=8:scaling-list=default:sao=0:level-idc=5.0"
          ":wpp=0:signhide=0:weightp=0:max-merge=2:open-gop=0"
          ":range=full:colorprim=bt709:transfer=bt709:colormatrix=bt709")
    # 1080 居中放好后再在底部补 8 行 -> 编码高度 1088
    vf = (f"scale=1920:{DISP_H}:force_original_aspect_ratio=decrease,"
          f"pad=1920:{DISP_H}:(ow-iw)/2:(oh-ih)/2:color=black,"
          f"pad=1920:{CODED_H}:0:0:color=black,setsar=1")

    print(f"1) 编码视频（1920x{CODED_H}，底部补 8 行）")
    sh([FFMPEG, "-y", "-v", "error", "-i", str(src), "-map", "0:v:0",
        "-vf", vf, "-r", "60000/1001", "-c:v", "libx265", "-preset", "medium",
        "-profile:v", "main", "-pix_fmt", "yuv420p", "-b:v", "40M",
        "-x265-params", xp, "-f", "hevc", str(es)])
    print(f"   {es.stat().st_size:,} B")

    print("2) 抽音频")
    sh([FFMPEG, "-y", "-v", "error", "-i", str(src), "-map", "0:a:0",
        "-ac", "2", "-ar", "48000", "-c:a", "pcm_s24le", "-f", "s24le", str(pcm)])
    print(f"   {pcm.stat().st_size:,} B")

    print("3) 逐版生成")
    results = []
    for out, flags, desc in VARIANTS:
        path = ROOT / out
        if path.exists():
            path.unlink()
        print(f"\n-- {out}  {desc}")
        mcm.build(str(es), str(path), str(ROOT / args.template),
                  drop_udta=False, audio_pcm=str(pcm), align_flags=flags,
                  display_size=(1920, DISP_H))
        errs = decode_errors(path)
        ok = not errs
        print(f"   解码错误 {len(errs)} 条 -> {'干净' if ok else errs[0][:90]}")
        results.append((out, ok, len(errs), desc))

    print("\n=== 汇总 ===")
    for out, ok, n, desc in results:
        print(f"  {out:16} {'OK ' if ok else 'ERR'} 错误={n:<4} {desc}")

    if args.copy:
        print("\n4) 拷到卡上（只拷干净的）")
        for out, ok, n, desc in results:
            if ok:
                sh([sys.executable, "-B", str(HERE / "copy-to-card.py"), out])
            else:
                print(f"  跳过 {out}（解码不干净）")


if __name__ == "__main__":
    main()
