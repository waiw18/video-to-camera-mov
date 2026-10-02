"""定位 ffmpeg / ffprobe。

优先级：
  1. 环境变量 CAMMOV_FFMPEG / CAMMOV_FFPROBE
  2. PATH 里的 ffmpeg / ffprobe
  3. 常见安装位置

都找不到时返回裸名字，交给系统解析（真调不到时 ffmpeg 自己会报错）。
"""
import os
import shutil
from pathlib import Path

_CANDIDATES = (
    r"C:\Program Files\ffmpeg\bin\{n}.exe",
    r"C:\ffmpeg\bin\{n}.exe",
    "/usr/bin/{n}",
    "/usr/local/bin/{n}",
)


def find(name):
    v = os.environ.get("CAMMOV_" + name.upper())
    if v and Path(v).exists():
        return v
    w = shutil.which(name)
    if w:
        return w
    for c in _CANDIDATES:
        p = c.format(n=name)
        if Path(p).exists():
            return p
    return name


FFMPEG = find("ffmpeg")
FFPROBE = find("ffprobe")
