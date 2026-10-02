"""Decode the NCDT/NCTG metadata table the camera writes into every video.

NCDT holds a NCTG record table plus embedded previews. The record layout,
from ExifTool's Nikon.pm and Exiv2's quicktimevideo.cpp, is

    tag(u32) | format(u16) | count(u16) | value

The metadata describes the clip: frame count, frame rate, dimensions, audio.
Every check so far compared only MP4 tables; if the camera trusts NCTG instead,
this is where a mismatch would live.

Prints every record for a camera original and for my export, side by side.
"""
import struct
import sys
from pathlib import Path

# from ExifTool Nikon.pm
NCTG_TAGS = {
    0x03: "Make", 0x04: "Model", 0x05: "Software", 0x06: "Equipment",
    0x07: "Orientation", 0x08: "ExposureTime", 0x09: "FNumber",
    0x0A: "ExposureProgram", 0x0B: "ISO", 0x0C: "ExposureCompensation",
    0x0D: "MeteringMode", 0x0E: "Flash", 0x0F: "FocalLength",
    0x10: "DateTimeOriginal", 0x11: "CreationDate", 0x12: "Make2",
    0x13: "FrameCount", 0x14: "FrameRate2", 0x15: "FrameRate3",
    0x16: "FrameRate", 0x17: "FrameRate2b", 0x18: "FrameSize",
    0x19: "FrameWidth2", 0x1A: "FrameHeight2",
    0x22: "FrameWidth", 0x23: "FrameHeight",
    0x24: "FrameCount2", 0x25: "FrameCount3",
    0x30: "AudioChannels", 0x31: "AudioSampleRate",
    0x32: "AudioBitRate", 0x33: "AudioType", 0x34: "AudioCodec",
    0x40: "PreviewImage", 0x41: "PreviewImage2",
    0x50: "NCDB",
}

FMT = {1: ("u8", 1), 2: ("char", 1), 3: ("u16", 2), 4: ("u32", 4),
       5: ("urational", 8), 7: ("undef", 1), 8: ("s16", 2),
       9: ("s32", 4), 10: ("srational", 8), 11: ("f32", 4), 12: ("f64", 8)}


def child_boxes(d, s, e):
    off = s
    while off + 8 <= e:
        sz = struct.unpack(">I", d[off:off + 4])[0]
        ty = d[off + 4:off + 8]
        if sz < 8:
            return
        yield off, ty, sz
        off += sz


def find_ncdt(d):
    for off, ty, sz in child_boxes(d, 0, len(d)):
        if ty != b"moov":
            continue
        for uo, ut, us in child_boxes(d, off + 8, off + sz):
            if ut != b"udta":
                continue
            for no, nt, ns in child_boxes(d, uo + 8, uo + us):
                if nt == b"NCDT":
                    return no, ns
    return None


def parse_nctg(blob):
    """Walk the tag|format|count|value records."""
    out = []
    p = 0
    while p + 12 <= len(blob):
        tag = struct.unpack(">I", blob[p:p + 4])[0]
        fmt = struct.unpack(">H", blob[p + 4:p + 6])[0]
        cnt = struct.unpack(">H", blob[p + 6:p + 8])[0]
        name, unit = FMT.get(fmt, ("?%d" % fmt, 1))
        nbytes = unit * cnt
        if p + 8 + nbytes > len(blob):
            out.append((hex(tag), fmt, cnt, "<truncated>"))
            break
        raw = blob[p + 8:p + 8 + nbytes]
        vals = []
        for k in range(min(cnt, 4)):
            chunk = raw[k * unit:(k + 1) * unit]
            if fmt == 5:
                n, dd = struct.unpack(">II", chunk)
                vals.append(f"{n}/{dd}" if dd else str(n))
            elif fmt in (3,):
                vals.append(struct.unpack(">H", chunk)[0])
            elif fmt in (4, 9):
                vals.append(struct.unpack(">I" if fmt == 4 else ">i", chunk)[0])
            elif fmt == 2:
                vals.append(chunk.split(b"\x00")[0].decode("latin1", "replace"))
            elif fmt == 7:
                vals.append(f"{len(raw)}B")
            else:
                vals.append(chunk.hex())
        out.append((hex(tag), name, fmt, cnt, vals))
        p += 8 + nbytes
    return out


def report(path):
    d = Path(path).read_bytes()
    loc = find_ncdt(d)
    print("=" * 78)
    print(f"{Path(path).name}  ({len(d):,} B)")
    print("=" * 78)
    if not loc:
        print("  no NCDT")
        return
    no, ns = loc
    print(f"  NCDT at {no:,}, size {ns:,}")
    # list sub-boxes inside NCDT
    p = no + 8
    end = no + ns
    subs = []
    while p + 8 <= end:
        sz = struct.unpack(">I", d[p:p + 4])[0]
        ty = d[p + 4:p + 8]
        if sz < 8 or p + sz > end:
            break
        subs.append((ty.decode("ascii", "replace"), p, sz))
        p += sz
    print(f"  sub-boxes: {[(t, f'{s:,}') for t, _, s in subs]}")
    for ty, off, sz in subs:
        if ty != "NCTG":
            continue
        recs = parse_nctg(d[off + 8:off + sz])
        print(f"  NCTG records: {len(recs)}")
        for r in recs:
            if len(r) == 5:
                tag, name, fmt, cnt, vals = r
                print(f"    {tag:>8} fmt={fmt:<2} n={cnt:<4} {name:<20} {vals}")
            else:
                print(f"    {r}")


for p in sys.argv[1:]:
    report(p)
