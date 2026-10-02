"""扫描 libx265 配置，找出 SPS 结构最接近相机的参数组合。

相机 DSC_8955.MOV 实测: VPS=33B SPS=416B PPS=7B
"""
import subprocess
import sys
from pathlib import Path

from binpath import FFMPEG, FFPROBE
SRC = ".probe/src.mp4"
OUT = Path(".probe/sps-sweep")
OUT.mkdir(parents=True, exist_ok=True)


def nals_of(es):
    b = Path(es).read_bytes()
    idx, i = [], b.find(b"\x00\x00\x01")
    while i != -1:
        idx.append(i)
        i = b.find(b"\x00\x00\x01", i + 3)
    out = {}
    for k, pos in enumerate(idx):
        st = pos + 3
        en = idx[k + 1] if k + 1 < len(idx) else len(b)
        nal = b[st:en].rstrip(b"\x00") if k + 1 == len(idx) else b[st:en]
        if not nal:
            continue
        t = (nal[0] >> 1) & 0x3F
        out.setdefault(t, nal)
    return out


CONFIGS = {
    "base":        "-c:v libx265 -preset fast -b:v 40M -profile:v main -pix_fmt yuv420p",
    "ref2":        "-c:v libx265 -preset fast -b:v 40M -profile:v main -pix_fmt yuv420p -x265-params ref=2:bframes=0:keyint=30:min-keyint=30",
    "ref2aud":     "-c:v libx265 -preset fast -b:v 40M -profile:v main -pix_fmt yuv420p -x265-params ref=2:bframes=0:keyint=30:min-keyint=30:aud=1:repeat-headers=1",
    "ref2audvui":  "-c:v libx265 -preset fast -b:v 40M -profile:v main -pix_fmt yuv420p -x265-params ref=2:bframes=0:keyint=30:min-keyint=30:aud=1:repeat-headers=1:vui-timing-info=1",
    "ref2hrd":     "-c:v libx265 -preset fast -b:v 40M -profile:v main -pix_fmt yuv420p -x265-params ref=2:bframes=0:keyint=30:min-keyint=30:aud=1:repeat-headers=1:hrd=1:vui-hrd-info=1:vui-timing-info=1",
    "ref2hrdinfo": "-c:v libx265 -preset fast -b:v 40M -profile:v main -pix_fmt yuv420p -x265-params ref=2:bframes=0:keyint=30:min-keyint=30:aud=1:repeat-headers=1:hrd=1:vui-hrd-info=1:vui-timing-info=1:info=0",
    "level50":     "-c:v libx265 -preset fast -b:v 40M -profile:v main -pix_fmt yuv420p -x265-params ref=2:bframes=0:keyint=30:min-keyint=30:aud=1:repeat-headers=1:level-idc=5.0:hrd=1:vui-hrd-info=1:vui-timing-info=1",
}


def main():
    # 只取 1 秒，扫描快
    for name, args in CONFIGS.items():
        es = OUT / f"{name}.265"
        cmd = [FFMPEG, "-y", "-v", "error", "-i", SRC, "-t", "1",
               "-vf", "scale=1920:1080,setsar=1", "-r", "60000/1001"]
        cmd += args.split() + ["-f", "hevc", str(es)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0:
            print(f"{name:14} 编码失败: {(r.stderr or '').strip().splitlines()[-1:]}")
            continue
        n = nals_of(es)
        v, s, p = len(n.get(32, b"")), len(n.get(33, b"")), len(n.get(34, b""))
        print(f"{name:14} VPS={v:>4} SPS={s:>4} PPS={p:>3}   相机: VPS=33 SPS=416 PPS=7")
    print("\n（只比长度，长度接近说明 SPS 里装了同类信息）")


if __name__ == "__main__":
    main()
