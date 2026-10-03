"""列出相机文件的关键规格：编码、分辨率、帧率、帧数、参数集指纹、布局。

用于判断哪些文件可以直接拼成一个（参数集一致的才能拼）。
"""
import hashlib
import struct
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from boxio import walk, children, find   # noqa: E402


def u32(d, o):
    return struct.unpack(">I", d[o:o + 4])[0]


def u64(d, o):
    return struct.unpack(">Q", d[o:o + 8])[0]


def mdhd_of(d, trak):
    mdia = find(d, trak[0], trak[0] + trak[2], b"mdia")
    mdhd = find(d, mdia[0], mdia[0] + mdia[2], b"mdhd")
    return u32(d, mdhd[0] + 20), u32(d, mdhd[0] + 24)     # timescale, duration


def plan_track(d, trak):
    stbl = find(d, trak[0], trak[0] + trak[2], b"stbl")
    s0, s1 = stbl[0], stbl[2]
    szb = find(d, s0, s0 + s1, b"stsz")
    bd = d[szb[0] + 8:szb[0] + szb[2]]
    ss, cnt = struct.unpack(">II", bd[4:12])
    sizes = ([ss] * cnt) if ss else [u32(bd, 12 + 4 * i) for i in range(cnt)]
    co = find(d, s0, s0 + s1, b"co64") or find(d, s0, s0 + s1, b"stco")
    is64 = d[co[0] + 4:co[0] + 8] == b"co64"
    cb = co[0] + 8
    nc = u32(d, cb + 4)
    coffs = ([u64(d, cb + 8 + 8 * i) for i in range(nc)] if is64
             else [u32(d, cb + 8 + 4 * i) for i in range(nc)])
    stsc = find(d, s0, s0 + s1, b"stsc")
    sb = d[stsc[0] + 8:stsc[0] + stsc[2]]
    nse = u32(sb, 4)
    ent = [(u32(sb, 8 + 12 * i), u32(sb, 12 + 12 * i)) for i in range(nse)]
    per = []
    for i, (first, spc) in enumerate(ent):
        last = ent[i + 1][0] - 1 if i + 1 < len(ent) else nc
        per.extend([spc] * (last - first + 1))
    if not per:
        per = [cnt]
    return sizes, coffs, per


def codec_info(d, trak):
    stbl = find(d, trak[0], trak[0] + trak[2], b"stbl")
    stsd = find(d, stbl[0], stbl[0] + stbl[2], b"stsd")
    inner = stsd[0] + 16
    esz = u32(d, inner)
    fmt = d[inner + 4:inner + 8].decode("latin1", "replace")
    entry = d[inner:inner + esz]
    out = {"format": fmt, "hash": "", "cfg": "", "npar": 0}
    for tag in (b"hvcC", b"avcC"):
        p = entry.find(tag)
        if p >= 0:
            ln = struct.unpack(">I", entry[p - 4:p])[0]
            rec = entry[p + 4:p - 4 + ln]
            out["cfg"] = tag.decode()
            out["hash"] = hashlib.sha1(rec).hexdigest()[:10]
            out["cfg_len"] = len(rec)
            out["npar"] = rec[22] if tag == b"hvcC" and len(rec) > 22 else (
                rec[5] & 0x1F if len(rec) > 5 else 0)
    return out


def main(paths):
    rows = []
    for p in paths:
        f = Path(p)
        d = f.read_bytes()
        tops = list(walk(d, 0, len(d)))
        moov = find(d, 0, len(d), b"moov")
        traks = [x for x in children(d, moov) if x[1] == b"trak"]
        v = plan_track(d, traks[0])
        a = plan_track(d, traks[1]) if len(traks) > 1 else ([], [], [])
        ci = codec_info(d, traks[0])
        ts, dur = mdhd_of(d, traks[0])
        ats, adur = mdhd_of(d, traks[1]) if len(traks) > 1 else (0, 0)
        rows.append(dict(name=f.name, size=len(d),
                         brand=d[8:12].decode("latin1", "replace"),
                         codec=ci["format"], cfg=ci["cfg"], hash=ci["hash"],
                         npar=ci["npar"], vframes=len(v[0]), ts=ts,
                         dur=dur / ts if ts else 0,
                         asamples=len(a[0]), a_dur=adur / ats if ats else 0,
                         mdat=next(x[0] for x in tops if x[1] == b"mdat")))
    print(f"{'文件':<26}{'大小':>12} {'品牌':<5}{'编码':<6}{'cfg':<6}{'参数集指纹':<12}"
          f"{'参数集':>5}{'视频帧':>8}{'时长s':>8}{'音频样本':>10}{'音频s':>8}{'mdat@':>10}")
    for r in rows:
        print(f"{r['name']:<26}{r['size']:>12,} {r['brand']:<5}{r['codec']:<6}"
              f"{r['cfg']:<6}{r['hash']:<12}{r['npar']:>5}{r['vframes']:>8}"
              f"{r['dur']:>8.3f}{r['asamples']:>10,}{r['a_dur']:>8.3f}{r['mdat']:>10,}")
    return rows


if __name__ == "__main__":
    main(sys.argv[1:])
