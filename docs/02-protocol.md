# 02 · 网络协议与消息帧

本文记录《This is Not TNT》v260906.1 客户端与服务器之间的消息层：一条明文 WebSocket 承载登录、大厅、房间和战斗，所有业务消息共用同一套 zlib + protobuf 封装。

**证据来源**：`data/proto_schema.txt`（从客户端二进制提取的 200+ 条消息定义）、`data/login8_events.jsonl`（完整登录推送链）、`data/b9_events.jsonl`（一局完整战斗）、`data/fire_req_samples.txt`（FireReq 上行样本）、`server/tnt_proto.py`（经真实客户端验证的帧编解码）。

**标记**：✅ 已验证（有抓包或真机复现）　◐ 推断（结构吻合但未闭环）　○ 未解。

## 1. 传输层

- 主游戏通道是 **明文 WebSocket**：`ws://<server>:8888`。客户端先发标准 HTTP Upgrade，之后所有帧都是 binary。
- 登录、大厅、房间、战斗**复用同一条 TCP 连接**，没有单独的战场端口或 UDP 通道；私服只需实现 8888 一个端点。✅
- 下行帧**不加 RFC6455 掩码**；上行帧**带掩码**（4 字节 mask key）。客户端还会周期性发 opcode `0x9` Ping。✅
- 8889/TLS 与 443/CDN 属于版本、SDK、资源通道，不承载业务消息；客户端连接 8888 后先完成 `101 Switching Protocols`，服务端随即推送登录链。✅

> 线上 8888 明文入口后来对新版客户端返回版本错误，旧客户端已无法直连官方服；本文协议来自历史抓包与本地私服回放，私服可完整复现。✅

## 2. 消息帧封装

### 2.1 字节布局

```text
ws binary payload（下行，无掩码）
┌────────────────────┬──────────────────────────────┐
│ 4B 大端 declen      │ zlib 流                       │
└────────────────────┴──────────────────────────────┘
zlib 解压后（body）
┌────────────────────┬────────────────────┬──────────────┬──────────┐
│ 4B 小端 msg_len     │ 4B 小端 name_len    │ name（ASCII） │ protobuf │
└────────────────────┴────────────────────┴──────────────┴──────────┘
```

- `declen = len(body)`，即 **解压后长度**，不是压缩后长度。✅
- `msg_len` **不含自己的 4 字节，但含 `name_len` 字段的 4 字节**：`msg_len = 4 + name_len + proto_len`。✅
- 因此 `declen = 4 + msg_len = 8 + name_len + proto_len`，两者相差固定 4。✅
- `name` 是消息名，如 `LoginReq`、`RoomNotify`、`BattleNotify`；没有 NUL 结尾。

一个 `LoginReq` 的合成示例（body 22 字节，msg_len 18）：

```text
payload: 00 00 00 16 | 78 9c ...
body:    12 00 00 00 | 08 00 00 00 | 4c 6f 67 69 6e 52 65 71 | 08 01 10 01 18 04
         msg_len=18   name_len=8    "LoginReq"                f1=1 f2=1 f3=4
```

> **旧实现错误**：把外层 4 字节写成压缩后长度。这样自建客户端之间能互通，但真实客户端会按解压后长度校验/切片而拒收。判别方法：解压后 `declen == len(body)`，不是 `declen == len(payload) - 4`。✅

### 2.2 解包实现要点

```python
import struct, zlib

def pack_msg(name: str, proto: bytes) -> bytes:
    # msg_len 含 name_len 字段(4B)，不含自身；declen 是 body 总长
    body = struct.pack("<II", 4 + len(name) + len(proto), len(name))
    body += name.encode("ascii") + proto
    return struct.pack(">I", len(body)) + zlib.compress(body)

def unpack_msg(payload: bytes):
    i = payload.find(b"\x78\x9c")          # 跳过 HTTP 101 升级残留
    if i < 4:
        raise ValueError("no zlib stream")
    # zlib 流没有长度字段：用 decompressobj 一直吃到流结束，不要按 declen 截断
    body = zlib.decompressobj().decompress(payload[i:])
    if struct.unpack(">I", payload[i - 4:i])[0] != len(body):
        raise ValueError("declen mismatch")
    msg_len, name_len = struct.unpack("<II", body[:8])
    name = body[8:8 + name_len].decode("ascii")
    proto = body[8 + name_len: 8 + name_len + (msg_len - 4 - name_len)]
    return name, proto
```

