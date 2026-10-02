"""把普通 JPEG 改写成"相机风格"的 JPEG。

起因：实测相机 MOV 里 NCDT/NCTH·NCM1·NCVW 三张缩略图与 ffmpeg 出的 JPEG 差别明显：

| 项 | 相机 | ffmpeg 默认 |
|---|---|---|
| Y 采样 | **2:1（4:2:2）** | 2:2（4:2:0）|
| 量化表 | **2 张**（亮/色各一张，DQT 长度 130）| 1 张（长度 65）|
| 标记序列 | `DQT DHT SOF0 SOS` | `APP0(JFIF) COM DQT DHT SOF0 SOS` |

相机把文件判成"无法显示"，所以这里按相机的样子重写：
色度改成 4:2:2、量化表换成相机那张（两张）、去掉 APP0 与 COM 注释。
"""
import struct
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def segments(j):
    """拆出 JPEG 的段：yield (marker, 段起始, 段总长含标记, 载荷)。"""
    if j[:2] != b"\xff\xd8":
        raise ValueError("不是 JPEG")
    i = 2
    while i + 1 < len(j):
        if j[i] != 0xFF:
            i += 1
            continue
        m = j[i + 1]
        if m == 0xD9:
            yield (m, i, 2, b"")
            return
        if m == 0xDA:                       # SOS：后面是熵编码数据，直接到结尾
            ln = struct.unpack(">H", j[i + 2:i + 4])[0]
            yield (m, i, len(j) - i, j[i + 4:i + 2 + ln])
            return
        if 0xD0 <= m <= 0xD7 or m == 0x01:
            yield (m, i, 2, b"")
            i += 2
            continue
        ln = struct.unpack(">H", j[i + 2:i + 4])[0]
        yield (m, i, 2 + ln, j[i + 4:i + 2 + ln])
        i += 2 + ln


def pillow_camera_jpeg(pil_img, cam_jpeg):
    """用"相机的量化表 + 4:2:2"编码，并把标记顺序重排成相机的样子。

    ★ 为什么不用 ffmpeg 改 SOF：ffmpeg 的 mjpeg 即使给 yuvj422p，写出来的采样率也是
      `2:2 / 1:2 / 1:2`，不是相机的 `2:1 / 1:1 / 1:1`。只把 SOF 的采样率改成相机的值，
      熵编码数据就对不上了 —— JPEG 直接损坏（实测每张 2 个解码错误），相机会拒绝**整个文件**。
      Pillow（libjpeg）的 `subsampling=1` 输出的正是 `2:1 / 1:1 / 1:1`，与相机一致。

    Pillow 还会带上 APP0(JFIF)/COM 注释，并且把 DQT/DHT 拆成多段、DHT 排在 SOF0 之后；
    相机是 `DQT DHT SOF0 SOS`，DQT 一段装两张表。这里统一重排。
    """
    import io
    from PIL import Image

    cam_qt = Image.open(io.BytesIO(cam_jpeg)).quantization
    buf = io.BytesIO()
    pil_img.convert("RGB").save(buf, "JPEG", qtables=cam_qt, subsampling=1,
                                optimize=False)
    raw = buf.getvalue()

    dqts, dhts, sof, sos = [], [], None, None
    for m, off, total, _pl in segments(raw):
        if m == 0xDB:
            dqts.append(raw[off:off + total])
        elif m == 0xC4:
            dhts.append(raw[off:off + total])
        elif m in (0xC0, 0xC1, 0xC2):
            sof = raw[off:off + total]
        elif m == 0xDA:
            sos = raw[off:off + total]
    if not (sof and sos and dhts):
        raise ValueError("Pillow 输出的 JPEG 结构异常")

    cam_dqt = find_dqt(cam_jpeg)
    # DHT 合并成一段（相机就是一段装 4 张表）
    payload = b"".join(d[4:] for d in dhts)
    dht = b"\xff\xc4" + struct.pack(">H", len(payload) + 2) + payload

    return b"\xff\xd8" + (cam_dqt or b"".join(dqts)) + dht + sof + sos


