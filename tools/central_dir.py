# -*- coding: utf-8 -*-
"""解析外层 ZIP 中央目录（从 libnativesoHelper_2.ogg 解出后得到）。

中央目录本身用修改版 RC4 加密；解出来后是标准 ZIP 中央目录：

    PK\x01\x02 | 46 字节固定字段 | name | extra | comment

其中 local_offset 指向 843.6MB 密文块内的偏移（数据区布局见
docs/01-resource-container.md）。本工具把 CD 解析成 CSV 并做统计。

用法：
    python tools/central_dir.py parse <decrypted_cd.bin> [out.csv]
    python tools/central_dir.py stats data/central_dir_full.csv
"""
from __future__ import annotations

import csv
import struct
import sys


def parse(raw: bytes):
    """返回 [dict]，字段与 ZIP 规范一致（偏移为绝对字节偏移）。"""
    out = []
    i = 0
    while True:
        i = raw.find(b"PK\x01\x02", i)
        if i < 0:
            break
        if i + 46 > len(raw):
            break
        (sig, _ver_made, _ver_need, _flags, _method, _mtime, _mdate, crc,
         csize, usize, nlen, elen, clen, _disk, _iattr, _eattr,
         lho) = struct.unpack("<IHHHHHHIIIHHHHHII", raw[i:i + 46])
        name = raw[i + 46:i + 46 + nlen].decode("utf-8", "replace")
        extra = raw[i + 46 + nlen:i + 46 + nlen + elen]
        comment = raw[i + 46 + nlen + elen:i + 46 + nlen + elen + clen]
        out.append({
            "cd_offset": i,
            "name": name,
            "extra_hex": extra.hex(),
            "comment_hex": comment.hex(),
            "crc": crc,
            "compressed": csize,
            "uncompressed": usize,
            "local_offset": lho,
        })
        i += 46 + nlen + elen + clen
    return out


def stats(rows) -> None:
    import collections

    ext = collections.Counter()
    for r in rows:
        name = r["name"]
        ext[name.rsplit(".", 1)[-1].lower() if "." in name else "(none)"] += 1
    print("entries = %d" % len(rows))
    print("total uncompressed = %.1f MB"
          % (sum(int(r["uncompressed"]) for r in rows) / 1048576.0))
    for k, v in ext.most_common():
        print("  %-8s %d" % (k, v))


def main(argv) -> None:
    if len(argv) < 2:
        print(__doc__)
        return
    cmd = argv[1]
    if cmd == "parse":
        src = argv[2]
        dst = argv[3] if len(argv) > 3 else src + ".csv"
        rows = parse(open(src, "rb").read())
        with open(dst, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print("wrote %s (%d entries)" % (dst, len(rows)))
        stats(rows)
    elif cmd == "stats":
        with open(argv[2], encoding="utf-8", newline="") as fh:
            stats(list(csv.DictReader(fh)))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
