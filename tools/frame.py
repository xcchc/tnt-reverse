# -*- coding: utf-8 -*-
"""消息帧编解码（ws binary payload <-> 消息名 + protobuf）。

帧格式（实测确认）：

    ws payload = [4B 大端: 解压后 body 长度][zlib stream]
    body       = [4B 小端: msg_len][4B 小端: name_len][name ASCII][protobuf]
    msg_len    = 4 + name_len + len(proto)

要点：
  * 4 字节头是**解压后**长度，不是压缩后长度（早期实现搞错过）。
  * zlib 流长度未知，解包时用 decompressobj 吃完整流，不要按 4B 头截断。
  * 上行客户端帧带 RFC6455 掩码（由 websocket 库剥离），下行不加掩码。

自检：python tools/frame.py
"""
from __future__ import annotations

import struct
import zlib


# ---------------------------------------------------------------- protobuf 基础

def enc_varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def dec_varint(buf: bytes, i: int):
    v = 0
    sh = 0
    while True:
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << sh
        if not b & 0x80:
            return v, i
        sh += 7


def tag(num: int, wire: int) -> bytes:
    return enc_varint((num << 3) | wire)


def pb_v(num: int, val: int) -> bytes:
    return tag(num, 0) + enc_varint(val)


def pb_b(num: int, val: bytes) -> bytes:
    return tag(num, 2) + enc_varint(len(val)) + val


def pb_s(num: int, s: str) -> bytes:
    return pb_b(num, s.encode("utf-8"))


def pb_f64(num: int, val: float) -> bytes:
    return tag(num, 1) + struct.pack("<d", val)


def pb_f32(num: int, val: float) -> bytes:
    return tag(num, 5) + struct.pack("<f", val)


def dec_pb(raw: bytes) -> dict:
    """通用解码：{field_num: [values]}；wire0=int, wire1=float64, wire5=float32, wire2=bytes。"""
    out: dict = {}
    i = 0
    n = len(raw)
    while i < n:
        tn, i = dec_varint(raw, i)
        num, wire = tn >> 3, tn & 7
        if wire == 0:
            v, i = dec_varint(raw, i)
        elif wire == 1:
            v = struct.unpack("<d", raw[i:i + 8])[0]
            i += 8
        elif wire == 5:
            v = struct.unpack("<f", raw[i:i + 4])[0]
            i += 4
        elif wire == 2:
            ln, i = dec_varint(raw, i)
            v = raw[i:i + ln]
            i += ln
        else:
            raise ValueError("unsupported wire type %d @field %d" % (wire, num))
        out.setdefault(num, []).append(v)
    return out


# ---------------------------------------------------------------- 帧封装

def pack_message(name: str, proto: bytes = b"") -> bytes:
    """组一个完整的 ws 下行 payload。"""
    nm = name.encode("ascii")
    body = (struct.pack("<I", 4 + len(nm) + len(proto))
            + struct.pack("<I", len(nm))
            + nm
            + proto)
    return struct.pack(">I", len(body)) + zlib.compress(body)


def unpack_message(payload: bytes):
    """返回 (name, proto)。自动跳过握手残留等前置垃圾。"""
    i = payload.find(b"\x78\x9c")
    if i < 0 or i > 8:
        raise ValueError("找不到 zlib 流")
    body = zlib.decompressobj().decompress(payload[i:])
    msg_len, name_len = struct.unpack("<II", body[:8])
    name = body[8:8 + name_len].decode("ascii", "replace")
    proto = body[8 + name_len:8 + name_len + (msg_len - 4 - name_len)]
    return name, proto


# ---------------------------------------------------------------- 自检

def _selftest() -> None:
    # 1) 往返
    proto = pb_v(1, 42) + pb_s(5, "hello")
    payload = pack_message("ChatNotify", proto)
    name, got = unpack_message(payload)
    assert name == "ChatNotify", name
    assert got == proto, got.hex()
    # 2) 4B 头 == 解压后长度
    body_len = struct.unpack(">I", payload[:4])[0]
    assert body_len == len(zlib.decompress(payload[4:])), "4B 头不是解压后长度"
    # 3) 真帧样本往返（data/b9_events.jsonl 的 proto 字段）
    import json
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", "b9_events.jsonl")
    n = 0
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                ev = json.loads(line)
                raw = bytes.fromhex(ev.get("proto", ""))
                if not raw:
                    continue
                nm, pr = unpack_message(pack_message(ev["name"], raw))
                assert nm == ev["name"] and pr == raw, ev["name"]
                n += 1
    print("frame selftest PASS（样本往返 %d 条）" % n)


if __name__ == "__main__":
    _selftest()