def find_dqt(j):
    """取出第一段 DQT（含标记与长度）的原始字节。"""
    if not j:
        return None
    for m, off, total, _pl in segments(j):
        if m == 0xDB:
            return j[off:off + total]
    return None


def ncdt_jpegs(mov):
    """从 MOV 的 NCDT 里取出各张缩略图 JPEG：{'NCTH': bytes, ...}

    注意 mov.find(b"NCDT") 给的是**类型字段**的位置（盒起点 + 4），
    所以子盒从 nd + 4 开始，盒尾是 (nd - 4) + size。
    """
    out = {}
    nd = mov.find(b"NCDT")
    if nd < 4:
        return out
    size = struct.unpack(">I", mov[nd - 4:nd])[0]
    p, end = nd + 4, nd - 4 + size
    while p + 8 <= end:
        sz = struct.unpack(">I", mov[p:p + 4])[0]
        t = bytes(mov[p + 4:p + 8]).decode("latin1")
        if sz < 8 or p + sz > end:
            break
        body = bytes(mov[p + 8:p + sz])
        if body[:2] == b"\xff\xd8":
            out[t] = body
        p += sz
    return out


def cam_style_jpeg(j, cam_dqt=None):
    """返回相机风格的 JPEG。cam_dqt 为相机那段 DQT 的原始字节。"""
    out = [b"\xff\xd8"]
    sos_tail = None
    for m, off, total, _pl in segments(j):
        if m in (0xE0, 0xFE):               # 丢掉 APP0(JFIF) 与 COM
            continue
        if m == 0xDB:                       # 量化表换成相机的
            out.append(cam_dqt if cam_dqt else j[off:off + total])
            continue
        if m == 0xC0 or m == 0xC1 or m == 0xC2:
            seg = bytearray(j[off:off + total])
            payload = seg[4:]
            nc = payload[5]
            for k in range(nc):
                # 分量表：id(1) 采样(1) 量化表号(1)
                if payload[6 + 3 * k] != 1:      # 色度分量 -> 用表 1
                    payload[8 + 3 * k] = 1
                # 采样统一成 4:2:2：Y=2x1，色度=1x1
                payload[7 + 3 * k] = 0x21 if payload[6 + 3 * k] == 1 else 0x11
            seg[4:] = payload
            out.append(bytes(seg))
            continue
        if m == 0xDA:                       # SOS 头 + 熵编码数据
            out.append(j[off:off + total])
            sos_tail = None
            continue
        if m == 0xD9:
            out.append(b"\xff\xd9")
            continue
        out.append(j[off:off + total])
    return b"".join(out)


def jpeg_info(j):
    sof, dqt, marks = None, 0, []
    for m, off, total, pl in segments(j):
        marks.append(hex(m))
        if m in (0xC0, 0xC1, 0xC2):
            nc = pl[5]
            comps = [(pl[6 + 3 * k], f"{pl[7 + 3 * k] >> 4}:{pl[7 + 3 * k] & 15}",
                      pl[8 + 3 * k]) for k in range(nc)]
            sof = (struct.unpack(">H", pl[1:3])[0], struct.unpack(">H", pl[3:5])[0], comps)
        elif m == 0xDB:
            dqt += 1
    return marks, sof, dqt


if __name__ == "__main__":
    src = Path(sys.argv[1]).read_bytes()
    tmpl = Path(sys.argv[2]).read_bytes() if len(sys.argv) > 2 else None
    cam = None
    if tmpl:
        cam = find_dqt(ncdt_jpegs(tmpl).get("NCTH", b""))
    out = cam_style_jpeg(src, cam)
    Path(sys.argv[3]).write_bytes(out)
    m, sof, q = jpeg_info(out)
    print(f"  {len(src):,}B -> {len(out):,}B")
    print(f"  标记={m[:8]}  量化表段={q}  SOF={sof}")
