# tools —— 可复现的最小实现

每个脚本都能单独跑，末尾自带已知答案测试（KAT）。全部只用标准库
（`fit_ballistics.py` 另需 numpy/scipy，仅用于重新拟合，不参与运行）。

| 脚本 | 作用 | 自检 |
|---|---|---|
| `configkey.py` | 从壳 config（1062 B BMP）派生 RC4 key | 与已知 10 字节 key 比对 |
| `modrc4.py` | 修改版 RC4（KSA 标准 + PRGA 变体） | 932,539 B 真实 keystream 逐字节比对 |
| `frame.py` | 消息帧打包/解包 + protobuf 基础 | 188 条真实帧往返 |
| `central_dir.py` | 解析内层 ZIP 中央目录 | 3,567 条统计 |
| `inner_payload.py` | 内层 payload（RC4 + `[u32][zlib]`）解密 | 需已知 key，见文档 |
| `ballistics.py` | 服务器权威弹道 + 力度反解 | 对 2 发真实 track 回归，容差 15px |
| `fit_ballistics.py` | 对 track 样本重新拟合常数 | 输出残差，供对照 |

跑一遍全部自检：

```bash
python tools/configkey.py
python tools/modrc4.py
python tools/frame.py
python tools/central_dir.py stats data/central_dir_full.csv
python tools/ballistics.py
```

## 几个刻意的设计约束

**弹道只有一份实现。** `tools/ballistics.py` 是唯一真相，`server/ballistics.py`
只是转发。历史上服务端与客户端各存一份常数且漂移，导致轨迹差 30px 以上。

**帧头 4 字节是解压后长度。** 写错成压缩后长度，自建客户端之间能跑通，
真实客户端会拒收。

**风不是 `raw/10000`。** 真实算法是把 `wind_raw` 与 `speed`、`round`、
所有 `order` 元素逐位 XOR 再按 int32 解释，单位 0.1。详见
`docs/02-protocol.md` 第 6.4 节。

**未知就报错。** `inner_payload.py` 拿不到正确 key 时让 zlib 自己抛异常，
不返回"看起来像"的垃圾数据。