上行多一步：websocket 帧头里的 mask key 在 payload 之前，先把 payload 与 4 字节 mask 循环异或，再交给 `unpack_msg`。✅

### 2.3 固定解码步骤

1. 重组 TCP 流，按 RFC6455 切帧，跳过 `HTTP/1.1 101` 文本头；上行先解 mask，下行直接用 payload。
2. 定位 zlib 头（通常 `78 9c`），用 `decompressobj` 解出完整 body。
3. 读 `msg_len`、`name_len`，取消息名与 protobuf（长度 `msg_len - 4 - name_len`）。
4. 按消息名分发；`BattleNotify` 再看内层 `f2`，protobuf 嵌套按 schema 递归。

### 2.4 `msg_id` 不是类型号

抓包导出 JSONL 里的 `msg_id` 列取自帧头 `msg_len`（`4 + len(name) + len(proto)`）；导出 proto 被截断时重算会对不上，但列值本身是帧长，不是消息类型。✅

- Fire 帧的该列（旧记录曾把它称作 type 号）见过 207、221、223、238、240、242、277、323、351 等值，全部是不同轨迹点数造成的不同帧长。
- **识别 Fire 不能靠这个数，也不能靠外层 type 号**；唯一可靠条件是：消息名 `BattleNotify`、内层 `f2 == 15`、且 `f14` 里有 `f10` 轨迹结构。✅

## 3. protobuf 编解码

- 只出现 4 种 wire type：`0` varint、`1` fixed64（double）、`2` length-delimited（字符串/bytes/嵌套消息/重复子消息）、`5` fixed32（float）。
- 字段 tag = `(field_number << 3) | wire_type`，tag 本身也是 varint；字段号大于 15 时 tag 占 2 字节，手写解析器必须按 varint 读，不能按单字节读。
- `repeated` 字段就是重复出现同号 tag；`order`、轨迹点、座位、物品列表都按这种方式编码。
- **负数**按 64 位补码编码为 10 字节 varint（如 -37081 → `...ffff01`）。读回后先取低 32 位，再按 int32 解释；直接当无符号数会得到 18446744073709514535 这类值。✅
- `data/proto_schema.txt` 给出字段名和编号，是字段语义的第一手来源；但**schema 里存在的字段不等于真帧可以发**（见 Fire 铁律）。

## 4. 登录 → 大厅下行序列

以下为客户端登录后服务端连续推送的逻辑序列。`data/login8_events.jsonl` 共 29 帧，语义如下：✅

| # | 消息 | 关键字段 | 语义 |
|---|------|----------|------|
| 1 | `NoticeNotify` | f1 `Login` | 登录公告，HTML 富文本；抓包中另有一条 f2 `Scene` 的场景公告 |
| 2 | `ActivityListNotify` | f1 repeated `Id` | 当前开启的活动 id 列表（100、101、102、113…） |
| 3 | `EntryNotify` | f1 Mall、f2 Bag、f3 Friend、f4 Master、f5 Task、f6 Enhance、f7 Talisman、f8 Hang | 各入口开关，1=开放、0=关闭；真帧只发非零项 |
| 4 | `HallNotify` | f1 repeated `{Lv, Comment}` | 大厅横幅/公告条目，`Comment` 为 HTML |
| 5 | `StarPoolNotify` | f1 `Version`、f2 repeated `{Id, Count, Quality}` | 幸运星池；f1 是池版本号 |
| 6 | `PlayerDataNotify` | f1 Id、f2 Override、f3 Lv、f4 Ce、f5 Name、f7 Info、f8 Item、f28 Privacy | 自己全量数据：等级、战力、昵称、装备、背包、隐私设置 |
| 7 | `FriendListNotify` | f1 Override、f2 repeated `PlayerInfo` | 好友列表（全量） |
| 8 | `MailListNotify` | f1 Override、f2 repeated `{Index, Title, Read, Time}` | 邮件列表；`Index` 是邮件 uuid 字符串 |
| 9 | `ChannelListNotify` | f1 Override、f2 repeated `{Index, Name, Type, Color, Cost, Closable, Readonly}` | 聊天频道；全量帧带 f1=1，增量帧只带 f2 |
| 10 | `LoginAck` | f1 Ok、f2 Msg、f3 Created、f4 Poster、f5 Skin | 登录结果；成功时 f2 是会话 token（十六进制字符串），失败时是错误文案 |
| 11 | `ChatNotify` | f1 Index、f2 Id、f3 Name、f4 Content、f5 Color、f7 Html | 系统频道滚动消息，服务端可连续推送多条 |
| 12 | `PlayerDataNotify` | f8 Item、f16~f23 统计字段 | 二次推送，增量或补充（背包/装备/战绩） |
| 13 | `GainNotify` | f1 Title、f2 Item、f3 Tip、f4 IdUse、f5 Fly | 奖励弹窗；在领取/购买后按需插入，不是登录固定帧 |
| 14 | `RoomNotify` / `RoomAck` | 见第 5 节 | 登录后房间状态；进入/创建房间时返回完整座位表 |

