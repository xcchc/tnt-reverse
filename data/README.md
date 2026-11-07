# data —— 复现所需的最小样本

只留能让 `tools/` 下每个自检跑起来的最小数据，全部已脱敏。

| 文件 | 大小 | 用途 |
|---|---|---|
| `outer_c0_config.bin` | 1,062 B | 壳 config（BMP 形态），`configkey.py` 的输入 |
| `outer_ks_real.bin` | 932,539 B | 运行时实测的真实 keystream，`modrc4.py` 的 KAT 基准 |
| `central_dir_full.csv` | 3,567 行 | 内层 ZIP 中央目录，资源清单与 CRC |
| `login8_events.jsonl` | 29 帧 | 完整登录推送链，私服登录即重放它 |
| `b9_events.jsonl` | 189 帧 | 一局完整战斗（6 人局），`frame.py` 往返测试 |
| `tnt_capture2_events.jsonl` | 117 帧 | 另一局战斗，`TNT_REPLAY_REAL` 回放用 |
| `traj_samples.json` | 5 发 | 真实 track + 对应 FireReq，弹道回归基准 |
| `weapon_dump.json` | 93 条 | 武器表内存快照，爆炸半径来源 |
| `proto_schema.txt` | 200+ 条 | 客户端二进制里提取的 protobuf 定义 |
| `fire_req_samples.txt` | — | `FireReq` 上行样本与解码 |

## 脱敏说明

抓包样本做过以下处理，不影响协议结构、字段编号与长度关系：

- 玩家昵称统一替换为 `玩家NN`；
- 会话 token 替换为等长的 `0`；
- 抓包里的服务器地址与内网 IP 替换为文档保留地址段
  （`203.0.113.0/24`、`198.51.100.0/24`、`192.0.2.0/24`）；
- 删除了 `tree` 字段（解析缓存，可随时用 `tools/frame.py` 重算）。

**注意**：替换昵称会让部分帧的帧长变化，因此脱敏后 `msg_id`
（等于帧头 `msg_len`）与重新计算的长度可能对不上。想知道原始长度关系，
用未脱敏的抓包验证——这一点在 `docs/02-protocol.md` 2.4 节有说明。

## 没放进来的东西

- 游戏 APK、解包后的资源、音频、美术素材；
- 内存 dump、截图、密钥扫描命中表等中间产物；
- 任何真实账号数据。
