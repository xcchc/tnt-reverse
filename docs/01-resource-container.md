# 01 · 资源容器与加密

本文记录《This is Not TNT》v260906.1 APK 里那 843.6 MB 资源是怎么装、怎么加密的，以及目前能复现到哪一步。

**证据来源**：`data/outer_c0_config.bin`（1062 B 的壳 config）、`data/outer_ks_real.bin`（运行时实测的 932,539 B 真实 keystream）、`data/central_dir_full.csv`（3,567 条内层中央目录）。

**标记**：✅ 已验证（有已知答案测试或运行时证据）　◐ 部分完成　○ 未解 / 未验证。

---

## 1. APK 的真实布局

安装包 867,804,372 字节，不是普通的"资源在 assets 里"结构：

```text
[0x00000000 .. 0x15EB440)   真实 local file header 簇 1（壳与系统条目，64 个）
[0x15EB440)                 raw/libnativesoHelper_1.ogg 的 LFH（CD 里 csize=0，空条目）
[0x15EB480 .. 0x33A6CC44)   843,585,476 B 连续密文块 ← 真正的资源数据区
[0x33A6CC44)                raw/libnativesoHelper_2.ogg 的 LFH（364,482 B）
[0x33A6CC44 .. 0x33B2B404)  真实 LFH 簇 2（收尾系统条目）
[0x33B40000 .. ...)         明文中央目录（3,632 条）
EOCD 之后另有 256 B 壳尾数据
```

要点：**APK 自带的中央目录是真的**，里面登记了 3,567 个资产条目，`flags=0 / method=0`，带真实 CRC32 与 csize/uncompressed_size。但这些条目的数据体全部落在中间那 843.6 MB 密文块里，文件里找不到对应的 LFH。

`assets/bttets/` 下 3,567 个文件全部是 **1 字节占位符**——那是给系统扫包用的空壳，真实资源一个都没有。✅

```text
APK
├── raw/libnativesoHelper_2.ogg   ← 修改版 RC4 → 内层 ZIP 中央目录（364,482 B）
├── raw/libnativesoHelper_1.ogg   ← CD 里是 0 字节；真正数据是中间那块 843.6 MB 密文
├── lib/**                        ← 47 个真实 .so（Qt 5.14 + 自研 libgame + 壳）
├── assets/bttets/**              ← 3,567 个 1 字节占位符
└── assets/libjgcggt*.so          ← 360 壳的多架构副本
```

`lib/libgame_arm64-v8a.so`（11,327,888 B）是**明文加载**的，没有被加密——被加密的只有资源容器。想静态分析游戏逻辑，直接从这个 ELF 入手，不必碰壳。✅

---

## 2. 两层加密

### 2.1 外层：修改版 RC4 覆盖整个 ZIP 容器

外层把整个内层 ZIP（中央目录 + 数据区）当一条流加密，**一条流、一个 key、从偏移 0 贯穿 843.6 MB**，不是每段重新 KSA。

曾验证过：拿 3,732 个已知 keystream 区段做前缀比对，只有 `ks_offset=0` 的第一段命中流首前缀，其余全部不命中 → 单流结论成立。✅

```text
KSA（标准 RC4，VA 0x7600，libjgcggt_64.so）
    S[i] = i
    j = 0
    for i in 0..255:
        j = (j + S[i] + key[i % klen]) & 0xFF
        swap(S[i], S[j])

PRGA（修改版，VA 0x7750；等价实现见 libjgdtc.so VA 0x26280）
    i = 3; j = 5                 ← 初值不是 0,0
    loop:
        i = (i + 2)     & 0xFF   ← 步长 2（标准是 1）
        j = (j + S[i] + 1) & 0xFF ← 多一个 +1
        swap(S[i], S[j])
        out = S[(S[i] + S[j]) & 0xFF]
```

可直接用的是 `tools/modrc4.py`：

```python
from modrc4 import keystream, xor
ks = keystream(key, len(cipher))   # 生成任意长度 keystream
plain = xor(cipher, ks)            # 加解密同一操作
```

### 2.2 密钥从哪来

key 不是编译进代码的字符串，而是由一个 1062 字节的 **BMP 形态 config** 现算出来的：