- 实测帧序在 `ChannelListNotify` 之后还插有一条 `RoomNotify` 和一条 `PlayerDataNotify` 增量，`LoginAck` 仍在聊天消息之前；按抓包原样重放最稳，不要插入真帧没有的消息。`EntryNotify` 则是“省略字段即默认值”的典型：真帧只发 `f1=1,f2=1,f3=1,f6=1,f7=1`，f4/f5/f8 的 0 值不出现，补上无意义且可能改变解析路径。✅

## 5. 房间、商城、邮件与其他消息

### 5.1 登录请求

`LoginReq` 的字段来自 `proto_schema.txt`（上行登录帧在抓包中常被截断，未取得完整实帧，字段语义为 ◐）：

| 字段 | 名称 | 说明 |
|---|---|---|
| f1 | `Version` | 客户端版本号，实帧为 1 |
| f2 | `Id` | 账号 id |
| f3 | `Password` | 密码/凭据 |
| f4 / f5 | `Recover` / `Pm` | 恢复标记 / 设备平台标记 |
| f6 | `Token` | 续登 token；旧记录把它写成 f4，已按 schema 更正 |

### 5.2 房间

| 消息 | 字段 | 说明 |
|---|---|---|
| `CreateRoomReq` | f1 `Mode`、f2 `Password`、f3 `Vs`、f4 `Free` | 真帧 `f1=0, f3=1, f4=0`，f2 省略（无密码）。`Vs` 是 1v1/组队标记 |
| `RoomAck` | f1 `Ok`、f2 `Msg`、f3 `Room` | `Room` 实测：f1~f5 房间设置，f6 repeated 座位，f7 repeated 道具位 |
| `RoomNotify` | f1 `Override`、f2 `Mode`、f3 `Locked`、f4 `Free`、f5 `Map`、f6 `Item`、f7 `Prop`、f8 `Reason`、f9 `Bell`、f10 `Matching`、f11 `Emotion`、f12 `Recent` | 座位/房间状态增量；`f10=30` 是匹配倒计时，`f10=0` 结束匹配 |
| `RoomNotify_Item` | f1 `Pos`、f2 `State`、f3 `Id`、f4 `Vip` | 单个座位：位号、状态、玩家 uid、VIP 等级 |
| `StartReq` | f1 `Cancel` | 真帧 `f1=0` 表示开始；`StartAck` 无字段 |
| `LoadingReq` | f1 `Percent` | 客户端加载进度 0~100 |
| `SyncReq` / `PassReq` | f1 `N` / 空 | 回合同步 / 放弃回合 |

座位状态（实测还原，✅）：

| State | 含义 |
|---|---|
| 0 | 锁定/空位，点击可开启 |
| 1 | 等待中 |
| 2 | 已加入 |
| 3 | 已准备 |
| 4 | 房主 |

- 开战按钮由房间形态决定：4 座、空位 State=0、自己 State=4 的**单人房**显示“开始游戏”；出现第二人（State=3）后退化为成员准备按钮。真实流程可全程不发 `SetReadyReq`，`CreateRoomReq → StartReq → LoadingReq×N → SyncReq/PassReq` 即可开战。✅

### 5.3 商城与强化

| 消息 | 字段 | 说明 |
|---|---|---|
| `BuyReq` | f1 `Item{Id,Count,Money}`、f2 `Present{Id,Content}` | 实测 `f1={191,5,2002}`：物品 191、期限 5 天、货币 2002 |
| `BuyAck` | f1 `Ok`、f2 `Msg`、f3 `LvVip` | 成功时 f1=1；紧接 `PlayerDataNotify` 推 f8 增量（物品/货币新数量） |
| `EnhanceReq` | f1 `Id`、f2 `Promote`、f3 `Protect`、f4 `Crystal` | 芯片/装备强化；Crystal 是晶体槽 |
| `EnhanceAck` | f1 `Ok`、f2 `Msg`、f3 `Chip` | f3 返回新芯片等级/结果 |

