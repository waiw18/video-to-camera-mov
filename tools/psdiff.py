"""对比两个 MOV 的 P 帧切片头字段（trace_headers 逐字段）。

校验器只 trace 第一帧（IDR），P 帧的切片头差异查不到；这里把 VPS/SPS/PPS 从
hvcC 里取出来，拼上指定帧的样本，做成小码流再逐字段 trace。
"""
import importlib.util
import re
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

_s = importlib.util.spec_from_file_location("cc", HERE / "camera-concat.py")
cc = importlib.util.module_from_spec(_s)
_s.loader.exec_module(cc)
from boxio import find, track_list, children, u32   # noqa: E402

from binpath import FFMPEG, FFPROBE


def param_sets(d):
    """从视频 trak 的 hvcC 里取出 VPS/SPS/PPS（Annex-B 形式）。"""
    tr = track_list(d)[0]
    stbl = find(d, tr[0], tr[0] + tr[2], b"stbl")
    stsd = find(d, stbl[0], stbl[0] + stbl[2], b"stsd")
    inner = stsd[0] + 16                       # stsd 头 8 + version/flags 4 + count 4
    # 样本条目：8 头 + 78 视觉头，之后是子盒
    p, end = inner + 8 + 78, inner + struct.unpack(">I", d[inner:inner + 4])[0]
    hvcc = None
    while p + 8 <= end:
        sz = struct.unpack(">I", d[p:p + 4])[0]
        t = bytes(d[p + 4:p + 8])
        if t == b"hvcC":
            hvcc = (p, sz)
            break
        p += sz
    if not hvcc:
        raise SystemExit("没找到 hvcC")
    c = d[hvcc[0] + 8:hvcc[0] + hvcc[1]]
    n = c[22]
    q = 23
    out = []
    for _ in range(n):
        typ = c[q] & 0x3F
        cnt = struct.unpack(">H", c[q + 1:q + 3])[0]
        q += 3
        for _ in range(cnt):
            ln = struct.unpack(">H", c[q:q + 2])[0]
            out.append((typ, c[q + 2:q + 2 + ln]))
            q += 2 + ln
    return out


def es_from(d, indices):
    ps = param_sets(d)
    tr = track_list(d)[0]
    sz, co, per = cc.plan_track(d, tr)
    blobs = cc.extract(d, sz, co, per)
    out = b""
    for t, n in ps:
        out += b"\x00\x00\x00\x01" + n
    for i in indices:
        b = blobs[i]
        p = 0
        while p + 4 <= len(b):
            ln = struct.unpack(">I", b[p:p + 4])[0]
            out += b"\x00\x00\x00\x01" + b[p + 4:p + 4 + ln]
            p += 4 + ln
    return out


def fields(es, tag):
    f = Path(f".probe/ph-{tag}.265")
    f.write_bytes(es)
    r = subprocess.run([FFMPEG, "-v", "trace", "-i", str(f), "-c", "copy",
                        "-bsf:v", "trace_headers", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    got = {}
    pat = re.compile(r"^\s*(\d+)\s+(\S+?)\s+(\S+)\s*=\s*(.+?)\s*$")
    for raw in (r.stderr or "").splitlines():
        s = raw.split("] ", 1)[-1] if "] " in raw else raw
        m = pat.match(s)
        if m and m.group(2) not in got:
            got[m.group(2)] = m.group(4)
    return got


def main():
    a, b = sys.argv[1], sys.argv[2]
    for tag, path in (("A", a), ("B", b)):
        d = Path(path).read_bytes()
        # 取 IDR 之后的第 1、2 帧（纯 P 帧）
        got = fields(es_from(d, [0, 1, 2]), tag)
        globals()[f"F{tag}"] = got
        print(f"=== {tag}: {Path(path).name}  字段数 {len(got)}")
    fa, fb = globals()["FA"], globals()["FB"]
    keys = sorted(set(fa) | set(fb))
    diff = [k for k in keys if fa.get(k) != fb.get(k)]
    print(f"\n不同字段 {len(diff)} / {len(keys)}")
    for k in diff:
        print(f"  XX {k:<46} A={fa.get(k,'—'):<14} B={fb.get(k,'—')}")


if __name__ == "__main__":
    main()
