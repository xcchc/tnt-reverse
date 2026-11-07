# -*- coding: utf-8 -*-
"""内层 payload 解密：修改版 RC4 + [u32 长度][zlib stream]。

外层 ZIP 解出的每个条目 payload 还有第二层：

    payload --(mod RC4)--> [u32 LE 解压后长度][zlib stream] --(inflate)--> 明文

密钥来源与内层 key 的取得方式见 docs/01-resource-container.md。
已知闭环只有音频（166 个 MP3）；PNG / pack 的 key 尚未取得，
本工具对未知 key 的情况会明确报错，不会假装解出。

用法：
    python tools/inner_payload.py <payload.bin> <key_hex>
"""
from __future__ import annotations

import struct
import sys
import zlib

try:
    from .modrc4 import xor, keystream
except ImportError:
    from modrc4 import keystream, xor


def decrypt_payload(payload: bytes, key: bytes):
    """返回 (明文, 声明的解压长度)。密钥不对时 zlib 会报错。"""
    if len(payload) < 8:
        raise ValueError("payload 太短：%d" % len(payload))
    stream = xor(payload, keystream(key, len(payload)))
    declared = struct.unpack("<I", stream[:4])[0]
    plain = zlib.decompress(stream[4:])
    if len(plain) != declared:
        raise ValueError("长度不符：声明 %d，实际 %d" % (declared, len(plain)))
    return plain, declared


def sniff(plain: bytes) -> str:
    """按魔数猜类型，便于人工确认解对了。"""
    if plain[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if plain[:2] == b"\xff\xd8":
        return "jpeg"
    if plain[:3] == b"ID3" or plain[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "mp3"
    if plain[:4] in (b"RIFF", b"OggS", b"PK\x03\x04"):
        return plain[:4].decode("latin1")
    if plain[:4] == b"\x00\x00\x01\x00":
        return "ttf/ico"
    return "unknown(%s)" % plain[:8].hex()


def main(argv) -> None:
    if len(argv) < 3:
        print(__doc__)
        return
    payload = open(argv[1], "rb").read()
    key = bytes.fromhex(argv[2])
    plain, n = decrypt_payload(payload, key)
    print("declared=%d actual=%d type=%s" % (n, len(plain), sniff(plain)))


if __name__ == "__main__":
    main(sys.argv)