`PlayerDataNotify.f8` 的 `ItemSlot` 是 `{Id, Count, Chip, Unequipped}`；购买 5 天物品后 `Count=432000`（秒），货币项则是新余额。✅

### 5.4 使用、邮件

| 消息 | 字段 | 说明 |
|---|---|---|
| `UseReq` | f1 `Id`、f2 `Notify` | 物品使用。签到领取走 **`UseReq{f1=2185}`**（`08 89 11`），不是 `CheckinReq` |
| `UseAck` | f1 `Msg`、f2 `CloseBag`、f3 `KeepMenu`、f4 `Gain` | 使用结果 |
| `ReadMailReq` | f1 `Index` | 邮件 uuid 字符串 |
| `ReadMailAck` | f1 `Ok`、f2 `Msg`、f3 `Id`、f4 `Name`、f5 `Content` | `Name` 标题、`Content` 正文 HTML；首读可附奖励 |
| `DelMailReq` / `DelMailAck` | f1 `Index` / f1 `Msg` | 删除邮件 |

## 6. 战斗协议

### 6.1 外层与类型分发

`BattleNotify` 顶层固定为：

| 字段 | 名称 | 说明 |
|---|---|---|
| f1 | `Sn` | 会话级单调序号 |
| f2 | `Type` | 战报类型码 |
| f3~f26 | oneof 载荷 | 按类型落到不同字段号 |

`f2`（类型码）与载荷字段号**不是同一个数**，必须查表：✅

| Type | 含义 | 载荷字段 | 备注 |
|---|---|---|---|
| 0 | Begin | f3 | 战斗初始化 |
| 2 | Result | f5 | 结算 |
| 3 | Loading | f6 | `{f1=uid, f2=progress}` |
| 4 / 13 | 回合切换 / Fly | 空 | bare 帧 |
| 7 | Card | f7 | 翻牌 |
| 8 | Action | f8 | 回合/行动顺序/风 |
| 9 | Change | f9 | 伤害、死亡、位置变化 |
| 11 | Move | f10 | 移动 |
| 15 | Fire | f14 | 开火/弹道 |
| 16 | Delay | f15 | 倒计时，`f15={f1:2000}` |

> **双层封装坑**：`battle_notify()` 已经按上表把载荷包进 oneof 字段。调用方只传内层字段，不能再包一层。例如 Loading 的真帧是 `08 32 10 03 32 06 08 <uid> 10 1e`（sn=50、type=3、f6={uid,30}）；若再套一次 f6，客户端加载条会一直停 0%。✅

### 6.2 oneof 字段全表

| 字段 | 名称 | 字段 | 名称 |
|---|---|---|---|
| f1 | `Sn` | f14 | `Fire` |
| f2 | `Type` | f15 | `Delay` |
| f3 | `Begin` | f16 | `Damage` |
| f4 | `Player` | f17 | `ResultMecha` |
| f5 | `Result` | f18 | `Ride` |
| f6 | `Loading` | f19 | `Storm` |
| f7 | `Card` | f20 | `Thunder` |
| f8 | `Action` | f21 | `BpBegin` |
| f9 | `Change` | f22 | `BpSelecting` |
| f10 | `Move` | f23 | `BpSelected` |
| f11 | `Stay` | f24 | `Meet` |
| f12 | `Error` | f25 | `ResultMeet` |
| f13 | `Fly` | f26 | `ResultSolo` |

### 6.3 `BattleNotify_Action`

| 字段 | 名称 | 类型 | 说明 |
|---|---|---|---|
| f1 | `Round` | varint | 回合号，从 1 开始 |
| f2 | `Order` | repeated varint | 本回合行动顺序，含 0 占位；当前行动者 = 第一个非 0 元素 |
| f3 | `Speed` | varint | 实测恒 100 |
| f4 | `Wind` | int32（varint） | 风原值，需 XOR 还原，见 6.4 |
| f5 | `Track` | repeated varint | schema 中存在，真帧未观测到 |

`data/b9_events.jsonl` 的三条真实 Action 解码（uid 为抓包数值，昵称不列出）：✅