```text
config[0x00:0x02]  "BM"              —— 伪装成 24 位 BMP
config[0x06:0x08]  int16  key_len    —— 有符号
config[0x08:0x0a]  uint16 flag       —— 非 0 时 key_len -= 1
config[0x36:]      bit_input         —— 位提取输入

每个输出字节 k（0 ≤ k < key_len）：
    for j in 0..7:
        x = bit_input[k*8 + (7-j)]
        v |= (((x>>6) ^ (x>>4) ^ (x>>2) ^ x) & 1) << j
```

`(x>>6)^(x>>4)^(x>>2)^x & 1` 就是字节 `x` 的偶校验位——等于把 8 个字节当 8 个 bit 用。

对 APK 里这份 config，派生结果是 10 字节：

```text
76 56 57 34 23 91 23 53 56 74
```

（前 8 字节恰好是 ASCII `vVW4#`，中间夹一个 `0x91` 非 ASCII，所以别指望按字符串读。）

实现：`tools/configkey.py`，自检会与上面的 hex 比对。

```bash
python tools/configkey.py
# key = 76565734239123535674  (10 B)
# KAT PASS
```

### 2.3 解出来的东西

外层解出的明文本身是一个标准 ZIP。中央目录解开后以 `PK\x01\x02` 开头，3,567 条记录：

| 类型 | 数量 |
|---|---|
| `.png` | 3,133 |
| `.pack` | 246（spine 骨骼动画） |
| `.mp3` | 166 |
| `.jpg` | 20 |
| `.ttf` | 1 |

合计解压后约 804 MB。解析工具：`python tools/central_dir.py stats data/central_dir_full.csv`。

同一条 keystream 也正确解出了 843.6 MB 数据块里的 LFH——`APK[0x15EB480 + n] XOR ks[n]` 得到 `assets/activity/739e69400002f7e91050a642b263ced7.png` 的头部，CRC 与中央目录登记的 `0x69975a79` 一致。**这证明 `_2.ogg` 与数据块共用同一条流、同一相位起点。** ✅

### 2.4 已知答案测试

`data/outer_ks_real.bin` 是从运行时 hook 拿到的、长度 932,539 B 的真实 keystream（head `bad165bfdd3c4fc97b4ecd102a97d21a`）。它是本仓库唯一权威的判据：

```bash
python tools/modrc4.py
# key      = 76565734239123535674 (10 B)
# keystream= bad165bfdd3c4fc97b4ecd102a97d21a ... 932539 B
# KAT PASS  md5=c67df7f96ca9c7e166045ea260a4e6cc
```

采集方式：hook linker 的 `__dl__ZN6soinfo17call_constructorsEv`，在壳注册之后、init 执行之前下钩，抓到 `KSA(keylen=10)` → `PRGA(len=932539)` 的一次调用，其输出经 inflate 得 2,258,161 B。用上面的算法重放，932,539 / 932,539 字节**逐字节一致**。

> ⚠️ **不要用 `3957c44d4c61...` 那条流。** 历史记录里另有一条 364,482 B 的"外层 keystream"，早期由明文中央目录 XOR 密文恢复，与真实流**不同源**。基于它推出的相位公式 `START_I = (3 + 2*L) % 256` 已被证伪。仓库不收录这条流，看到以 `3957c44d` 开头的推导一律作废。

---

## 3. 内层：第二层加密

外层 ZIP 解出的每个条目 payload **仍然不是明文**，还有一层：

```text
payload --(mod RC4)--> [u32 LE 解压后长度][zlib stream] --(inflate)--> 明文
```

ZIP 层的 `flags=0 / method=0` 只是伪装，数据体照样加密。实现：`tools/inner_payload.py`。

### 3.1 已闭环的部分：音频

客户端运行后会把音频释放到 `/data/data/<pkg>/files/` 下，可以直接拉出来当**已知明文**：

- 166 个 MP3，合计 60,405,395 B，分布在 `anim/ bag/ battle/ chip/ common/ hall/ login/ main/ map/ res/ room/ talisman/` 12 个目录；
- 与中央目录里 166 条 MP3 记录逐条比对，**size 全等、CRC32 全等**（166/166）。

有了明文就能反推：`inner_cipher = mp3_plain XOR inner_ks`，再与外层密文合起来得到外层 keystream 片段。这条线是完整的。✅

