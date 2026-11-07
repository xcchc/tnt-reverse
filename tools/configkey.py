# -*- coding: utf-8 -*-
"""从 360 外壳的 config（1062 字节，BMP 形态）派生 RC4 key。

结构（依据 fcn.00007d20 / fcn.00007e30 反汇编）：

    config[0x00:0x02]  "BM"            —— 伪装成 24 位 BMP
    config[0x06:0x08]  int16 key_len    —— 有符号
    config[0x08:0x0a]  uint16 flag      —— 非 0 时 key_len -= 1
    config[0x36:]      bit_input        —— 位提取输入

    每个输出字节 k 取 bit_input[k*8 .. k*8+7]，
    第 j 位来自 bit_input[k*8 + (7-j)]，位值 = 该字节的偶校验位：
        bit = ((x>>6) ^ (x>>4) ^ (x>>2) ^ x) & 1

对 APK 内这份 config，派生结果是 10 字节 key（十六进制）：

    76 56 57 34 23 91 23 53 56 74

自检：python tools/configkey.py
"""
from __future__ import annotations

CONFIG_OFF_KEYLEN = 0x06
CONFIG_OFF_FLAG = 0x08
CONFIG_OFF_BITINPUT = 0x36


def parity_bit(x: int) -> int:
    """偶校验位：bit0..bit7 中 1 的个数的奇偶。"""
    return ((x >> 6) ^ (x >> 4) ^ (x >> 2) ^ x) & 1


def derive_key(config: bytes, key_len: int | None = None) -> bytes:
    """按 config 自行声明的 key_len 派生（也可显式覆盖）。"""
    if config[0:2] != b"BM":
        raise ValueError("config 不是 BMP 形态：%r" % config[0:2])
    if key_len is None:
        key_len = int.from_bytes(config[CONFIG_OFF_KEYLEN:CONFIG_OFF_KEYLEN + 2],
                                 "little", signed=True)
        flag = int.from_bytes(config[CONFIG_OFF_FLAG:CONFIG_OFF_FLAG + 2], "little")
        if flag != 0:
            key_len -= 1
    if not 0 < key_len <= 64:
        raise ValueError("可疑 key_len=%d" % key_len)

    key = bytearray(key_len)
    for k in range(key_len):
        v = 0
        for j in range(8):
            x = config[CONFIG_OFF_BITINPUT + k * 8 + (7 - j)]
            v |= parity_bit(x) << j
        key[k] = v
    return bytes(key)


def derive_key_from_config(path: str) -> bytes:
    with open(path, "rb") as fh:
        return derive_key(fh.read())


if __name__ == "__main__":
    import sys

    p = sys.argv[1] if len(sys.argv) > 1 else "data/outer_c0_config.bin"
    k = derive_key_from_config(p)
    print("key = %s  (%d B)" % (k.hex(), len(k)))
    assert k.hex() == "76565734239123535674", "KAT 失败"
    print("KAT PASS")