| 帧 | Round | Order | Speed | Wind raw | XOR 还原 | 显示风 |
|---|---|---|---|---|---|---|
| pkt 160 | 1 | `[55536, 52593, 55535, 61001, 27448, 55533, 0]` | 100 | -37081 | -80 | **-8.0** |
| pkt 184 | 1 | `[55535, 61001, 27448, 55533, 0, 55536, 52593]` | 100 | 37059 | 84 | **+8.4** |
| pkt 226 | 2 | `[55536, 55535, 61001, 55533, 52593, 27448, 0]` | 100 | 37104 | 100 | **+10.0** |

### 6.4 风的还原与镜像

客户端显示风不是 `wind_raw / 10000`。真实算法是先把 `wind_raw` 与速度、回合、所有 order 元素逐位异或，再按 int32 解释：✅

```python
def decode_wind(wind_raw: int, speed: int, round_no: int, order: list[int]) -> float:
    h = (wind_raw & 0xFFFFFFFF) ^ (speed & 0xFFFFFFFF) ^ (round_no & 0xFFFFFFFF)
    for uid in order:
        h ^= uid & 0xFFFFFFFF
    h &= 0xFFFFFFFF
    return (h - (1 << 32) if h >= (1 << 31) else h) / 10.0   # 单位 0.1
```

- 把 XOR 结果按 int32 解释为 `h`（0.1 单位；上表为 -80、84、100），显示风 = `h / 10`，三条正好是 -8.0、+8.4、+10.0。✅
- 反推时先算 `h = round(显示风 × 10)`，再 `wind_raw = h ^ speed ^ round ^ XOR(order)`；结果按 uint32 写 varint，为负时按 64 位补码写成 10 字节。✅
- **旧版“实际值 = 显示值 × 10000”（即 `wind_raw / 10000`）的读法不正确**：只在少数样本上巧合接近真实显示值（上表 pkt 160 会得到 -3.7081，而客户端显示 -8.0）。正确口径是上面的 XOR 还原。✅
- 物理风：`W = 1.46643562 × Action.wind × side`，其中 `Action.wind = h / 10`（显示值）；`side` 由行动者在左/右半场取 ±1。水平加速度再乘积分系数 `K = 0.0186951412`。✅
- **镜像规则**：服务端下发的风是“行动方视角”。同一回合，左右两端客户端收到的 `wind` 互为相反数（1v1 双端 12 回合逐回合符号全反，实测）。✅ 实现时按每个客户端自己的视角发值，不要在下发后再统一取反；每个 Action 帧携带自己的风，3v3 里不同行动者的风并不相同，不能假设整局一个常量。

### 6.5 Fire 下行

`BattleNotify.f14` 的 schema 结构：

| 字段 | 名称 | 说明 |
|---|---|---|
| f1 | `Id` | 开火者 uid |
| f2 | `Left` | 左/右标记，真帧为 0 |
| f3 | `Pos` | `{f1=X, f2=Y}`，角色位置（不是枪口） |
| f4~f7, f9 | Weapon/Power/Action/Animation/Sound | schema 有，真帧省略，**不要发** |
| f8 | `Blast` | 真帧可见 `f8=482`（varint），只在人玩家射击出现 |
| f10 | `Combo` | repeated，弹道主体 |

`f10` 的实际嵌套（真帧逐字节吻合）：✅

```text
Fire.f10 = Combo {
  f1 = Branch {
    f1 = Priority = 0
    f2 = Track {
      f2 = repeated Pos { f1 = X, f2 = Y }   # 逐帧弹道点，首点为枪口
      f3 = repeated Event                     # 冲击/伤害/浮字事件
    }
    f3 = Angle (fixed64 double)               # 真实物理角
  }
}
```

- 轨迹首点 = 角色位置 + 28px 沿炮管方向：`(x + 28cosθ, y − 28sinθ)`；`Branch.Angle` 与 `FireReq.f3` 是同一物理角，0=右、90=上、>90=左上。✅

### 6.6 FireReq 上行

| 字段 | 名称 | wire | 说明 |
|---|---|---|---|
| f1 | `Round` | varint | 实测是本局射击序号（如 9、11、15、19…），不是回合号 |
| f2 | `Left` | varint | 武器槽；现有样本恒 0，语义未定 ◐ |
| f3 | `Angle` | varint | 角度 0~179，单位为度 |
| f4 | `Force` | fixed64 | 力度 double，0~100，与 UI 力度条一致 |
| f5 | `Hash` | bytes(16) | 每次射击的 16 字节随机值；用途未定，服务端可不校验 ◐ |

