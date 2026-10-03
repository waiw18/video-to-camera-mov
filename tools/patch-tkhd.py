"""诊断用：把已有 MOV 的 tkhd.duration 改成指定值，产出对照文件。

用途：判定"相机快进卡死"是不是 tkhd.duration 造成的。
    python patch-tkhd.py 输入.MOV 输出.MOV [来源模板.MOV]
不给模板则用输入自己的 tkhd 值（相当于原样复制，用于验证脚本本身）。
"""
import shutil
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from boxio import find, track_list, u32   # noqa: E402


def get_tkhd_dur(d, trak):
    tk = find(d, trak[0], trak[0] + trak[2], b"tkhd")
    return u32(d, tk[0] + 28) if d[tk[0] + 8] == 0 else u32(d, tk[0] + 36), tk


def main(src, dst, tmpl=None):
    d = bytearray(Path(src).read_bytes())
    trs = track_list(d)
    ref = Path(tmpl).read_bytes() if tmpl else None
    rtrs = track_list(ref) if ref else None
    for i, tr in enumerate(trs):
        cur, tk = get_tkhd_dur(d, tr)
        if ref:
            want = get_tkhd_dur(ref, rtrs[i])[0]
        else:
            want = cur
        struct.pack_into(">I", d, tk[0] + 28, want)
        nm = "视频" if i == 0 else "音频"
        print(f"    {nm} tkhd.duration: {cur:,} -> {want:,}")
    Path(dst).write_bytes(bytes(d))
    print(f"  -> {dst}  {len(d):,} B")


if __name__ == "__main__":
    a = sys.argv[1:]
    print(f"=== 改 tkhd.duration: {Path(a[0]).name} -> {Path(a[1]).name}"
          + (f"（参照 {Path(a[2]).name}）" if len(a) > 2 else "（原值）"))
    main(*a)
