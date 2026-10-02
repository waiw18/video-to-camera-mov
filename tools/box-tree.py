"""打印 MOV 的完整盒树（含层级与偏移），用于定位结构差异。"""
import struct
import sys
from pathlib import Path

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts",
              b"dinf", b"mvex", b"moof", b"traf", b"mfra", b"skip", b"strk",
              b"ipro", b"sinf", b"schi", b"wave", b"stsd"}


def tree(d, start, end, depth=0, out=None):
    out = [] if out is None else out
    p = start
    while p + 8 <= end:
        sz = struct.unpack(">I", d[p:p + 4])[0]
        t = bytes(d[p + 4:p + 8])
        hdr = 8
        if sz == 1:
            sz = struct.unpack(">Q", d[p + 8:p + 16])[0]
            hdr = 16
        elif sz == 0:
            sz = end - p
        if sz < hdr or p + sz > end:
            out.append(f"{'  ' * depth}<坏盒 @{p:,} size={sz} type={t}>")
            break
        out.append(f"{'  ' * depth}{t.decode('latin1')} @{p:,} size={sz:,}")
        if t in CONTAINERS and t != b"stsd":
            tree(d, p + hdr, p + sz, depth + 1, out)
        elif t == b"stsd":
            # stsd: version/flags(4) + count(4) + 条目
            n = struct.unpack(">I", d[p + hdr + 4:p + hdr + 8])[0]
            q = p + hdr + 8
            for _ in range(n):
                esz = struct.unpack(">I", d[q:q + 4])[0]
                et = bytes(d[q + 4:q + 8])
                out.append(f"{'  ' * (depth + 1)}{et.decode('latin1')} @{q:,} size={esz:,}")
                tree(d, q + 8 + 78, q + esz, depth + 2, out)
                q += esz
        p += sz
    return out


def main(paths):
    for f in paths:
        d = Path(f).read_bytes()
        print(f"===== {Path(f).name}  ({len(d):,} B)")
        for line in tree(d, 0, len(d)):
            print("  " + line)


if __name__ == "__main__":
    main(sys.argv[1:])
