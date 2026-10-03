"""把已有 MOV 无损截断成前 N 秒（不重编码），用来做对照实验。

背景：DSC_4521（254.7 s）真机能播、快进正常；我用 --duration 60 编出来的
60 秒文件却卡住。但两者差了两个变量（长度 + 编码路径）。

这个工具把 DSC_4521 **原样**截成 60 秒：码流与音频逐字节搬运，只重建容器。
于是"长度"成了唯一变量，一次就能判定。

用法：
    python cut-mov.py 输入.MOV 输出.MOV --seconds 60 [--template 模板.MOV]
"""
import argparse
import importlib.util
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True


def load(name, fn):
    s = importlib.util.spec_from_file_location(name, HERE / fn)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


cc = load("cc", "camera-concat.py")
mcm = load("mcm", "make-camera-mov.py")
from boxio import find, track_list, u32   # noqa: E402

AUDIO_TS = 48000


def samples_of(d, tr):
    sz, co, per = cc.plan_track(d, tr)
    return cc.extract(d, sz, co, per)


def to_annexb(blobs):
    """把 length-prefixed 的样本列表转成 Annex-B（4 字节长度 -> 起始码）。"""
    out = bytearray()
    for b in blobs:
        p = 0
        while p + 4 <= len(b):
            ln = struct.unpack(">I", b[p:p + 4])[0]
            if p + 4 + ln > len(b):
                break
            out += b"\x00\x00\x00\x01" + b[p + 4:p + 4 + ln]
            p += 4 + ln
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--template", default="cam-tests/DSC_8955.MOV")
    a = ap.parse_args()

    d = Path(a.src).read_bytes()
    trs = track_list(d)
    vs = samples_of(d, trs[0])
    aus = samples_of(d, trs[1])

    # 帧率与时基
    vmd = find(d, trs[0][0], trs[0][0] + trs[0][2], b"mdhd")
    vts = u32(d, vmd[0] + 20)
    stts = find(d, trs[0][0], trs[0][0] + trs[0][2], b"stbl")
    st = find(d, stts[0], stts[0] + stts[2], b"stts")
    delta = u32(d, st[0] + 20) or 1001
    fps = vts / delta
    print(f"  源: 视频 {len(vs):,} 帧 @ {vts}/{delta} = {fps:.4f} fps；音频 {len(aus):,} 样本")

    n_v = min(len(vs), int(round(a.seconds * fps)))
    n_a = min(len(aus), int(round(n_v * delta / vts * AUDIO_TS)))
    print(f"  截取: 视频 {n_v:,} 帧 / 音频 {n_a:,} 样本"
          f" = {n_v * delta / vts:.4f} s")

    es = Path(a.out).with_suffix(".cut.265")
    pcm = Path(a.out).with_suffix(".cut.pcm")
    # 参数集：源样本里没有内联参数集，从 hvcC 里取出来放到码流最前面
    hi = d.find(b"hvcC")
    body = d[hi + 4:]
    p, arrays = 23, []
    for _ in range(body[22]):
        at = body[p]
        n = struct.unpack(">H", body[p + 1:p + 3])[0]
        p += 3
        for _ in range(n):
            ln = struct.unpack(">H", body[p:p + 2])[0]
            p += 2
            arrays.append(bytes(body[p:p + ln]))
            p += ln
    es.write_bytes(b"".join(b"\x00\x00\x00\x01" + x for x in arrays) + to_annexb(vs[:n_v]))
    pcm.write_bytes(b"".join(aus[:n_a]))
    print(f"  码流 {es.stat().st_size:,} B（含 {len(arrays)} 个参数集）"
          f" / PCM {pcm.stat().st_size:,} B")

    mcm.build(str(es), a.out, a.template, audio_pcm=str(pcm),
              align_flags={"drop_aspect": True, "dependent_slices": True,
                           "conf_bottom": 4, "tick": delta, "scale": vts},
              display_size=(1920, 1080), fps=(vts, delta),
              level_idc=150 if fps > 40 else 123)
    es.unlink(missing_ok=True)
    pcm.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
