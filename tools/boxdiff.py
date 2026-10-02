"""逐 box 比较两个 MP4/MOV，指出哪些 box 字节不同。"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from boxio import walk, children, find

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta", b"stsd"}


def flatten(d):
    """-> {路径: (off, size)}"""
    out = {}

    def rec(s, e, path, depth):
        for off, t, sz, hdr in walk(d, s, e):
            name = t.decode("latin1", "replace")
            p = f"{path}/{name}"
            out[p] = (off, sz)
            if t in CONTAINERS and depth < 12:
                inner = off + hdr + (8 if t == b"stsd" else 0)
                rec(inner, off + sz, p, depth + 1)

    rec(0, len(d), "", 0)
    return out


def main(a_path, b_path):
    A, B = Path(a_path).read_bytes(), Path(b_path).read_bytes()
    print(f"A = {Path(a_path).name}  {len(A):,} B")
    print(f"B = {Path(b_path).name}  {len(B):,} B")
    fa, fb = flatten(A), flatten(B)
    print(f"\nbox 路径数 A={len(fa)} B={len(fb)}")

    only_a = sorted(set(fa) - set(fb))
    only_b = sorted(set(fb) - set(fa))
    if only_a:
        print(f"只在 A 里: {only_a[:12]}")
    if only_b:
        print(f"只在 B 里: {only_b[:12]}")

    print("\n不同的 box:")
    same_cnt = 0
    for p in sorted(set(fa) & set(fb)):
        oa, sa = fa[p]
        ob, sb = fb[p]
        ba, bb = A[oa:oa + sa], B[ob:ob + sb]
        if ba == bb:
            same_cnt += 1
            continue
        # 找第一个不同字节
        n = min(len(ba), len(bb))
        first = next((i for i in range(n) if ba[i] != bb[i]), n)
        print(f"  {p:34} A@{oa:>10,}({sa:>9,}) B@{ob:>10,}({sb:>9,}) "
              f"首差@{first}")
        if sa == sb and first < n:
            print(f"      A: {ba[first:first+12].hex(' ')}")
            print(f"      B: {bb[first:first+12].hex(' ')}")
    print(f"\n字节相同的 box: {same_cnt}")

    # mdat 载荷比较
    ma = next(x for x in walk(A, 0, len(A)) if x[1] == b"mdat")
    mb = next(x for x in walk(B, 0, len(B)) if x[1] == b"mdat")
    pa = A[ma[0]:ma[0] + ma[2]]
    pb = B[mb[0]:mb[0] + mb[2]]
    print(f"\nmdat 载荷: A={len(pa):,} B={len(pb):,} 相同={pa == pb}")
    if pa != pb and len(pa) == len(pb):
        n = len(pa)
        first = next((i for i in range(n) if pa[i] != pb[i]), n)
        diff = sum(1 for i in range(n) if pa[i] != pb[i])
        print(f"  首差字节 @{first:,}（mdat 内偏移 {first - (ma[0] + 8):,}）不同字节数={diff:,}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
