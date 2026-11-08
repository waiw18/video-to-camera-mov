"""阴性对照生成器：把一份合格 MOV 改坏，用来证明 tools/nikon-box-audit.py 真会拒绝。

**它不是产品工具**，只在做「附加审查可信度」验证时用：一把永远通过的尺子等于没有尺子，
所以每加一条检查，都要有对应的 mutant 能把它打红（见 docs/尼康ISO-BMFF子集-参考.md 的阴性对照表）。

用法
    python tools/box-mutant.py <src> <dst> trunc <n>
    python tools/box-mutant.py <src> <dst> shift <delta>        # 视频轨每个 chunk 偏移 +delta
    python tools/box-mutant.py <src> <dst> stsz  <idx> <val>    # 第 idx 个样本长度改成 val

原理：moov（含 stbl 各表）都在 mdat 之前，原地改字节不会移动任何偏移，
       所以改完仍是一个"结构完整但内容错误"的文件。
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("nba", HERE / "nikon-box-audit.py")
nba = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nba)


def video_offbox(d):
    """返回视频轨的 chunk 偏移表盒 (co64 或 stco)，以及该轨的 stbl 子盒字典。"""
    top = list(nba.iter_boxes(d, 0, len(d)))
    moov = [b for b in top if b[0] == b"moov"][0]
    for tk in nba.iter_boxes(d, moov[2], moov[2] + moov[3]):
        if tk[0] != b"trak":
            continue
        mdia = nba.walk(d, tk, b"mdia")
        hdlr = nba.one(d, mdia[2], mdia[2] + mdia[3], b"hdlr")
        if bytes(d[hdlr[2] + 8:hdlr[2] + 12]) != b"vide":
            continue
        stbl = nba.walk(d, tk, b"mdia", b"minf", b"stbl")
        ch = nba.children(d, stbl)
        return (ch[b"co64"][0] if b"co64" in ch else ch[b"stco"][0]), ch
    raise SystemExit("没有视频轨")


def shift_all(d, delta):
    box, _ch = video_offbox(d)
    width = 8 if box[0] == b"co64" else 4
    b0 = box[2]
    count = int.from_bytes(d[b0 + 4:b0 + 8], "big")
    for i in range(count):
        at = b0 + 8 + i * width
        v = int.from_bytes(d[at:at + width], "big")
        d[at:at + width] = (v + delta).to_bytes(width, "big")
    return count


def set_stsz(d, idx, val):
    _box, ch = video_offbox(d)
    stsz = ch[b"stsz"][0]
    constant = int.from_bytes(d[stsz[2] + 4:stsz[2] + 8], "big")
    if constant:
        raise SystemExit("该片 stsz 是等长表，改不了单条")
    at = stsz[2] + 12 + idx * 4
    d[at:at + 4] = int(val).to_bytes(4, "big")


def main():
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    src, dst, op = sys.argv[1], sys.argv[2], sys.argv[3]
    d = bytearray(Path(src).read_bytes())
    if op == "trunc":
        d = d[:len(d) - int(sys.argv[4])]
    elif op == "shift":
        n = shift_all(d, int(sys.argv[4]))
        print(f"改了 {n:,} 个 chunk 偏移")
    elif op == "stsz":
        set_stsz(d, int(sys.argv[4]), int(sys.argv[5]))
    else:
        raise SystemExit(f"未知操作 {op}")
    Path(dst).write_bytes(bytes(d))
    print(f"{dst} 已写出（{len(d):,} 字节）")


if __name__ == "__main__":
    main()
