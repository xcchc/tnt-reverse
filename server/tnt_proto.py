# -*- coding: utf-8 -*-
"""TNT v260906.1 私服协议层:protobuf 编解码 + 消息封装。

消息帧格式(已实测验证,见 REVERSE_STATE.md §47/§49):
  ws 二进制帧(payload) = [4B BE 解压后长度][zlib]
  zlib 解压 = [<I msg_len][<I name_len][name ascii][protobuf]
  msg_len = name_len字段(4B) + name长度 + proto长度
上行客户端帧带 RFC6455 掩码(由 websockets 库自动去除);服务器下行不加掩码。
"""
import struct
import zlib

def enc_varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1  # protobuf 64 位补码约定(-1→ffffffffffffffffff01,与模板负数 count 编码一致)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
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
    """通用解码: {field_num: [values]} wire0=int, wire1=float64, wire5=float32, wire2=bytes"""
    out = {}
    i = 0
    while i < len(raw):
        tn, i = dec_varint(raw, i)
        num, wire = tn >> 3, tn & 7
        if wire == 0:
            v, i = dec_varint(raw, i)
        elif wire == 1:
            v = struct.unpack("<d", raw[i : i + 8])[0]
            i += 8
        elif wire == 5:
            v = struct.unpack("<f", raw[i : i + 4])[0]
            i += 4
        elif wire == 2:
            ln, i = dec_varint(raw, i)
            v = raw[i : i + ln]
            i += ln
        else:
            raise ValueError(f"wire{wire} unsupported @f{num}")
        out.setdefault(num, []).append(v)
    return out

def pack_message(name: str, proto: bytes = b"") -> bytes:
    """完整 ws payload: [4B BE 解压后长度][zlib[[<I msg_len][<I name_len][name][proto]]]

    注意 4B 头 = **解压后长度**(2026-10-17 真实客户端实测 decl=40=len(body)),
    不是压缩长度——旧版打 len(z) 只在自测客户端之间自洽。
    """
    body = struct.pack("<I", 4 + len(name) + len(proto)) + struct.pack("<I", len(name)) + name.encode("ascii") + proto
    z = zlib.compress(body)
    return struct.pack(">I", len(body)) + z

def unpack_message(payload: bytes):
    """返回 (name, proto_bytes);自动跳过前置垃圾(握手残留)。

    帧头 4B BE = **解压后长度**(2026-10-17 真实客户端 LoginReq 实测),
    zlib 流长度未知时用 decompressobj 吃完整流,不要按 decl 截断。
    """
    i = payload.find(b"\x78\x9c")
    if i < 4:
        raise ValueError("no zlib stream")
    body = zlib.decompressobj().decompress(payload[i:])
    msg_len, name_len = struct.unpack("<II", body[:8])
    name = body[8 : 8 + name_len].decode("ascii", "replace")
    proto = body[8 + name_len : 8 + name_len + (msg_len - 4 - name_len)]
    return name, proto
