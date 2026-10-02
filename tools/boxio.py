"""MP4/MOV box 遍历的公共实现（避免每个工具各写一份、各踩同一个坑）。

坑在哪
    `walk(d, box_offset, box_end)` 会把**容器 box 自己**也 yield 一次，于是
    "递归进容器找子 box" 的代码如果直接从容器起点开始扫，就会不断拿到容器自身，
    永远进不去子层级（症状：重建后的 trak 里没有 minf/stbl，或套了一层 box）。

正确做法
    进入容器时从 `off + hdr` 开始扫；需要"盒子里有哪些直接子 box"就用 children()。

用法
    from boxio import walk, children, find, find_all, box, u32
"""
import struct

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta", b"meta"}


def walk(d, s, e):
    """遍历 [s, e) 里的**同级** box，yield (off, type, size, hdr)。不递归。"""
    off = s
    while off + 8 <= e:
        sz = struct.unpack(">I", d[off:off + 4])[0]
        ty = bytes(d[off + 4:off + 8])
        hdr = 8
        if sz == 1:
            if off + 16 > e:
                return
            sz = struct.unpack(">Q", d[off + 8:off + 16])[0]
            hdr = 16
        elif sz == 0:
            sz = e - off
        if sz < 8 or off + sz > e:
            return
        yield off, ty, sz, hdr
        off += sz


def children(d, box):
    """某个 box 的直接子 box 列表。box = (off, type, size, hdr)。"""
    return list(walk(d, box[0] + box[3], box[0] + box[2]))


def find_all(d, s, e, typ, out=None, descend=True):
    """在 [s, e) 内（含容器的子层级）找出所有 typ。返回 [(off, type, size, hdr)]。"""
    if out is None:
        out = []
    for off, t, sz, hdr in walk(d, s, e):
        if t == typ:
            out.append((off, t, sz, hdr))
        if descend and t in CONTAINERS:
            find_all(d, off + hdr, off + sz, typ, out)
    return out


def find(d, s, e, typ):
    r = find_all(d, s, e, typ)
    return r[0] if r else None


def top(d):
    return list(walk(d, 0, len(d)))


def box(typ, payload):
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def u32(d, o):
    return struct.unpack(">I", d[o:o + 4])[0]


def u64(d, o):
    return struct.unpack(">Q", d[o:o + 8])[0]


def track_list(d):
    """moov 下的所有 trak（直接子级）。注意 children() 会连 mvhd/udta 一起返回，
    所以凡是"取第 N 条轨"都必须走这个函数，不要直接索引 children()。"""
    moov = find(d, 0, len(d), b"moov")
    if not moov:
        raise SystemExit("找不到 moov")
    return [x for x in children(d, moov) if x[1] == b"trak"]
