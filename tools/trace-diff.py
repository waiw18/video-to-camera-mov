"""比对两份 trace_headers 输出里的 SPS / VPS / PPS 语法元素。"""
import re
import sys
from pathlib import Path

LINE = re.compile(r"^\s*(\d+)\s+(\S+?)\s+(\S+)\s*=\s*(.+?)\s*$")


def sections(path):
    """-> {('VPS'|'SPS'|'PPS'|'SH'): [(字段, 值, 位长)]}"""
    out, cur, name = {}, [], None
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if "Video Parameter Set" in raw:
            name = "VPS"
            cur = []
            out.setdefault(name, cur)
            continue
        if "Sequence Parameter Set" in raw:
            name = "SPS"
            cur = []
            out.setdefault(name, cur)
            continue
        if "Picture Parameter Set" in raw:
            name = "PPS"
            cur = []
            out.setdefault(name, cur)
            continue
        if "Slice Header" in raw or "slice_header" in raw:
            name = "SH"
            cur = out.setdefault("SH", [])
            continue
        if name is None:
            continue
        m = LINE.match(raw.split("] ", 1)[-1] if "] " in raw else raw)
        if m:
            nbits, field, bits, val = m.groups()
            cur.append((field, val, nbits))
    return out


def dedup(rows):
    """切片头里字段会重复出现；只保留各字段第一次的值。"""
    seen = {}
    for f, v, n in rows:
        seen.setdefault(f, (v, n))
    return seen


def main(cam_path, mine_path, which="SPS"):
    a = sections(cam_path).get(which, [])
    b = sections(mine_path).get(which, [])
    sa, sb = dedup(a), dedup(b)
    keys = list(dict.fromkeys([k for k, _, _ in a] + [k for k, _, _ in b]))
    print(f"=== {which}：相机 {len(a)} 项 / 本工具 {len(b)} 项 ===")
    diffs = 0
    for k in keys:
        va = sa.get(k, ("—", ""))[0]
        vb = sb.get(k, ("—", ""))[0]
        if va != vb:
            print(f"  ✗ {k:<48} 相机={va:<14} 本工具={vb}")
            diffs += 1
    print(f"  不同字段数: {diffs} / {len(keys)}")


if __name__ == "__main__":
    main(".probe/tc.txt", ".probe/tm.txt", "SPS")
    print()
    main(".probe/tc.txt", ".probe/tm.txt", "VPS")
    print()
    main(".probe/tc.txt", ".probe/tm.txt", "PPS")
