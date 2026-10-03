"""严格验证 MOV 的可跳转性（seek）——相机快进卡死就出在这里。

做法：对若干时间点，分别
  (a) 直接 -ss 跳过去取一帧（依赖容器索引：stss/stts/stsz/stco）
  (b) 完整解码后在同样时间取一帧（真值）
比较两者的 PTS 与画面哈希。不一致 → 容器索引有问题。
"""
import hashlib
import subprocess
import sys
from pathlib import Path

from binpath import FFMPEG as FF, FFPROBE


def frame_at(path, t, accurate=False):
    """取 t 秒处的一帧，返回 (实际pts, md5)。accurate=True 用 -ss 在 -i 之后（精确但慢）。"""
    cmd = [FF, "-v", "error"]
    if not accurate:
        cmd += ["-ss", f"{t}"]
        cmd += ["-i", path]
    else:
        cmd += ["-i", path, "-ss", f"{t}"]
    cmd += ["-frames:v", "1", "-f", "image2pipe", "-vcodec", "rawvideo",
            "-pix_fmt", "gray", "-"]
    r = subprocess.run(cmd, capture_output=True)
    return len(r.stdout), hashlib.md5(r.stdout).hexdigest()[:10] if r.stdout else "空"


def main(path):
    print(f"--- {Path(path).name}")
    dur = float(subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path], capture_output=True, text=True).stdout.strip())
    print(f"    时长 {dur:.3f} s")
    ok = 0
    for frac in (0.1, 0.25, 0.5, 0.75, 0.9):
        t = round(dur * frac, 3)
        fast = frame_at(path, t)            # 靠容器索引跳
        slow = frame_at(path, t, True)      # 精确解码跳
        same = fast[1] == slow[1]
        ok += same
        print(f"    t={t:7.3f}s  索引跳转 {fast[0]:>8}B/{fast[1]}   "
              f"精确 {slow[0]:>8}B/{slow[1]}   {'✓ 一致' if same else '!! 不一致'}")
    print(f"    => {ok}/5 一致")


if __name__ == "__main__":
    for a in sys.argv[1:]:
        main(a)
