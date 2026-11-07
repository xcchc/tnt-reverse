# -*- coding: utf-8 -*-
"""修改版 RC4（外壳 libjgcggt / libjgdtc 使用的流密码）。

与标准 RC4 的唯一差别在 PRGA：

    KSA   与标准 RC4 完全相同
    PRGA  初值 i=3, j=5（不是 0,0）
          每字节 i=(i+2)&0xff      （步长 2，标准是 1）
                 j=(j+S[i]+1)&0xff （多一个 +1）
                 swap(S[i],S[j])
                 out = S[(S[i]+S[j]) & 0xff]

依据：libjgcggt_64.so KSA@VA0x7600 / PRGA@VA0x7750 的反汇编，且经运行时
hook 逐字节验证 —— 见 docs/01-resource-container.md。

自检：python tools/modrc4.py  （跑 KAT，见 run_kat）
"""
from __future__ import annotations


def ksa(key: bytes) -> bytearray:
    """标准 RC4 KSA，返回 256 字节 S 盒。"""
    if not key:
        raise ValueError("empty key")
    s = bytearray(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    return s


def prga(s: bytearray, n: int, i: int = 3, j: int = 5) -> bytes:
    """修改版 PRGA，从 S 盒续生成 n 字节 keystream。

    s 会被原地修改，可分数次调用做「续流」。
    """
    out = bytearray(n)
    for k in range(n):
        i = (i + 2) & 0xFF
        j = (j + s[i] + 1) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[k] = s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def keystream(key: bytes, n: int) -> bytes:
    """一次性取 n 字节 keystream。"""
    return prga(ksa(key), n)


def xor(data: bytes, ks: bytes) -> bytes:
    """等长 XOR（流密码加/解密同一操作）。"""
    if len(data) != len(ks):
        raise ValueError("length mismatch: %d vs %d" % (len(data), len(ks)))
    return bytes(a ^ b for a, b in zip(data, ks))


def run_kat(path: str | None = None, limit: int = 1 << 20) -> None:
    """已知答案测试：用 config 派生的 key 重放，必须与运行时实测 keystream 一致。"""
    import hashlib
    import os

    from configkey import derive_key_from_config

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = path or os.path.join(root, "data", "outer_ks_real.bin")
    cfg = os.path.join(root, "data", "outer_c0_config.bin")
    if not os.path.exists(path):
        print("skip: %s 不存在（样本未随仓库分发）" % path)
        return
    real = open(path, "rb").read(limit)
    key = derive_key_from_config(cfg)
    got = keystream(key, len(real))
    ok = got == real
    print("key      =", key.hex(), "(%d B)" % len(key))
    print("keystream=", got[:16].hex(), "... %d B" % len(got))
    print("KAT %s  md5=%s" % ("PASS" if ok else "FAIL", hashlib.md5(got).hexdigest()))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    run_kat()