- 按下蓄力先发 `HoldReq{f1=shotId}`，松手再发 `FireReq` 五字段。✅
- 样本：`0809 1000 1821 21 <8B double 20.72> 2a10 <16B>` → shotId=9、槽 0、角度 33°、力度 20.72。✅ 力度是 double，不是 varint；按 varint 读会把 8 字节浮点位型读成荒唐的大整数。

### 6.7 Fire 载荷铁律

**Fire 只允许 `f1/f2/f3/f10` 最小集。** 按 schema 补 `f4:Weapon`、`f9:Sound` 等字段，会让真实客户端直接崩溃（Qt 进程退出，非断连）。✅ 这是踩过坑的负面铁律：schema 有字段名，不代表客户端解析器接受该形态。

- `f8=482` 是后来确认的真帧字段，但必须发成 **varint 形态**；把它发成 Blast 子消息会崩。未对齐真帧字节前，仍按 `f1/f2/f3/f10` 发。✅
- 轨迹点用原值整数坐标，y 轴向下，不要改成“地面为基准”的相对值；`Track.f3` 事件流（eff=24 地面冲击、eff=4 平台冲击、eff=14 伤害、eff=11 浮字、eff=13 击杀公告）是弹坑/飘字来源，不属于最小集。✅

## 7. 协议铁律

1. **oneof 载荷字段号逐 type 实测**，不要假设“type=N 就写 fN”。Fire(type 15) 落 f14，Delay(type 16) 落 f15，Loading(type 3) 落 f6，Result(type 2) 落 f5。
2. **封装只包一层**。`battle_notify()` 已按上表包好 oneof；载荷里再包同号字段会产生 `f5{f5{...}}` 这类畸形帧，客户端可能崩溃或静默忽略。
3. **真帧省略的字段不要补**。`EntryNotify` 的 0 值入口、Fire 的 f4~f7/f9、Result 的 f4 都是例子。补字段会改变解析路径。
4. **Fire 不能按类型号/帧长匹配**。导出里的 207/221/… 是 `msg_len`；判定看 `BattleNotify.f2=15` 和 `f14.f10` 结构。
5. **外层 4 字节是解压后长度**，且 `declen = msg_len + 4`；不是压缩长度。
6. **`msg_len` 含 `name_len` 字段、不含自身**：`msg_len = 4 + name_len + proto_len`。
7. **负数按 64 位补码**；读风、坐标、伤害时先 mask 到 32 位再转 signed，否则会得到 1.8e19 量级的值。
8. **上行掩码、下行不掩码**；mask key 在 payload 之前，顺序颠倒会让所有上行解码失败。
9. **zlib 流长度未知**，用 `decompressobj` 吃完整流；不要按 `declen` 截断，也不要用定长 `decompress`。
10. **登录期不要主动推签到消息**。`CheckinAck` 在登录链中插入会导致客户端登录状态机卡死、无限重试；签到领取只走 `UseReq{2185}`。✅

## 8. 未解与边界

- **签到计数 0/8 的显示源未定**：登录链消息、PDP f6/f23/f8 物品、`ReadProgressAck`、`CheckinAck`、聊天与战后消息均已排查，面板计数不随战斗或重登递增。候选只剩真服专用同步消息或客户端本地计数，本版不可达。○
- `FireReq.f2`（武器槽？）、`f5`（16B Hash 的生成算法/用途）与 `Fire.f4`~`f9` 的字段语义未定；服务端可透传或不校验。○
- `BattleNotify` type 5/6/10 的载荷未观测；`Change` 的 f16/f18/f19 部分子字段语义仍未定。○
- 多弹头武器 `f10` 内第二条轨迹的结构未定。○
- 登录上行 `LoginReq` 未取得完整实帧，字段语义来自 schema（下行完整可复现）；个别早期抓包在 Upgrade 后前几帧呈非 zlib 随机字节，疑为握手期临时封装，后续抓包从首帧即可解，私服未实现该层。◐/○

---

**最小可复现路径**：用 `server/tnt_proto.py` 的 `pack_message/unpack_message` 对齐帧封装 → 按 `data/login8_events.jsonl` 顺序重放登录链 → 用 `CreateRoomReq/StartReq/LoadingReq` 进房 → 用 `BattleNotify` 的 type 映射发 Begin/Loading/Action → 收 `FireReq` 后按 `f1/f2/f3/f10` 发 Fire → 用 type 9 的 `f18`（剩余 HP）和 `f19`（伤害）收尾。整条链在真实客户端上已验证。✅