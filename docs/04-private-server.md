# 04 · 私服实现

`server/` 是一个能跑通完整对局的服务端。协议全部来自真实客户端抓包与
`data/proto_schema.txt`，没有猜测字段。

## 模块

| 文件 | 职责 |
|---|---|
| `tnt_proto.py` | 消息帧封装 + protobuf 编解码（与 `tools/frame.py` 同源） |
| `ballistics.py` | 转发 `tools/ballistics.py`，**不另存物理常数** |
| `combat_model.py` | 伤害公式 + 13 件宝物效果引擎 |
| `terrain.py` | 可变形高度图、弹坑雕刻、弹道碰撞 |
| `game_state.py` | Player / Room / Battle 状态机 |
| `server.py` | asyncio + websockets 主服务，监听 `0.0.0.0:8888` |
| `test_client.py` | 双客户端自动化测试 |

## 启动

```bash
cd server
uv run server.py         # 或 python server.py（需要 websockets>=12）
uv run test_client.py    # 登录 → 商城 → 开局 → 开火 → 结算
```

服务默认监听 `0.0.0.0:8888`。登录推送链直接重放 `data/login8_events.jsonl`，
所以**必须先有这个文件**，否则登录进不了大厅。

## 已实现的链路

| 链路 | 状态 | 备注 |
|---|---|---|
| 登录 → 大厅 | ✅ | 重放 29 条真实推送，客户端能进大厅 |
| 商城购买 | ✅ | `BuyReq` → 扣货币 → `BuyAck` + `PlayerDataNotify` 增量 |
| 建房 / 座位 | ✅ | `RoomNotify` 座位数组 |
| 战斗开局 | ✅ | `type0 Begin` 从真帧结构重建 |
| 回合与风 | ✅ | `type8 Action`，风按 XOR 公式编码 |
| 开火与弹道 | ✅ | `FireReq` → 服务端权威 `type14 Fire` 广播 |
| 掉血 / 死亡 | ✅ | `type9 Change` |
| 结算 / 翻牌 | ✅ | `Result` → 卡片 → 奖励 |
| 邮件 | ✅ | `ReadMailReq` → `ReadMailAck` |
| 聊天 | ✅ | 全服广播 |
| 签到 / 芯片 | ◐ | 请求链路通，个别计数显示源未定 |

## 几条实现上的硬约束

**Fire 帧只能发最小字段集。** 真帧里 `fire` 只有 `f1/f2/f3/f10`（外加人玩家射击时的
`f8 Blast`）。按 schema 补上 `f4 Weapon` / `f9 Sound` 会让客户端 Qt 层直接崩溃——
schema 有字段不代表真帧会发，客户端解析器是按真实帧形态硬解析的。

**风必须按 XOR 公式编。** 直接发 `wind * 10000` 客户端显示的风是错的。

**弹道只有一份实现。** `server/ballistics.py` 是转发层，物理在 `tools/ballistics.py`。

**登录链是数据驱动的。** `load_login_chain()` 读 `data/login8_events.jsonl`，
改这个文件就等于改登录推送，不需要改代码。

## 账号与密码

`server.py` 里的账号密码走环境变量，不留明文：

```bash
export TNT_PW_61001=...
export TNT_PW_72143=...
```

不设置时使用占位值 `test`，仅供本地测试。

## 已知限制

- **客户端接入需要自己搭桥**：真实客户端连的是固定地址，需要把它的服务器地址
  指向本机（hosts / DNAT / 代理，自行按环境选择）。仓库不含这类工具。
- **地图与地形是简化模型**：地面基线固定，没有做多地图随机匹配。
- **签到"当日 8 次战斗"计数**：领取链路通（`UseReq{f1:2185}` → `UseAck`），
  但面板显示 0/8 的数字来源没找到，多轮排查（PDP 各字段、`ReadProgressAck`、
  `CheckinAck`、`ChatNotify`、战后消息）全部阴性。
- **角色数据**：登录后展示的是一份重放快照，多账号差异化数据待做。
- **`v261016.1` 的 TLS 通道（8889）**：没做，需先分析证书校验是否为 pinning。

## 调试开关

| 环境变量 | 作用 |
|---|---|
| `TNT_ONESHOT=1` | 一发测试：开一炮就结算 |
| `TNT_MAP_ID` | 指定地图 |
| `TNT_REPLAY_REAL=1` | 用 `data/tnt_capture2_events.jsonl` 的真实帧回放开局与回合 |

上行原始消息会落到 `runtime_upstream.log`（仓库根目录），便于比对真实客户端行为。