### 3.2 未完成的部分：PNG / pack

PNG 与 spine `.pack` 的**内层 key 没有拿到**，全量解密没有闭环。已知的定性事实：

- 内层不是"一段一 KSA"，而是一条连续流，相位随数据偏移推进；
- 想覆盖全部 804 MB，必须先恢复内层 RC4 的 `(S, i, j)` 中间状态，再向前生成、向后回推；
- 向后回推是可逆的：`S_old[i] = S'[j']`，`j = j' - S_old[i] - 1`，`i -= 2`。

这条路试过多轮，没有收敛。**本文不把它写成结论。** ○

---

## 4. 资源索引结构（未验证）


运行时内存里存在一份已解析的资源索引，字段自描述，形如：

```text
u32 名字长度 | ASCII 名字 | 4 字节对齐填充
```

一条记录含 `path` / `pos` / `length` / `md5` 以及图集切图的几何量（`sh sw sx sy w x y rotate pos length image`）。

如果 `pos` 能完整解析出来，验证一个候选 salt/key 就只需要解 16 字节看文件头，不必盲扫 844 MB。**但这条线没有落地验证，仓库不含相关脚本。** ○

---

## 5. AES 资源加载路径（未验证）

另有一条与上面完全独立的加密路径，位于 `AssetsAES::loadFile`：

```cpp
data = Assets::loadFileData(owner->path);
if (owner->plain) return data;                        // owner+0x30
QByteArray salt = m_dir;                              // "Grapefruit/Game/TNT Mobile/"
salt.append(owner->salt);                             // owner+0x38，每文件 16 字节
QByteArray key = QCryptographicHash::hash(salt, 4);   // 4 = Sha256 → 32 字节
return m_aes.decode(data, key, owner->salt);          // iv = 同一个 salt
```

即 `key = SHA256("Grapefruit/Game/TNT Mobile/" ‖ salt₁₆)`、`iv = salt₁₆`，AES-256。

反汇编里所有外部调用都是靠"读 GOT 真实地址 → 在 Qt 库 `.dynsym` 里反查"还原出名字的：

| 桩地址 | 真名 |
|---|---|
| `0xee7630` | `QByteArray::append(QByteArray const&)` |
| `0xed8c50` | `QCryptographicHash::hash(QByteArray const&, Algorithm)` |
| `0xed8eb0` | `QCoreApplication::organizationName()` |
| `0xeeee60` | `QCoreApplication::applicationName()` |
| `0xede280` | `QString::arg(QString const&, int, QChar)` |
| `0xed8190` | `EVP_DecryptInit_ex` |

**状态：未验证。** 当时还没解析出索引里的 `pos`/`length`，拿不到任何一个文件的 `salt`，所以这套推导没有用真实文件对上过，相关脚本也没保留。它可能对、也可能是错的，不要当成结论引用。○

已排除的候选（省得再试一遍）：

- `salt` 为空 → `SHA256(dir)` 直接解 head/tail，无文件头；
- `salt = md5(明文)` 的 raw16 / hex32 两种形态，盲扫全容器无命中；
- `%1/Game/%2/` 模板串、`Grapefruit`、`TNT Mobile` 等变体，均无命中。

---

## 6. 复现清单

| 目标 | 命令 | 状态 |
|---|---|---|
| 验证 config→key 派生 | `python tools/configkey.py` | ✅ |
| 验证修改版 RC4（932 KB KAT） | `python tools/modrc4.py` | ✅ |
| 解析内层中央目录 | `python tools/central_dir.py stats data/central_dir_full.csv` | ✅ |
| 解密单个内层 payload | `python tools/inner_payload.py <payload> <key_hex>` | ◐ 仅音频 key 已知 |
| 全量解出 PNG / pack | — | ○ 未完成 |

已知常量一览（供交叉核对）：

```text
外层 key      = 76565734239123535674   （config 派生，10 字节）
外层 PRGA     = i0=3, j0=5, i+=2, j+=S[i]+1
外层 KAT      = bad165bfdd3c4fc97b4ecd102a97d21a...  （932,539 B）
明文布局      = [u32 LE 长度][zlib]
数据块起点    = APK 0x15EB480
中央目录起点  = APK 0x33A6CC44
```
