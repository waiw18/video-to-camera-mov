"""判断相机的 416 字节 SPS 是否 = 标准 SPS + 追加的私有数据。

做法：把相机 SPS 与 libx265/NVENC 的 SPS 都展开成比特串，
求最长公共前缀，看分歧点在哪、之后还剩多少字节。
"""
import struct
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from boxio import find, track_list   # noqa: E402


def hvcc_nals(d):
    tr = track_list(d)[0]
    stbl = find(d, tr[0], tr[0] + tr[2], b"stbl")
    stsd = find(d, stbl[0], stbl[0] + stbl[2], b"stsd")
    inner = stsd[0] + 16
    esz = struct.unpack(">I", d[inner:inner + 4])[0]
    e = d[inner:inner + esz]
    p = e.find(b"hvcC")
    ln = struct.unpack(">I", e[p - 4:p])[0]
    h = e[p + 4:p - 4 + ln]
    out = {}
    q = 23
    while q + 3 <= len(h):
        t = h[q] & 0x3F
        n = struct.unpack(">H", h[q + 1:q + 3])[0]
        q += 3
        for _ in range(n):
            L = struct.unpack(">H", h[q:q + 2])[0]
            q += 2
            out.setdefault(t, h[q:q + L])
            q += L
    return out


def unescape(b):
    """去掉 00 00 03 的 emulation prevention。"""
    out, i, zeros = bytearray(), 0, 0
    while i < len(b):
        if zeros >= 2 and b[i] == 3:
            zeros = 0
            i += 1
            continue
        zeros = zeros + 1 if b[i] == 0 else 0
        out.append(b[i])
        i += 1
    return bytes(out)


def bits(b):
    return "".join(f"{x:08b}" for x in b)


def es_nals(path):
    b = Path(path).read_bytes()
    idx, i = [], b.find(b"\x00\x00\x01")
    while i != -1:
        idx.append(i)
        i = b.find(b"\x00\x00\x01", i + 3)
    out = {}
    for k, pos in enumerate(idx):
        st = pos + 3
        en = idx[k + 1] if k + 1 < len(idx) else len(b)
        nal = b[st:en].rstrip(b"\x00") if k + 1 == len(idx) else b[st:en]
        if nal:
            out.setdefault((nal[0] >> 1) & 0x3F, nal)
    return out


def main():
    cam = hvcc_nals(Path(r"cam-tests\DSC_8955.MOV").read_bytes())
    cam_sps = cam[33]
    ref_sps = None
    for cand in (".probe/sps-sweep/ref2.265",):
        p = Path(cand)
        if p.exists():
            ref_sps = es_nals(p).get(33)
            break

    print(f"相机 SPS: {len(cam_sps)} B")
    cam_payload = unescape(cam_sps[2:])
    print(f"  去 emulation 后载荷: {len(cam_payload)} B")
    print(f"  原始: {cam_sps[:24].hex(' ')}")
    print(f"  去转义: {cam_payload[:24].hex(' ')}")

    if ref_sps:
        print(f"\nlibx265 SPS: {len(ref_sps)} B")
        rp = unescape(ref_sps[2:])
        print(f"  去 emulation 后载荷: {len(rp)} B")
        print(f"  原始: {ref_sps[:24].hex(' ')}")

        a, b = bits(cam_payload), bits(rp)
        n = 0
        while n < min(len(a), len(b)) and a[n] == b[n]:
            n += 1
        print(f"\n最长公共前缀: {n} bit = {n / 8:.1f} 字节")
        print(f"  相机在 {n} bit 处的字节下标 = {n // 8}")
        print(f"  相机该处: {cam_payload[n // 8:n // 8 + 12].hex(' ')}")
        print(f"  libx265处: {rp[n // 8:n // 8 + 12].hex(' ')}")
        print(f"\n  相机载荷 {len(cam_payload)} B，公共前缀 {n // 8} B，"
              f"多出 {len(cam_payload) - n // 8} B")

    # 找 rbsp_trailing_bits：末尾的 1 后跟 0
    tail = cam_payload
    k = len(tail) - 1
    while k >= 0 and tail[k] == 0:
        k -= 1
    print(f"\n相机载荷尾部: ...{tail[-16:].hex(' ')}")
    print(f"  最后一个非零字节在 {k}（值 {tail[k]:02x}），"
          f"之后 {len(tail) - 1 - k} 个 0 字节")


if __name__ == "__main__":
    main()
