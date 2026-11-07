# This is Not TNT · 逆向笔记

《This is Not TNT》（v260906.1）的 Android 逆向记录：一层壳、两层加密、一套自定义
WebSocket 协议、一个能跑通完整对局的私服。这里只保留**可复现**的部分，试错过程不收录。

> 本项目仅用于安全研究与互操作性学习。游戏资源与客户端版权归原作者所有，
> 本仓库不包含任何游戏本体、解包资源或美术素材。

## 目录

```
docs/     四篇技术文档（结论 + 复现步骤）
tools/    可复现的最小实现，每个都带已知答案测试
server/   私服（登录 / 大厅 / 房间 / 战斗 / 商城 / 邮件 / 签到）
data/     复现所需的最小数据样本（脱敏）
```

## 文档

| 文档 | 内容 |
|---|---|
| [01 资源容器与加密](docs/01-resource-container.md) | APK 真实布局、修改版 RC4、config→key 派生、双层结构、哪些闭环哪些没闭环 |
| [02 网络协议](docs/02-protocol.md) | 消息帧封装、BattleNotify oneof、风的 XOR 还原与镜像、FireReq、登录序列 |
| [03 战斗与物理](docs/03-battle-and-physics.md) | 弹道逐帧模型、常数标定、伤害与宝物模型、地形 |
| [04 私服实现](docs/04-private-server.md) | 模块划分、部署方式、已知限制 |

## 快速验证

不需要 APK、不需要设备，仓库自带数据样本，三条命令就能验完核心算法：

```bash
python tools/configkey.py    # config → 10 字节 RC4 key
python tools/modrc4.py       # 修改版 RC4 → 与 932,539 B 真实 keystream 逐字节一致
python tools/frame.py        # 帧编解码 → 188 条真实帧往返
python tools/ballistics.py   # 弹道 → 对 2 发真实 track 回归
```

输出里出现 `PASS` / `KAT PASS` 即为通过。

## 几个最容易踩的坑

| 坑 | 正确做法 |
|---|---|
| 帧头 4 字节写成压缩后长度 | 是**解压后**长度 `len(body)` |
| 风按 `wind_raw / 10000` 解 | 与 speed / round / 所有 order 逐位 XOR，再按 int32 解释，单位 0.1 |
| 弹道先更新速度再更新位置 | 反过来，先位置后速度 |
| 服务端自己再存一份物理常数 | 只保留 `tools/ballistics.py` 一份 |
| 给 Fire 帧补上"看起来该有"的字段 | 真帧省略的字段就不要再发，多发会让客户端崩溃 |
| 把 1 字节的 `bttets/` 文件当成资源 | 那是占位符，真资源在 843.6 MB 密文块里 |

## 还没做到的事

仓库不把这些写成结论，它们仍是开放问题：

- PNG / spine `.pack` 的内层解密没有闭环（音频已闭环，166/166 CRC 通过）；
- 资源索引里 `pos` / `length` 未能解析，因此按位置定向验证候选 key 走不通；
- `AssetsAES::loadFile` 的 `key = SHA256(dir ‖ salt16)` 推导没有用真实文件验证过；
- 签到面板"当日 8 次战斗"计数（0/8）的显示源未确定。

## 复现需要什么

核心算法的验证只需要本仓库 + Python 3.10+，无第三方依赖。

要做端到端复现（真客户端连私服）则需要自行合法取得对应版本的 APK、一台能跑
Android 的设备或模拟器，以及把客户端的服务器地址指向本机的工具。这些都不在仓库里。

```bash
cd server
uv run server.py        # 监听 0.0.0.0:8888
uv run test_client.py   # 两个模拟客户端跑通登录/商城/开局/开火
```

依赖只有 `websockets>=12`。

## 许可证

代码与文档以 [MIT](LICENSE) 发布。

**该许可不覆盖游戏本身**：客户端、美术资源、音频、商标等权利仍归原作者。
仓库内 `data/` 只含互操作性研究所需的最小协议数据。详见 [NOTICE.md](NOTICE.md)。
