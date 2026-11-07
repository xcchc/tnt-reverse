# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=12"]
# ///
"""TNT v260906.1 私服 —— 登录/大厅/聊天/商城/战斗 最小可用服务端。

协议(全部实测,REVERSE_STATE.md §42-49):
  ws://host:8888  消息帧 [4B BE declen][zlib[[<I msg_len][<I name_len][name][proto]]]
  登录推送链 = login8_events.jsonl 原样重放(Notice→…→LoginAck)
  战斗 = BattleNotify{f1:seq, f2:type, f3:payload}
    type0 Begin(初始化) type8 Action(round/order/speed/wind_raw) type9 Change(HP)
    type14 Fire(权威弹道广播, combo+track) type15 位置同步 type4 回合
  商城 = BuyReq{f1{f1:itemId,f2:days,f3:currencyId}} → BuyAck{f1:1} + PlayerDataNotify{f8增量}

启动: uv run server.py   (监听 0.0.0.0:8888)
测试: 游戏客户端把 <server>:8888 指向本机(hooks/hosts),或用 tools/ws_client.py 模拟。
"""
import asyncio
import json
import os
import random
import struct
import sys
from pathlib import Path

from websockets.server import serve

sys.path.insert(0, str(Path(__file__).parent))
from tnt_proto import dec_pb, pack_message, pb_b, pb_s, pb_v, pb_f64, unpack_message  # noqa: E402
from ballistics import K, blast_of, track, first_hit, solve_target, muzzle  # noqa: E402
from game_state import Battle, Player, Room  # noqa: E402
from combat_model import (CombatState, compute_damage, apply_hit, maybe_revive,  # noqa: E402
                          regen_tick, DEFAULT_TREASURES)
from terrain import Terrain  # noqa: E402


def get_combat(battle: Battle, p: Player) -> CombatState:
    """取(或建)该玩家本场战斗的宝物运行态。"""
    st = battle.combat.get(p.uid)
    if st is None:
        st = CombatState(p)
        battle.combat[p.uid] = st
    if p.uid not in battle.stats:
        battle.stats[p.uid] = {"shots": 0, "hits": 0, "dmg": 0, "kill": 0, "death": 0}
    return st

ROOT = Path(__file__).resolve().parent.parent
LOGIN_REPLAY = ROOT / "data" / "login8_events.jsonl"

# 会话 token 在真实登录链里是随机 16 字节；仓库内数据已脱敏为全 0，
# 这里保持一致。真实部署时按链里下发的值透传即可。
SESSION_TOKEN = bytes.fromhex("0" * 32)
SESSION_TOKEN_HEX = b"0" * 32  # LoginAck f2 是 hex 字符串，等长替换用

RAW_LOG = ROOT / "runtime_upstream.log"


def raw_log(event: str, payload: bytes = b"", name: str = ""):
    """把真实客户端上行消息原样落盘(教训:大输出写文件,聊天只回摘要)。"""
    import time
    try:
        with open(RAW_LOG, "a", encoding="utf-8") as g:
            g.write("%.3f %s name=%s len=%d hex=%s\n"
                    % (time.time(), event, name, len(payload), payload.hex()))
    except Exception:
        pass

ACCOUNTS = {
    "61001": Player(61001, "玩家01", os.environ.get("TNT_PW_61001", "test")),
    "72143": Player(72143, "玩家02", os.environ.get("TNT_PW_72143", "test")),
}
# 种子货币(原型经济;真实客户端 UI 余额来自重放 PlayerDataNotify,服务端账本需一致)
for _p in ACCOUNTS.values():
    _p.currencies = {2001: 60000, 2002: 60000, 2003: 60000}

ROOMS: dict[int, Room] = {}
NEXT_ROOM = [1]
BATTLES: dict[int, Battle] = {}
DAILY_BATTLES: dict = {"date": "", "n": 0}  # §55.46 签到「当日8次战斗」计数(结算递增)


def load_login_chain():
    chain = []
    for line in open(LOGIN_REPLAY, encoding="utf-8"):
        ev = json.loads(line)
        name, proto_hex = ev.get("name"), ev.get("proto", "")
        if not name or not proto_hex:
            continue
        chain.append((name, bytes.fromhex(proto_hex)))
    return chain


LOGIN_CHAIN = load_login_chain()


def send(player: Player, name: str, proto: bytes = b""):
    if player.ws:
        raw_log("SEND", proto, name)
        try:
            asyncio.get_running_loop().create_task(player.ws.send(pack_message(name, proto)))
        except Exception:
            pass


def broadcast_all(players, payload: str, proto: bytes = b""):
    for p in players:
        send(p, payload, proto)


# BattleNotify 载荷字段号 = 消息类型号(type0 Begin 例外→字段3);真实实样 battle5_events pkt=7/29/36
TYPE_FIELD = {0: 3, 2: 5, 3: 6, 4: 4, 7: 7, 8: 8, 9: 9, 11: 10, 13: 21, 15: 14, 16: 15}
# §55.39 万帧全录实证(btn_field_map.py): type→载荷 oneof 字段 = {0:3, 2:5, 3:6, 7:7, 8:8,
#   9:9, 11:10, 15:14, 16:15}, type 4/5/6/10/13=空载荷。**旧缺 2/7 → Result/Card 落默认
#   字段 2(与 Type 自身同号冲突!)客户端 f5/f7 全空 → 胜负横幅/统计行/签到计数三症状全解。**
G_REPLAY = {"frames": {}}  # TNT_REPLAY_REAL 模式: pkt→(name,proto)

def battle_notify(seq: int, btype: int, payload: bytes) -> bytes:
    proto = pb_v(1, seq) + pb_v(2, btype)
    if payload:
        proto += pb_b(TYPE_FIELD.get(btype, btype), payload)
    return pack_message("BattleNotify", proto)


# ---------- 登录推送链 ----------
# 2026-10-17 崩溃根因补丁: 登录链是 61001 的 PlayerData,而战斗 Begin 的「我」=72143
# → PanelBattle::onOpen() 按自身 uid 找 BattlePlayer 得 null → BattleSpritePlayer::setTeam SIGSEGV
# (Bugly tomb: setTeam(shared_ptr<BattlePlayer> const&)+80 / PanelBattle::onOpen()+452)
# 修法: 全链 varint 61001→72143 + 名字 玩家01→玩家02(同 12 字节等长) 递归替换。
UID_OLD, UID_NEW = 61001, 72143
NAME_OLD = "玩家01".encode("utf-8")
NAME_NEW = "玩家02".encode("utf-8")


def _enc_varint(v: int) -> bytes:
    if v < 0:
        v &= (1 << 64) - 1  # 64 位补码;否则 v>>=7 永不为 0 死循环(gen_type15 同款教训)
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            break
    return bytes(out)


def _patch_pb(proto: bytes, target_uid: int = UID_NEW) -> bytes:
    # 2026-10-17 修正: 结构化重写在 1047B PlayerDataNotify 上因异常字段整体回退,
    # 导致自身 uid 仍是 61001 而 Begin 无 61001 → PanelBattle::onOpen 自身 BattlePlayer=null SIGSEGV。
    # 改为等长字节替换: varint 61001(c9dc03)→72143(cfb304) 均为3B; 名字 12B→12B。
    # 2026-10-18 每账号数据: 61001 本尊登录 → 链数据即本人勿改; 72143 → 换 uid/名字。
    if target_uid == UID_OLD:
        return proto
    return proto.replace(_enc_varint(UID_OLD), _enc_varint(UID_NEW)).replace(NAME_OLD, NAME_NEW)


# 72143 真实快照战斗力(login8 pkt71 195B PDP f4=9803);61001 模板 f4=12289。
# 均为 2 字节 varint(81 60 / cb 4c)→ 顶层结构行走等长替换(勿全量 replace: 20 81 60 可能出现在子消息)。
POWER_61001, POWER_72143 = 12289, 9803


def _patch_pdp_power(proto: bytes) -> bytes:
    old = b"\x20" + _enc_varint(POWER_61001)
    new = b"\x20" + _enc_varint(POWER_72143)
    if len(old) != len(new):
        return proto
    i = 0
    while i < len(proto):
        tag = proto[i]
        wt = tag & 7
        if tag == 0x20 and proto[i:i + len(old)] == old:
            return proto[:i] + new + proto[i + len(old):]
        i += 1
        if wt == 0:
            while i < len(proto) and proto[i] & 0x80:
                i += 1
            i += 1
        elif wt == 1:
            i += 9
        elif wt == 2:
            ln = 0
            sh = 0
            while True:
                if i >= len(proto):
                    return proto
                b = proto[i]
                i += 1
                ln |= (b & 0x7F) << sh
                sh += 7
                if not b & 0x80:
                    break
            i += ln
        elif wt == 5:
            i += 5
        else:
            return proto
    return proto


def _parse_ordered(proto: bytes):
    """[(field, wt, value)] 保序; value: int | bytes。非法返回 None。"""
    items = []
    i = 0
    while i < len(proto):
        tag = proto[i]
        i += 1
        fl = tag >> 3
        wt = tag & 7
        if fl == 0 or wt in (3, 4, 6, 7):
            return None
        if wt == 0:
            v = 0
            sh = 0
            while True:
                if i >= len(proto):
                    return None
                b = proto[i]
                i += 1
                v |= (b & 0x7F) << sh
                sh += 7
                if not b & 0x80:
                    break
            items.append((fl, 0, v))
        elif wt == 2:
            ln = 0
            sh = 0
            while True:
                if i >= len(proto):
                    return None
                b = proto[i]
                i += 1
                ln |= (b & 0x7F) << sh
                sh += 7
                if not b & 0x80:
                    break
            if i + ln > len(proto):
                return None
            items.append((fl, 2, proto[i:i + ln]))
            i += ln
        elif wt == 5:
            if i + 4 > len(proto):
                return None
            items.append((fl, 5, proto[i:i + 4]))
            i += 4
        elif wt == 1:
            if i + 8 > len(proto):
                return None
            items.append((fl, 1, proto[i:i + 8]))
            i += 8
    return items


def _encode_ordered(items) -> bytes:
    out = bytearray()
    for fl, wt, v in items:
        out.append((fl << 3) | wt)
        if wt == 0:
            out += _enc_varint(v)
        elif wt == 2:
            out += _enc_varint(len(v))
            out += v
        elif wt == 5:
            out += v
        elif wt == 1:
            out += v
    return bytes(out)


def _swap_uid(proto: bytes, old: int = 62804, new: int = 61327) -> bytes:
    """递归替换 varint uid(等长 3 字节),仅重建可完整解析的子消息。"""
    items = _parse_ordered(proto)
    if items is None:
        return proto
    out = []
    changed = False
    for fl, wt, v in items:
        if wt == 0 and v == old:
            out.append((fl, wt, new))
            changed = True
        elif wt == 2:
            rec = _swap_uid(v, old, new)
            if rec != v:
                changed = True
            out.append((fl, wt, rec))
        else:
            out.append((fl, wt, v))
    return _encode_ordered(out) if changed else proto


def rebuild_begin_enemy(proto: bytes) -> bytes:
    """BattleNotify(Begin) 原样重建,仅把左 item(敌方) f1 uid→61327(Lindsay, 好友缓存) f2 名字→Lindsay."""
    outer = _parse_ordered(proto)
    if outer is None:
        return proto
    new_outer = []
    for fl, wt, v in outer:
        if fl == 3 and wt == 2:
            body = _parse_ordered(v)
            if body is None:
                new_outer.append((fl, wt, v))
                continue
            new_body = []
            for fl2, wt2, v2 in body:
                if fl2 == 3 and wt2 == 2:
                    item = _parse_ordered(v2)
                    if item is None:
                        new_body.append((fl2, wt2, v2))
                        continue
                    new_item = []
                    for fl3, wt3, v3 in item:
                        if fl3 == 1 and wt3 == 0:
                            new_item.append((fl3, wt3, 61327))
                        elif fl3 == 2 and wt3 == 2:
                            new_item.append((fl3, wt3, b"Lindsay"))
                        else:
                            new_item.append((fl3, wt3, v3))
                    new_body.append((fl2, wt2, _encode_ordered(new_item)))
                else:
                    new_body.append((fl2, wt2, v2))
            new_outer.append((fl, wt, _encode_ordered(new_body)))
        else:
            new_outer.append((fl, wt, v))
    return _encode_ordered(new_outer)


def make_enemy_pdp(proto: bytes) -> bytes:
    """完整 PlayerDataNotify 模板重建为对手 61327/Lindsay(85级): f1 uid, f3 level, f5 名字."""
    items = _parse_ordered(proto)
    if items is None:
        return proto
    out = []
    for fl, wt, v in items:
        if fl == 1 and wt == 0:
            out.append((fl, wt, 61327))
        elif fl == 3 and wt == 0:
            out.append((fl, wt, 85))
        elif fl == 5 and wt == 2:
            out.append((fl, wt, b"Lindsay"))
        else:
            out.append((fl, wt, v))
    return _encode_ordered(out)


def do_login(player: Player):
    for name, proto in LOGIN_CHAIN:
        data = proto
        try:
            fields = dec_pb(proto)
            # LoginAck: f2=会话token 重生成;PlayerDataNotify: f1=自己的uid
            if name == "LoginAck":
                buf = bytearray(proto)
                if 2 in fields and isinstance(fields[2][0], bytes):
                    tok = fields[2][0]
                    off = proto.find(tok)
                    if off >= 0:
                        # 必须等长替换:f2 是 hex 字符串(32B ASCII),直接换 16B 原始字节
                        # 会错位长度前缀把 LoginAck 写坏(2026-10-17 真实客户端卡"登录中"根因)
                        newtok = SESSION_TOKEN_HEX[: len(tok)].ljust(len(tok), b"0")
                        buf[off : off + len(tok)] = newtok
                data = bytes(buf)
            elif name == "PlayerDataNotify" and 1 in fields:
                # 简化:原样重放(客户端能进大厅即可),资产数据由商城模块动态维护
                pass
        except Exception:
            data = proto
        data = _patch_pb(data, player.uid)  # 61001 本尊不改;72143 换 uid/名字(战斗自对象查找根因修复)
        if name == "PlayerDataNotify" and player.uid != UID_OLD:
            data = _patch_pdp_power(data)  # f4 战斗力 12289→9803(72143 快照真值,等长 varint)
        if name == "EntryNotify":
            # (2026-10-18 §55.16) 真实链只有 f1Mall/f2Bag/f3Friend/f6Enhance/f7Talisman=1;
            # f4=Master(达人)/f5=Task/f8=Hang 缺省 0 → 达人成功率 0.00% 根因候选(Entry 未开)。全 8 入口开。
            data = b"".join(pb_v(i, 1) for i in range(1, 9))
        if name == "PlayerDataNotify" and len(proto) > 800:
            # (2026-10-18 §55.16) 模板 Privacy{13:Talent}=[{5:5},{6:0}] 是子消息;Talent6 疑=强化成功率天赋
            # (0 → 达人加成 0.00%)。字节级 {6:0}→{6:10}(4B 等长 08061000→0806100a)。
            data = data.replace(b"\x08\x06\x10\x00", b"\x08\x06\x10\x0a")
            # (2026-10-18 §55.17) f25=LvVip=达人等级(实证: 模板/快照 f25=3 与 UI「Lv.3 达人」一致;
            # 达人宫殿特权表 #9=芯片强化概率提升20% 为 4 级特权)。等长 varint 3→9 解锁全部特权。
            data = data.replace(b"\xc8\x01\x03", b"\xc8\x01\x09")
            # (§55.57 ⑤计数源速验) 登录 PDP 尾追 f6=5+f23=5 测试值——面板若显示(5/8)即定案计数字段。
            data += pb_v(6, 5) + pb_v(23, 5)
        send(player, name, data)
    # (2026-10-18 f8 探测) 登录链后补发增量 PDP(42B 战斗帧同构,无 f2=Override=增量语义):
    # 金币2000×12345 + 点券2003×500 + 材料1000×50 —— 验证背包渲染/货币栏数据源
    # (2026-10-18 f8 车辆实证) 增量 delta 可渲染背包/货币栏;f2=Override=1 是全量替换(会清空装备,probe3 实证);
    # 1047B 模板自带的 f8×78 条目整包不被客户端采纳(原因未明),但把同批条目以增量重发即可渲染。
    # 语义: f2=1 全量替换 / 无 f2 增量合并;负数 count=-1 渲染无毒(原样显示);裸条目(无count)也渲染。
    f8_blobs = []
    for nm, proto in LOGIN_CHAIN:
        if nm != "PlayerDataNotify" or len(proto) < 800:
            continue
        try:
            off = 0
            while off < len(proto):
                key, o2 = 0, off
                sh = 0
                while True:
                    b = proto[o2]
                    o2 += 1
                    key |= (b & 0x7F) << sh
                    sh += 7
                    if not (b & 0x80):
                        break
                fl, wt = key >> 3, key & 7
                if wt == 0:
                    while proto[o2] & 0x80:
                        o2 += 1
                    o2 += 1
                elif wt == 2:
                    ln, o3 = 0, o2
                    sh = 0
                    while True:
                        b = proto[o3]
                        o3 += 1
                        ln |= (b & 0x7F) << sh
                        sh += 7
                        if not (b & 0x80):
                            break
                    if fl == 8:
                        f8_blobs.append(proto[o3:o3 + ln])
                    o2 = o3 + ln
                elif wt == 5:
                    o2 += 4
                elif wt == 1:
                    o2 += 8
                else:
                    break
                off = o2
        except Exception:
            pass
        break
    delta = pb_v(1, player.uid)
    ledger = ITEM_LEDGER.setdefault(player.uid, {})
    for blob in f8_blobs:
        delta += pb_b(8, blob)
        try:
            bf = dec_pb(blob)
            iid = bf.get(1, [None])[0]
            cnt = bf.get(2, [0])[0]
            if iid is not None:
                ledger[iid] = cnt
        except Exception:
            pass
    # 货币(2000=金币 2003=点券): 模板条目已含真值,仅在运行时余额非零时覆盖(否则 0 会盖掉模板值)
    for cid in (2000, 2001, 2002, 2003):
        cv = player.currencies.get(cid, 0)
        if cv:
            delta += pb_b(8, pb_v(1, cid) + pb_v(2, cv))
    # (2026-10-18 §55.18) 达人积分=物品 2084(f8 count=1650 与面板 1650/3000 完全吻合);
    # 4 级特权#9=芯片强化+20%。注入 5000 分解锁 4 级达人。
    delta += pb_b(8, pb_v(1, 2084) + pb_v(2, 5000))
    # (2026-10-18 §55.19 等级载体二分) 模板 count=3 条目全量→4: 1189/2055/2110/2176(20xx 块优先)
    for iid in (1189, 2055, 2110, 2176):
        delta += pb_b(8, pb_v(1, iid) + pb_v(2, 4))
    # (§55.57 ⑤计数源最后假说) f8 物品计数驱动面板(0/8):真档 2207:8/2209:8=目标8,当前计数物品
    # 疑为缺省 id(0=省略)。射测试值 3 覆盖 2203-2209 候选段——面板若显示(3/8)即定案物品 id。
    for iid in (2203, 2204, 2206, 2208):
        delta += pb_b(8, pb_v(1, iid) + pb_v(2, 3))
    send(player, "PlayerDataNotify", delta)
    raw_log("PDP_DELTA", delta, "uid=%d items=%d" % (player.uid, len(f8_blobs)))
    # (2026-10-17 移除对手 PDP 注入: make_enemy_pdp 在 1047B 模板上解析失败回退原样,反而污染自身份;
    #  根因已定位为自身 uid 不匹配,verbatim 流只需自身修复)
    # §55.51 负面铁律(实证 run148): 登录链后主动推 CheckinAck → **客户端登录状态机卡死**
    # (LoginReq 无限重试 6 次+注册弹窗回归,checkin13b.png)。回退: 不在登录期发任何签到消息!
    player.logged_in = True


# ---------- 邮件(2026-10-17 proto_schema.txt + sys1 真实样本) ----------
ITEM_LEDGER = {}  # uid -> {item_id: count}; do_login 从模板 f8 初始化,FuseReq 增减(§55.15)
# ReadMailReq{Index=1(mail uuid)} → ReadMailAck{Ok=1,Msg=2,Id=3,Name=4,Content=5}
MAILS = {}  # index(uuid) -> {"title","content","granted"}

DEFAULT_MAIL_CONTENT = (
    '你是第523位更新的玩家！获得以下奖励：<br />'
    '<span style="color:#ffdd22">点券</span>(10000)<br />'
    '<span style="color:#ffdd22">道具记忆卡</span>(7天)<br /><br />'
    '更新内容：<br />1.机甲模型、特效完全重构<br />2.商店新增一些C级武器<br />'
    '3.新增点券返利动画<br />4.其他细节优化和调整<br /><br />欢迎体验！'
)


def load_mails_from_chain():
    """从登录链的 MailListNotify{Override, Mail[]{Index,Title,Read,Time}} 建邮件表。"""
    for name, proto in LOGIN_CHAIN:
        if name != "MailListNotify":
            continue
        try:
            f = dec_pb(proto)
        except Exception:
            continue
        for item in f.get(2, []):
            try:
                it = dec_pb(item)
            except Exception:
                continue
            idx = it.get(1, [b""])[0]
            title = it.get(2, [b""])[0]
            idx = idx.decode("utf-8", "replace") if isinstance(idx, bytes) else str(idx)
            title = title.decode("utf-8", "replace") if isinstance(title, bytes) else str(title)
            MAILS[idx] = {"title": title, "content": "", "granted": False}


def handle_read_mail(player: Player, proto: bytes):
    try:
        idx = dec_pb(proto).get(1, [b""])[0].decode("utf-8", "replace")
    except Exception:
        idx = ""
    m = MAILS.get(idx)
    if m is None:
        send(player, "ReadMailAck", pb_v(1, 0) + pb_s(2, "邮件不存在"))
        return
    ack = pb_v(1, 1) + pb_s(4, m["title"]) + pb_s(5, m["content"] or DEFAULT_MAIL_CONTENT)
    send(player, "ReadMailAck", ack)
    if not m["granted"]:
        # 附件:点券10000(与正文一致);PlayerDataNotify f8={货币id,余额}
        m["granted"] = True
        player.currencies[2003] = player.currencies.get(2003, 0) + 10000
        pd = pb_v(1, player.uid) + pb_b(8, pb_v(1, 2003) + pb_v(2, player.currencies[2003]))
        send(player, "PlayerDataNotify", pd)


# ---------- 商城 ----------

SHOP_PRICES = {  # weaponId -> (currencyId, price, days)
    191: (2002, 32, 5),    # 祖卡火箭筒 C 32徽章 5天
    180: (2003, 290, 1),   # 血红匕首 C 290点券 1天(2026-10-17 真实客户端弹框实测)
}
# 货币id: 2001=钻石 2002=勇气徽章 2003=点券(以 BuyReq currencyId 实测为准)
CURRENCY_NAME = {2001: "钻石", 2002: "勇气徽章", 2003: "点券"}

def handle_buy(player: Player, proto: bytes):
    try:
        f = dec_pb(proto)
        inner = dec_pb(f[1][0]) if 1 in f else {}
        item_id = inner.get(1, [0])[0]
        days = inner.get(2, [0])[0]
        currency = inner.get(3, [0])[0]
    except Exception:
        send(player, "BuyAck", pb_v(1, 0))
        return
    # 未知商品兜底:按 290/天 计价(原型经济),保证真实客户端任意商品可成交
    price_entry = SHOP_PRICES.get(item_id) or (currency, 290 * max(days, 1), days)
    ok = 0
    if price_entry:
        cur, price, _d = price_entry
        bal = player.currencies.get(cur, 0)
        if bal >= price:
            player.currencies[cur] = bal - price
            player.items[item_id] = days * 86400
            ok = 1
            # PlayerDataNotify f8 增量: {item,expire} + {currency,balance}
            pd = pb_v(1, player.uid)
            pd += pb_b(8, pb_v(1, item_id) + pb_v(2, days * 86400))
            pd += pb_b(8, pb_v(1, cur) + pb_v(2, player.currencies[cur]))
            send(player, "PlayerDataNotify", pd)
    send(player, "BuyAck", pb_v(1, ok))


# ---------- 战斗 ----------

def build_room_notify(room, with_empty: bool = False) -> bytes:
    """RoomNotify{Override=1,Mode=2,Locked=3,Free=4,Map=5,Item[]{Pos,State,Id},Prop[]7}
    座位 State 真实状态机(battle6 全 delta 确认): 0=锁 1=等待中 2=已加入 3=准备 4=房主
    mumu_room2.png 真实房主房: Free=1(自由开局) + 8座位(空位State=1「等待中」) + 队友State=3 → 按钮=开始游戏"""
    body = pb_v(1, 1) + pb_v(2, room.mode) + pb_v(3, 0) + pb_v(4, 1) + pb_v(5, 0)  # f4=Free=1 自由开局
    seats = {}
    for pos, p in enumerate(room.players):
        # (2026-10-18 §55.20) State=3「准备」渲染 bot 座位(State=2 客户端显示空位,已试);
        # 0=房主(4)保持。开始按钮门未解→服务器侧自动开战(见 handle_create_room 尾部)。
        seats[pos] = (4 if pos == 0 else 3, p.uid)
    for pos in range(8):  # 真实 8 座(4上4下)
        if pos in seats:
            st, uid = seats[pos]
            body += pb_b(6, pb_v(1, pos) + pb_v(2, st) + pb_v(3, uid))
        elif with_empty:
            body += pb_b(6, pb_v(1, pos) + pb_v(2, 1))  # State=1 等待中
    for _ in range(3):
        body += pb_v(7, 0)
    return body


BOT_UID = 61001  # 客户端已知(登录链 PlayerDataNotify 缓存有名字/形象),渲染房间座位用

def make_bot() -> Player:
    bot = Player(BOT_UID, "玩家01", role="")
    bot.team = 0  # 真实: f3=Left=对手
    bot.hp = 85
    bot.x, bot.y = 1539, 320
    return bot


def handle_create_room(player: Player, proto: bytes, room_box: list = None, seq: list = None):
    """CreateRoomReq{Mode=1,Password=2,Vs=3,Free=4} → RoomAck{Ok,Msg,Room} + RoomNotify。
    快速加入实测上行: CreateRoomReq{Mode=0}(2026-10-17)。单人时放机器人对手。"""
    try:
        f = dec_pb(proto)
        mode = f.get(1, [0])[0]
    except Exception:
        mode = 0
    room = Room(NEXT_ROOM[0])
    NEXT_ROOM[0] += 1
    ROOMS[room.rid] = room
    room.mode = mode
    room.players.append(player)
    player.team = 0
    # mumu_room2.png 真实房主房: 房主(State=4)+队友(State=3准备) → 按钮=开始游戏
    if not os.environ.get("TNT_REPLAY_REAL") and len(room.players) < 2:
        room.players.append(make_bot())
    if os.environ.get("TNT_REPLAY_REAL"):
        # 重放模式: 原样真实 RoomAck(tnt_capture2 events pkt=41, 单人房 72143 + 3空位State=0)
        real_ack = bytes.fromhex("08011a2c0801100018002000280032040801100032040802100032040803100032080800100418cfb304380038003800")
        send(player, "RoomAck", real_ack)
        return room
    if room_box is not None and room_box:
        room_box[0] = room
    # (2026-10-18 §55.21) 按钮门排查: round-9 按钮可用(SetReadyReq 上行)时走的是 verbatim RoomAck
    # (tnt_capture2 pkt=41: 4座/Free=0/空位State=0锁/Player 座0 State=4 在末位)。合成房旧格式
    # (8座/Free=1/空位State=1)按钮零上行。改为镜像 verbatim + bot 插座1(State=3准备)。
    room_body = pb_v(1, 1) + pb_v(2, 0) + pb_v(3, 0) + pb_v(4, 0) + pb_v(5, 0)
    room_body += pb_b(6, pb_v(1, 0) + pb_v(2, 4) + pb_v(3, player.uid))   # 我=座0 房主
    room_body += pb_b(6, pb_v(1, 1) + pb_v(2, 3) + pb_v(3, BOT_UID))      # bot=座1 准备
    room_body += pb_b(6, pb_v(1, 2) + pb_v(2, 0))                          # 座2 锁
    room_body += pb_b(6, pb_v(1, 3) + pb_v(2, 0))                          # 座3 锁
    for _ in range(3):
        room_body += pb_v(7, 0)
    # 真实入口(tnt_capture2 pkt=41)只有 RoomAck{f1=1,f3=Room(含空位)},无单独 RoomNotify
    send(player, "RoomAck", pb_v(1, 1) + pb_b(3, room_body))
    # (2026-10-18 §55.20) 房间「开始游戏」按钮门未解(点按零流量,座位 State 3 渲染 bot 但按钮不触发);
    # 服务器侧 6s 自动开战: Begin 广播会让客户端加载战斗场景(重放流同机制)。bot 在场才触发。
    if seq is not None and any(p.uid == BOT_UID for p in room.players):
        async def _auto_start():
            await asyncio.sleep(6.0)
            if room.state == "lobby":
                raw_log("AUTOSTART", b"", f"rid={room.rid} players={len(room.players)}")
                try:
                    try_start_battle(room, player, seq)
                except Exception as e:
                    raw_log("AUTOSTART_ERR", b"", repr(e))
        try:
            asyncio.get_running_loop().create_task(_auto_start())
        except Exception:
            pass
    return room


def build_init(players: list[Player], map_id: int | None = None) -> bytes:
    if map_id is None:
        map_id = int(os.environ.get("TNT_MAP_ID", "229"))
    """type0 Begin 本体 = 真实实样 tnt_capture2 pkt=55 逐字节结构(240B):
    Begin{f1=Mode,f2=Map, f3=Left[敌], f4=Right[我], f6=100(speed), f7=10,
          f10=1,f11=1,f12=1, f13=2090,f14=1844(地图边界)} 【无 f5/f8/f9/f15】(§55.21 真实解码修正)
    队员: f1=uid,f2=name,f3=sex,f5=[装备{f1=道具,f3=到期}],f6=最大HP,f7=8技能,f8={x,y},
          f9=怒气,f10=Ce,f13=double,f14=4/3,f16=1,f17=6/3 【无 f15】"""
    def f64raw(tag: int, hexstr: str) -> bytes:
        return bytes([tag]) + bytes.fromhex(hexstr)  # wire1 fixed64 原样字节
    def left_item(p: Player) -> bytes:  # 敌方=真实62804数值
        e = pb_v(1, p.uid) + pb_s(2, p.name) + pb_v(3, 0 if p.uid == 61001 else 1)
        for iid, exp in ((11127, None), (1049, None), (104, 2026)):
            e += pb_b(5, pb_v(1, iid) + (pb_v(3, exp) if exp else b""))
        e += pb_v(6, 20 if os.environ.get("TNT_ONESHOT") else 1870)  # §55.41 调参回调: 默认真实1870;TNT_ONESHOT=1 一发定胜测试
        for slot in (170, 163, 164, 0, 0, 0, 639, 0):
            e += pb_v(7, slot)
        e += pb_b(8, pb_v(1, 1539) + pb_v(2, 320))
        e += pb_v(9, 89) + pb_v(10, 9580)  # §55.26 f9=初始怒气(非HP!);f2:15/30..100=怒气槽增15/回合
        e += f64raw(0x69, "05504237aafe4940") + pb_v(14, 4) + pb_v(16, 1) + pb_v(17, 6)
        return e
    def right_item(p: Player) -> bytes:  # 我方=真实72143数值(8件装备)
        e = pb_v(1, p.uid) + pb_s(2, p.name) + pb_v(3, 0 if p.uid == 61001 else 1)
        for iid, exp in ((10013, 2021), (10014, None), (10015, None), (10214, None),
                         (1134, None), (10048, None), (10490, None), (136, 2024)):
            e += pb_b(5, pb_v(1, iid) + (pb_v(3, exp) if exp else b""))
        e += pb_v(6, 2290)
        for slot in (174, 171, 172, 0, 0, 0, 634, 0):
            e += pb_v(7, slot)
        e += pb_b(8, pb_v(1, 585) + pb_v(2, 325))
        e += pb_v(9, 85) + pb_v(10, 9581)
        e += f64raw(0x69, "555237299c194040") + pb_v(14, 3) + pb_v(16, 1) + pb_v(17, 3)
        return e
    # (2026-10-18 §55.28 座位↔侧位实验) 客户端按座位绑定战斗自对象(seat0↔f3): 房间 seat0=登录玩家,
    # 故 f3 必须=自己。旧序 f3=bot → 客户端把 bot 条目当自己(run110: bot 血条空了却报「您失败了」)。
    # 角色数据按 uid 配模板(self=72143 真实8装备/敌=62804 3装备),位置不变(自 585,325 / 敌 1539,320)。
    left = b"".join(pb_b(3, left_item(p)) for p in players if p.team == 0)  # §55.30 真形态: f3=对手
    right = b"".join(pb_b(4, right_item(p)) for p in players if p.team == 1)  # §55.30: f4=自己(两真抓包一致)
    # (2026-10-18 §55.21) 修正字段号: 真实 pkt=55 解码(BattleNotify{f3:Begin}内)速度=f6/10=f7
    # (旧 f5/f6 错位一位,客户端解析错位→拒收,卡倒计时0);边界 f13/f14=地图真值。
    # §55.31 地图制式实验: TNT_MAP_ID=121(battle5 击杀制图) 胜负判定疑与 229 宝石制不同。
    _bounds = {229: (2090, 1844), 121: (1588, 1235)}
    w, h = _bounds.get(map_id, (2090, 1844))
    return (pb_v(1, 0) + pb_v(2, map_id) + left + right
            + pb_v(6, 100) + pb_v(7, 10) + pb_v(10, 1) + pb_v(11, 1) + pb_v(12, 1)
            + pb_v(13, w) + pb_v(14, h))


def build_action(round_no: int, order: list[int], speed: int, wind_raw: int) -> bytes:
    """type8 Action:wind_ui = wind_raw ^ speed ^ round ^ orders(见battle_protocol.decode_action)。"""
    body = pb_v(1, round_no)
    for o in order:
        body += pb_v(2, o)
    body += pb_v(3, speed) + pb_v(4, wind_raw & 0xFFFFFFFF)
    return body


def build_fire(shooter: Player, theta: float, force: float, wind_ui: float, blast: int, targets: dict, terrain=None):
    """type14 Fire = gen_type15.build_type15 已验证真机结构(§55.21 客户端崩溃修复):
    f14{f1:uid, f2:left=0, f3:{x,y}角色位, f10:{f1:{f1:0, f2:轨迹点×{f1:x,f2:y}原值, f3:角度f64}}}
    旧结构自拼 f4-f9/branch/combo 多余字段+「地面为基准」y 编码 → 客户端渲染崩溃(run89 实证)。
    (§55.44) terrain=地形高度图: 弹道碰撞/落点/弹坑雕刻全走真实地形。"""
    mx, my = muzzle(shooter.x, shooter.y, theta)  # 轨迹自枪口起算(28px 沿炮管)
    gf = terrain.surface_y if terrain is not None else GROUND_REF
    pts = track(mx, my, theta, force, wind_ui, ground_y=gf)
    hit_uid, ground = first_hit(pts, targets, blast, ground_y=gf)
    raw_log("HITCHECK", b"", f"shooter={shooter.uid} hit={hit_uid} blast={blast} targets={targets} npts={len(pts)}")
    pts_blob = b"".join(pb_b(2, pb_v(1, int(round(x))) + pb_v(2, int(round(y)))) for x, y in pts[:300])
    # §55.57 ★地形冲击事件破解(真录 9 Fire 全解析): Track.f3=事件流,eff=24{1:24,2:{1:1}}=地面
    # 冲击(弹坑渲染!),eff=4{1:4,2:{1:1}}=平台冲击(不可破坏),eff=14=伤害/11=浮字/13=击杀公告。
    # 事件追加在轨迹点后(Track.f3)——弹坑客户端渲染的缺失环节。
    if ground is not None:
        _gx = int(round(ground[0]))
        _on_plat = any(abs(_gx - cx) <= hw for cx, _y, hw in getattr(terrain, "platforms", [])) if terrain else False
        _eff = 4 if _on_plat else 24
        pts_blob += pb_b(3, pb_v(1, _eff) + pb_b(2, pb_v(1, 1)))
        raw_log("IMPACT_EV", b"", f"eff={_eff} at=({ground[0]:.0f},{ground[1]:.0f}) on_plat={_on_plat}")
    inner = pb_v(1, 0) + pb_b(2, pts_blob) + pb_f64(3, float(theta))
    # §55.57 ★真 Fire 实测携带 f8=482(varint!仅人玩家射击/bot 无)——§55.50 崩溃实验是把 f8
    # 发成**消息形态**(Blast{...})=形态错误!真=f8:varint(疑=爆半径,弹坑渲染参数)。镜像真值测试。
    f = (pb_v(1, shooter.uid) + pb_v(2, 0)
         + pb_b(3, pb_v(1, int(shooter.x)) + pb_v(2, int(shooter.y)))
         + pb_v(8, 482)
         + pb_b(10, pb_b(1, inner)))
    return f, hit_uid, ground


GROUND_REF = 900.0  # ballistics.GROUND_Y 的镜像(编码用)

def parse_fire(proto: bytes):
    """FireReq{Round=1,Left=2,Angle=3,Force=4,Hash=5}(proto_schema.txt)。
    Angle=度;Force=double(fixed64)。力度→内部初速的映射待真实 FireReq 校准(FORCE_SCALE)。"""
    f = dec_pb(proto)
    theta = float(f.get(3, [45])[0]) if 3 in f else 45.0
    raw_force = float(f.get(4, [50.0])[0]) if 4 in f else 50.0
    return theta, raw_force


def fire_decode(proto: bytes):
    """FireReq 完整解码(2026-10-17 全 pcap 60+ 样本实证, 见 analysis/fire_req_samples.txt):
    f1=shotId(varint 本局递增), f2=武器槽(0), f3=角度(varint 0-90), f4=fixed64 double 力度(0-100),
    f5=16B 射击UUID。HoldReq{f1=shotId} 为按下蓄力, FireReq 为松开。"""
    out = {}
    i = 0
    while i < len(proto):
        tag = proto[i]; i += 1
        fl, wt = tag >> 3, tag & 7
        if wt == 0:
            v = 0; sh = 0
            while True:
                b = proto[i]; i += 1
                v |= (b & 0x7F) << sh; sh += 7
                if not b & 0x80:
                    break
            out.setdefault(fl, []).append(v)
        elif wt == 1:
            out.setdefault(fl, []).append(struct.unpack("<d", proto[i:i + 8])[0]); i += 8
        elif wt == 2:
            ln = proto[i]; i += 1
            out.setdefault(fl, []).append(proto[i:i + ln]); i += ln
        elif wt == 5:
            out.setdefault(fl, []).append(proto[i:i + 4]); i += 4
        else:
            break
    return {k: (v[0] if len(v) == 1 else v) for k, v in out.items()}


FORCE_SCALE = 1.0 / 100.0  # 假定力度 0~100(UI)→内部 0~1;待真实值校准

def make_wind_raw(wind_ui: float, round_no: int, order: list, speed: int = 100) -> int:
    """(2026-10-18 §55.24) 风同步: 客户端 wind_ui = signed32(wind_raw^speed^round^orders)/10
    (battle_protocol.decode_action 实证公式);反解 wind_raw = (wind_ui*10) ^ speed ^ round ^ orders。
    room.wind 直接取 wind_ui(与弹道模型单位一致),bot solve_target 与客户端显示风从此一致。"""
    ui = int(round(wind_ui * 10)) & 0xFFFFFFFF
    h = ui ^ (speed & 0xFFFFFFFF) ^ (int(round_no) & 0xFFFFFFFF)
    for o in order:
        h ^= int(o) & 0xFFFFFFFF
    return h


async def synth_settle(room, seq_box, winner_uid):
    """(2026-10-18 §55.22) 合成战斗结算序列:镜像真实(Result→f6→Card×2→f5),2.5s 节奏+10s 尾帧延时留截图窗口。"""
    if getattr(room, "settled", False):
        return
    room.settled = True
    room.state = "settle"
    # (2026-10-18 §55.27 定案) Result 条目=【按位次自报】: f1=自己条目位标志(1⇔item[0],0⇔item[1]);
    # 客户端渲染自己那条的标志(f5:1/f6:0/f7:1=胜,f5:0/f6:1=败)。5 真样本全为「自己败方」帧
    # (battle5: f1=0,自己=item[1]败; live14#1: f1=1,自己=item[0]败; tnt_battle2: f1=0,自己=item[1]败)。
    # 旧 verbatim live14 帧=61001(该局自己)败方帧 → 客户端渲染「您失败了」是正确行为(run106/109 实证)。
    # 真胜方帧从未捕获 → 对称构造: 自己在前 + 胜者标志 + f1=1。
    # (2026-10-18 §55.30 定案) 真形态两抓包一致: f3=对手、f4=自己;Result 条目=【f3,f4 序】(=uid 升序)。
    # f1:Win=自胜/胜者下标双解兼容, f2:LeftWin=左方(f3=敌)胜。
    # 胜方帧=battle5 败方帧严格镜像: f1:1,f2:0,条目[敌败,自胜]; 败方帧=battle5 逐字: f1:0,f2:1,条目[敌胜,自败]。
    # 旧全线 f2:1=「左方(=敌)胜」是「你失败了」根因(§55.27 矩阵 6 排列全带 f2:1)。
    self_p = next((p for p in room.players if p.uid == getattr(room, "self_uid", winner_uid)), None)
    if self_p is None:
        self_p = next(p for p in room.players if p.uid == winner_uid)
    other_p = next(p for p in room.players if p.uid != self_p.uid)

    def _ritem(p, win: int) -> bytes:
        # §55.44 战绩=本场真实统计(f2=hit,f3=shot,f4=dmg,f5=kill,f6=death,f7=mvp)
        _b = BATTLES.get(room.rid)
        st = ((_b.stats.get(p.uid) if _b else None)
              or {"shots": 0, "hits": 0, "dmg": 0, "kill": 0, "death": 0})
        base = (pb_v(1, p.uid) + pb_v(2, st["hits"]) + pb_v(3, st["shots"]) + pb_v(4, st["dmg"])
                + pb_v(5, st["kill"]) + pb_v(6, st["death"]))
        if win:
            return pb_b(3, base + pb_v(7, 1))
        return pb_b(3, base)

    # §55.54 ★EXP 语义终破(2026-10-18 settleM_8.png 真机铁证): 统计屏渲染「+972 / 总经验: 2001」
    # = f4 消息 **{1:总经验累计, 2:本次gain}**!旧 varint/后置/PDP增量四假说全败之谜=字段形态是
    # message 非 varint。真帧不带 f4=真服务器缺省时客户端显 0——补消息即修复 ⑦总经验:0。
    _gain = 972
    self_p.exp = getattr(self_p, "exp", 4356) + _gain
    exp_msg = pb_b(4, pb_v(1, self_p.exp) + pb_v(2, _gain))
    if self_p.uid == winner_uid:
        # §55.40 字段修复后真形态: 条目=[f3敌,f4自]序(=真 battle5 序!), f1:Win=胜者条目下标
        # (自=条目[1]→1), f2:LeftWin=0(左=f3=敌败)。真 battle5: f3 胜→f1:0,f2:1 —— 完全镜像!
        result = (pb_v(1, 1) + pb_v(2, 0) + _ritem(other_p, 0) + _ritem(self_p, 1) + exp_msg)
    else:
        result = (pb_v(1, 0) + pb_v(2, 1) + _ritem(other_p, 1) + _ritem(self_p, 0)
                  + pb_b(4, pb_v(1, getattr(self_p, "exp", 4356)) + pb_v(2, 0)))
    seq_box[0] += 1
    # §55.57 ⑤签到计数源假说: 真档 PDP f23=0 与面板「(0/8)」数值吻合→试 f23=当日战斗数。
    # 结算后 PDP 推送携带 f23=n+1(含本场;计数自增在 SETTLE_SYNTH 处,防重复计)。
    send(self_p, "PlayerDataNotify",
         pb_v(1, self_p.uid) + pb_v(4, self_p.exp) + pb_v(23, DAILY_BATTLES.get("n", 0) + 1)
         + pb_v(6, DAILY_BATTLES.get("n", 0) + 1))
    raw_log("EXP_DELTA", b"", f"uid={self_p.uid} exp={self_p.exp} gain={_gain} daily23={DAILY_BATTLES.get('n', 0) + 1}")
    # §55.39 修复双层封装: battle_notify 已按 TYPE_FIELD 包载荷字段,勿再 pb_b(5,...) 重包!
    room.broadcast(battle_notify(seq_box[0], 2, result))
    await asyncio.sleep(2.5)
    seq_box[0] += 1
    room.broadcast(battle_notify(seq_box[0], 6, b""))
    await asyncio.sleep(2.5)
    # §55.30 卡片=【f3,f4 序】(battle5 真序=胜者先)。§55.49 修复: 固定 [other,self] 序在自胜时
    # 会把败者 Error 卡前置,客户端误判「没造成任何伤害不能翻牌」——必须胜者先发!
    win_p = self_p if self_p.uid == winner_uid else other_p
    lose_p = other_p if win_p is self_p else self_p
    for p in [win_p, lose_p]:
        seq_box[0] += 1
        if p.uid == winner_uid:
            # §55.50 奖品改 2001:972(经验物品候选;旧2000=金币)——翻牌显示物品名可定 id 语义
            card = pb_v(1, p.uid) + pb_b(2, pb_v(1, 2001) + pb_v(2, 972) + pb_v(3, 6))
        else:
            card = pb_v(1, p.uid) + pb_v(3, 1)
        room.broadcast(battle_notify(seq_box[0], 7, card))
        await asyncio.sleep(2.5)
    await asyncio.sleep(10)
    seq_box[0] += 1
    room.broadcast(battle_notify(seq_box[0], 5, b""))
    room.state = "lobby"
    # §55.46 每日战斗计数(签到 0/8 门): 结算完成 +1
    import datetime as _dt
    _today = _dt.date.today().isoformat()
    if DAILY_BATTLES.get("date") != _today:
        DAILY_BATTLES["date"] = _today
        DAILY_BATTLES["n"] = 0
    DAILY_BATTLES["n"] += 1
    raw_log("SETTLE_SYNTH", b"", "winner=%s uids=%s daily=%d" % (winner_uid, [p.uid for p in room.players], DAILY_BATTLES["n"]))


def handle_fire(battle: Battle, shooter: Player, proto: bytes, seq_box: list):
    theta, raw_force = parse_fire(proto)
    force = raw_force * FORCE_SCALE
    # §55.47 战斗道具消费(本发生效): 0=48%伤害券 1=+2角度券 2=×3连发券(HUD 序;真语义待抓包校准)
    skill = getattr(battle, "skill_buff", {}).pop(shooter.uid, None)
    skill_mult, skill_txt = 1.0, ""
    if skill == 0:
        skill_mult, skill_txt = 1.48, "道具48% 伤害提升"
    elif skill == 1:
        theta = theta + 2.0
        skill_txt = "角度券+2"
    elif skill == 2:
        skill_mult, skill_txt = 1.20, "三连发"
    raw_log("FIREREQ", proto, f"theta={theta} raw_force={raw_force} skill={skill}")
    wind_ui = battle.room.wind
    blast = blast_of(shooter.equipped)
    targets = {p.uid: (p.x, p.y) for p in battle.room.players if p is not shooter and p.hp > 0}
    terrain = getattr(battle, "terrain", None)
    fire_payload, hit_uid, ground = build_fire(shooter, theta, force, wind_ui, blast, targets, terrain)
    # §55.47 小机制: 连续4次未命中→下次行动获得跟踪效果(必中);命中清零(VS加载 tip 文案实证)
    track_txt = ""
    if getattr(battle, "tracking", {}).get(shooter.uid) and not hit_uid and targets:
        hit_uid = next(iter(targets))  # 跟踪弹: 本发必中最近目标
        ground = (shooter.x, shooter.y)
        track_txt = "跟踪弹 命中"
        battle.tracking[shooter.uid] = False
    if hit_uid:
        battle.misses[shooter.uid] = 0
    else:
        battle.misses[shooter.uid] = battle.misses.get(shooter.uid, 0) + 1
        if battle.misses[shooter.uid] >= 4 and not battle.tracking.get(shooter.uid):
            battle.tracking[shooter.uid] = True
            track_txt = "连续4次未命中 获得跟踪效果"
            raw_log("TRACK_ON", b"", f"uid={shooter.uid} misses={battle.misses[shooter.uid]}")
    if hit_uid and track_txt == "跟踪弹 命中":
        raw_log("TRACK_HIT", b"", f"uid={shooter.uid} victim={hit_uid}")
    seq_box[0] += 1
    battle.room.broadcast(battle_notify(seq_box[0], 15, fire_payload))  # 真实 type15=f14 Fire
    if terrain is not None:  # §55.44 地形改变: 爆炸雕坑(命中=目标脚下;脱靶=落点)
        if hit_uid:
            _v = next(p for p in battle.room.players if p.uid == hit_uid)
            terrain.carve(_v.x, _v.y, blast)
        else:
            terrain.carve(ground[0], ground[1], blast)
        raw_log("CARVE", b"", f"hit={hit_uid} impact={ground} blast={blast} craters={terrain.craters}")
    shooter_st = get_combat(battle, shooter)
    battle.stats[shooter.uid]["shots"] += 1
    shooter_st.rage = min(100, shooter_st.rage + 15)  # 怒气+15/回合(§55.26)
    if hit_uid:
        victim = next(p for p in battle.room.players if p.uid == hit_uid)
        vic_st = get_combat(battle, victim)
        # §55.44 伤害公式+宝物效果(combat_model): 替换旧随机 28-42
        dist_px = ((victim.x - shooter.x) ** 2 + (victim.y - shooter.y) ** 2) ** 0.5
        shooter_max = getattr(shooter, "maxhp", 1870)
        victim_max = getattr(victim, "maxhp", 1870)
        missing = max(0, shooter_max - shooter.hp)  # 奥杜因之牙: 低于上限血量
        dmg, dlog = compute_damage(shooter_st, vic_st, dist_px, theta, wind_ui,
                                   int(getattr(shooter, "equipped", 1049)), missing, shooter_max)
        dmg, hit_ev, heal_sig = apply_hit(shooter_st, vic_st, dmg, victim_max)
        if skill_mult != 1.0:  # §55.47 道具增伤(在宝物乘区之后)
            dmg = int(round(dmg * skill_mult))
        raw_log("DAMAGE_CALC", b"",
                f"uid={shooter.uid}->{victim.uid} dist={dist_px:.0f} theta={theta} wind={wind_ui} "
                f"dmg={dmg} ev={hit_ev} log={dlog}")
        new_hp = max(0, victim.hp - dmg)
        if new_hp == 0:  # 不死鸟之羽: 致死免死+护盾2回合
            revived, shield_hp = maybe_revive(vic_st, victim_max)
            if revived:
                new_hp = shield_hp
                raw_log("REVIVE", b"", f"uid={victim.uid} shield={shield_hp}")
        # (2026-10-18 §55.26 三证交叉定案) f18=受害者**剩余HP**(f6 量纲;血条=f18/f6):
        #  ① battle5 Begin 10659 f6(max)=1580 与其 f18 序列峰值 1580 吻合→f18=受害者当前血;
        #  ② winK 硬编码 f18=1810 → 自己血条实测 1810/2290*163=129px——旧「f19 只动自己」结论实为 f18 写死所致;
        #  ③ f2=15/30..100=怒气槽(增15/回合,非HP),f9=89/85=初始怒气。伤害条目**不带 f2**。
        # 击杀帧 f18=0 必须下发→客户端本地归零触发胜负 UI;自己阵亡仍跳过死亡帧(run94 教训)直进结算。
        # §55.45 真实协议: f7 飘字=宝物效果文案(真帧实证"距离太近 伤害减少45%"),红数字=f19 独立渲染。
        # 有宝物修正时 f7 换效果文案+特效id 1136(真帧黄字样式);否则"失去生命N点"+1049。
        top_tre, top_txt = 1.0, ""
        for _k, _v in dlog.get("mults", {}).items():
            if abs(_v - 1.0) > abs(top_tre - 1.0):
                top_tre, top_txt = _v, _k
        _TRE_TXT = {
            "sniper": lambda v: f"远距离命中 伤害{'增加' if v >= 1 else '减少'}{abs(round((v-1)*100))}%",
            "melee": lambda v: f"距离太近 伤害{'增加' if v >= 1 else '减少'}{abs(round((v-1)*100))}%",
            "headwind": lambda v: f"逆风增伤 命中伤害增加{round((v-1)*100)}%",
            "high_arc": lambda v: f"高抛命中 伤害增加{round((v-1)*100)}%",
            "dragon_tooth": lambda v: f"奥杜因之牙 伤害增加{round((v-1)*100)}%",
            "counter": lambda v: f"伯恩的计数器 伤害增加{round((v-1)*100)}%",
            "unyield": lambda v: f"不屈药剂 伤害减少{abs(round((v-1)*100))}%",
            "rage_neck": lambda v: f"龙牙项链 伤害{'增加' if v >= 1 else '减少'}{abs(round((v-1)*100))}%",
            "metronome": lambda v: f"超级节拍器 攻击{'提升' if v >= 1 else '削弱'}",
            "rage_taken": lambda v: f"龙牙项链 受伤增加{round((v-1)*100)}%",
        }
        if skill_txt:  # 道具文案优先
            float_text, float_id = skill_txt, 1136
        elif track_txt:
            float_text, float_id = track_txt, 1136
        elif top_txt in _TRE_TXT and abs(top_tre - 1.0) >= 0.01:
            float_text, float_id = _TRE_TXT[top_txt](top_tre), 1136
        else:
            float_text, float_id = f"失去生命{dmg}点", 1049
        text_sub = bytes.fromhex("088084fdffffffffffff01") + pb_s(2, float_text)
        f7 = pb_b(7, pb_v(1, float_id) + pb_b(2, text_sub))
        entry = pb_v(1, hit_uid) + f7 + pb_v(18, new_hp) + pb_b(19, pb_v(1, dmg)) \
            + pb_b(22, pb_v(1, int(victim.x)) + pb_v(2, int(victim.y)))
        other = [p for p in battle.room.players if p.uid != hit_uid]
        entry2 = b"".join(pb_b(1, pb_v(1, p.uid) + pb_b(22, pb_v(1, int(p.x)) + pb_v(2, int(p.y))))
                          for p in other)
        victim.hp = new_hp
        battle.stats[shooter.uid]["hits"] += 1
        battle.stats[shooter.uid]["dmg"] += dmg
        # 暮色之月: 攻击方<20%血 → 吸血20%(上限500)
        if heal_sig == -1 and shooter.hp < shooter_max * 0.20:
            heal = min(500, int(dmg * 0.20))
            shooter.hp = min(shooter_max, shooter.hp + heal)
            raw_log("LIFESTEAL", b"", f"uid={shooter.uid} heal={heal}")
        died = (new_hp == 0)
        if died:
            battle.stats[shooter.uid]["kill"] += 1
            battle.stats[victim.uid]["death"] += 1
        if not (died and victim.uid == getattr(battle.room, "self_uid", None)):
            seq_box[0] += 1
            battle.room.broadcast(battle_notify(seq_box[0], 9, pb_b(1, entry) + entry2 + pb_v(2, 0)))
        if died:
            async def _delayed_settle():
                await asyncio.sleep(2.5)  # 留客户端死亡/胜利动画窗口
                await synth_settle(battle.room, seq_box, shooter.uid)
            asyncio.ensure_future(_delayed_settle())
            return
    else:
        shooter_st.hit_streak = 0  # 未命中断连(阿斯顿芯片)
        if track_txt:  # §55.47 跟踪激活飘字(未命中也显示: f7-only 条目)
            _ts = bytes.fromhex("088084fdffffffffffff01") + pb_s(2, track_txt)
            _e = pb_v(1, shooter.uid) + pb_b(7, pb_v(1, 1136) + pb_b(2, _ts)) \
                + pb_v(18, shooter.hp) + pb_b(22, pb_v(1, shooter.x) + pb_v(2, shooter.y))
            seq_box[0] += 1
            battle.room.broadcast(battle_notify(seq_box[0], 9, pb_b(1, _e) + pb_v(2, 0)))
    # 回合推进
    nxt = battle.next_turn()
    if nxt is None:
        seq_box[0] += 1
        battle.room.broadcast(battle_notify(seq_box[0], 4, b""))
        battle.room.state = "settle"
        return
    nxt_st = get_combat(battle, nxt)
    nxt_st.turn_buffs()
    _regen_hp = regen_tick(nxt_st, getattr(nxt, "maxhp", nxt.hp), nxt.hp)
    if _regen_hp != nxt.hp:
        raw_log("REGEN", b"", f"uid={nxt.uid} hp={nxt.hp}->{_regen_hp}")
        nxt.hp = _regen_hp
    if nxt_st.frozen_turns > 0:  # 北境纹章: 冻结跳过本回合
        nxt_st.frozen_turns -= 1
        raw_log("FROZEN_SKIP", b"", f"uid={nxt.uid}")
        return handle_pass(battle, nxt, seq_box)
    seq_box[0] += 1
    wind_ui = random.randint(-80, 80) / 10.0
    battle.room.wind = wind_ui
    order = [nxt.uid] + [p.uid for p in battle.room.players if p is not nxt][:2] + [0]
    wind_raw = make_wind_raw(wind_ui, battle.turn, order)
    act = build_action(battle.turn, order, 100, wind_raw)
    battle.room.broadcast(battle_notify(seq_box[0], 8, act))
    if nxt.ws is None:  # 机器人回合:延迟后自动开炮
        try:
            asyncio.get_running_loop().create_task(bot_fire(battle, nxt, seq_box))
        except Exception:
            pass


def handle_pass(battle: Battle, passer: Player, seq_box: list):
    """(2026-10-18 §55.21) PassReq 过回合:同 handle_fire 尾部回合推进(无射击/伤害)。bot 回合自动开炮。"""
    raw_log("PASS_TURN", b"", f"uid={passer.uid} turn={battle.turn}")
    nxt = battle.next_turn()
    if nxt is None:
        seq_box[0] += 1
        battle.room.broadcast(battle_notify(seq_box[0], 4, b""))
        battle.room.state = "settle"
        return
    seq_box[0] += 1
    wind_ui = random.randint(-80, 80) / 10.0
    battle.room.wind = wind_ui
    order = [nxt.uid] + [p.uid for p in battle.room.players if p is not nxt][:2] + [0]
    wind_raw = make_wind_raw(wind_ui, battle.turn, order)
    act = build_action(battle.turn, order, 100, wind_raw)
    battle.room.broadcast(battle_notify(seq_box[0], 8, act))
    if nxt.ws is None:
        try:
            asyncio.get_running_loop().create_task(bot_fire(battle, nxt, seq_box))
        except Exception:
            pass


async def bot_fire(battle: Battle, bot: Player, seq_box: list):
    await asyncio.sleep(3.0)
    if battle.room.state != "playing" or bot.hp <= 0:
        return
    import math
    targets = [p for p in battle.room.players if p is not bot and p.hp > 0]
    if not targets:
        return
    t = targets[0]
    dx = t.x - bot.x
    dist = math.hypot(dx, t.y - bot.y) / 82.5  # 距(1距=82.5px)
    # (2026-10-18 §55.20) bot 瞄准接 solve_target(真机模型精确反解): 高抛角 55°(左向 125°),含风与高差。
    theta = 55.0 if dx >= 0 else 125.0
    height_ju = (bot.y - t.y) / 82.5  # 目标高于射手为正(solver 约定)
    dist_signed = dist if dx >= 0 else -dist
    try:
        f = solve_target(theta, dist_signed, height_ju, battle.room.wind)
        force = max(5.0, min(100.0, f if f is not None else 60.0))
    except Exception:
        force = min(100.0, 35.0 + dist * 6.0)  # 兜底粗略
    # (§55.47) bot 上行序列=真客户端形态: HoldReq{f1:shotId}(蓄力) → FireReq{f1:shotId,f2:槽0,
    # f3:角度,f4:f64力度,f5:16B射击UUID}(FireReq 完整解码实证 fire_req_samples.txt)。
    import uuid as _uuid
    shot_id = battle.turn
    _uid16 = _uuid.uuid4().bytes
    raw_log("BOT_HOLD", pb_v(1, shot_id), f"uid={bot.uid}")
    proto = (pb_v(1, shot_id) + pb_v(2, 0) + pb_v(3, int(theta))
             + pb_f64(4, force) + pb_b(5, _uid16))
    handle_fire(battle, bot, proto, seq_box)


# ---------- 主循环 ----------

def try_start_battle(room, player, seq):
    """开战序列(StartReq 或房主点按钮=SetReadyReq 都走这里):
    StartAck + type0 Begin + type8 Action(风)。真实上行实录 battle2_up:StartReq{f1=0}。"""
    # 真实序列(battle5): RoomNotify{f10=30}(匹配倒计时) → StartAck{} → Begin → RoomNotify{f10=0}
    room.broadcast(pack_message("RoomNotify", pb_v(10, 30)))
    # 真实 StartAck 为空载荷(battle5 pkt=5 "proto":""),带 f1=1 会阻止客户端进战斗
    send(player, "StartAck", b"")
    if len(room.players) < 2:
        room.players.append(make_bot())  # 单人补机器人,覆盖所有入口
    raw_log("TRY_START", b"", f"players={len(room.players)} state={room.state}")
    if len(room.players) >= 2 and room.state == "lobby":
        room.state = "playing"
        room.act_sent = False  # §55.48 复位: 跨战斗残留 True 会让第二场卡 VS 屏(实证 run142 battle2)
        battle = Battle(room)
        battle.terrain = Terrain(int(os.environ.get("TNT_MAP_ID", "229")))  # §55.44 可变形地形
        BATTLES[room.rid] = battle
        for p in room.players:
            # 真实(tnt_capture2): f3=Left=对手, f4=Right=自己(72143)
            p.team = 0 if p.uid == BOT_UID else 1
            if p.team == 0:
                room.left_uid = p.uid  # §55.25 结算 LeftWin 依据
            else:
                room.self_uid = p.uid  # §55.25 f1=Win(我方胜)依据
            p.hp = 2290  # §55.26 HP=Begin f6 量纲(血条=f18/f6);我方 f6=2290
            # (2026-10-18 §55.26) bot hp=30(=其 f6) 一发定胜测试参;可回调 1870。
            if p.uid == BOT_UID:
                p.hp = 20 if os.environ.get("TNT_ONESHOT") else 1870  # §55.41 调参回调: 默认真实1870
            p.maxhp = p.hp  # §55.44 战斗模型量纲(奥杜因之牙/回血/护盾用)
            # (2026-10-18 §55.21) 战斗坐标与 build_init f8 一致(bot 1539,320 / 我 585,325)。
            # Player 默认 (1211,836) 与 Begin 渲染位不符→bot 瞄错点永不命中(run90 实证18回合不掉血)。
            if p.uid == BOT_UID:
                p.x, p.y = 1539, 320
            else:
                p.x, p.y = 585, 325
        # §55.50 出生平台挂地形: 角色脚下悬浮台面(弱射弹落台面=可见弹坑测试点)
        for p in room.players:
            battle.terrain.add_platform(p.x, p.y + 25, 210)
        # 真实序列(tnt_capture2 pkt=50/52/54/56/58, 毫秒级突发): RoomNotify{f10=30} → StartAck
        #   → Begin → RoomNotify{座位f7×3} → RoomNotify{f10=0} —— 全部无延迟连发(有延迟客户端超时回退)
        async def delayed_battle():
            seq[0] += 1
            init = battle_notify(seq[0], 0, build_init(room.players))
            raw_log("SEND BattleNotify#Begin", init, f"len={len(init)}")
            room.broadcast(init)
            seat4 = pb_b(6, pb_v(1, 0) + pb_v(2, 4) + pb_v(3, player.uid)) + pb_v(7, 0) + pb_v(7, 0) + pb_v(7, 0)
            room.broadcast(pack_message("RoomNotify", seat4))
            room.broadcast(pack_message("RoomNotify", pb_v(10, 0)))
            # 真实: type8 Action 在双方加载100%后才发(LoadingReq 到100时补发,见 LoadingReq 分支)
        try:
            asyncio.get_running_loop().create_task(delayed_battle())
        except Exception:
            pass
        return True
    return False


async def handle(ws):
    player = None
    room = None
    seq = [0]
    raw_log("CONNECT")
    try:
        async for raw in ws:
            try:
                name, proto = unpack_message(raw)
            except Exception as e:
                print("[unpack fail]", len(raw), e)
                raw_log("UNPACK_FAIL", raw if isinstance(raw, bytes) else b"")
                continue
            print("[recv]", name, len(proto))
            raw_log("RECV", proto, name)
            if name == "SkillReq" and player:
                # §55.47 战斗道具槽(48%/+2/×3): **真录实证**(run143) SkillReq{f1:Round=9, f2:Target=0,
                # f3:Index=1(槽位), f4:Speed=50(蓄力值)} —— 道具在**蓄力过程中**使用(前置点击无效!)。
                # → SkillAck{f1:1,f2:idx};效果挂下一发: 48%=伤害×1.48 / +2=角度+2 / ×3=伤害×1.2。
                f = dec_pb(proto)
                idx = int(f.get(3, [0])[0])
                raw_log("SKILL_USE", proto, f"uid={player.uid} idx={idx} round={f.get(1)} target={f.get(2)}")
                send(player, "SkillAck", pb_v(1, 1) + pb_v(2, idx))
                for _b in BATTLES.values():
                    for _p in _b.room.players:
                        if _p is player:
                            if not hasattr(_b, "skill_buff"):
                                _b.skill_buff = {}
                            _b.skill_buff[player.uid] = idx
                continue
            if name == "CheckinReq" and player:
                # (§55.46) 签到: CheckinReq → CheckinAck(真录无样本,schema 无此族——试验字段)。
                # 面板「当日完成8次战斗(0/8)」计数疑由本 ack 下发;f1=当日战斗数,f2=目标8。
                import datetime as _dt
                _today = _dt.date.today().isoformat()
                if DAILY_BATTLES.get("date") != _today:
                    DAILY_BATTLES["date"] = _today
                    DAILY_BATTLES["n"] = 0
                n = DAILY_BATTLES.get("n", 0)
                raw_log("CHECKIN_ACK", b"", f"n={n}")
                send(player, "CheckinAck", pb_v(1, n) + pb_v(2, 8) + pb_v(3, 1))
                continue
            if name == "BuyReq" and player:
                handle_buy(player, proto)
                continue
            if name == "ReadMailReq" and player:
                handle_read_mail(player, proto)
                continue
            if name == "DelMailReq" and player:
                send(player, "DelMailAck", pb_s(1, ""))
                continue
            if name == "ChatReq" and player:
                try:
                    txt = dec_pb(proto).get(1, [b""])[0]
                    if isinstance(txt, bytes):
                        txt = txt.decode("utf-8", "replace")
                except Exception:
                    txt = ""
                for p in ACCOUNTS.values():
                    if p.ws:
                        send(p, "ChatNotify", pb_s(1, player.name) + pb_s(2, txt))
                continue
            if name == "EmotionReq" and player:
                # 表情包: EmotionReq{Index} → EmotionAck{Msg=1}
                # 关键: 真实服会把表情广播给房间内所有人(RoomNotify f11=Emotion{Pos,Id,Index}),
                # 私服若不回环,本地发出去的表情永远不显示(实测: 面板关闭但画面上无任何表情)。
                try:
                    _idx = int(dec_pb(proto).get(1, [0])[0])
                except Exception:
                    _idx = 0
                _pos = 0
                try:
                    if room:
                        for _i, _p in enumerate(room.players):
                            if _p is player:
                                _pos = _i
                                break
                except Exception:
                    _pos = 0
                send(player, "EmotionAck", pb_v(1, 1))
                _em = pb_b(11, pb_v(1, _pos) + pb_v(2, player.uid) + pb_v(3, _idx))
                # 先回环给自己(可靠), 再广播给房间其他人(room.players 有时不含自己 → 之前广播丢包)
                send(player, "RoomNotify", _em)
                try:
                    if room:
                        room.broadcast(pack_message("RoomNotify", _em), skip=player)
                except Exception as e:
                    print("[emotion broadcast fail]", e)
                # 面板打开时客户端不渲染表情 → 面板关闭后再补发两次(实测可见)
                async def _replay_em(delays, msg, ws):
                    for _d in delays:
                        await asyncio.sleep(_d)
                        try:
                            if ws:
                                await ws.send(msg)
                        except Exception:
                            pass
                try:
                    asyncio.get_running_loop().create_task(
                        _replay_em((1.0, 2.2, 3.4, 4.6, 5.8, 7.0), pack_message("RoomNotify", _em), player.ws))
                except Exception:
                    pass
                raw_log("EMOTION", proto, f"idx={_idx} pos={_pos} uid={player.uid}")
                continue
            # ---- 模块语义(2026-10-18 proto_schema.txt 二进制字段名定案) ----
            # 跨名配对缺口: schema 无 DrawAck/ConvertAck,AUTOACK 会发不存在的消息名。
            if name == "DrawReq" and player:
                # 幸运星/签到抽奖: DrawReq{Pos=1} → RollAck{Ok,Msg,Pos}(Pos=抽中位) + GainNotify 奖励弹窗
                # 奖励表(§48.5 签到 7 天: 勋章=2002 勇气徽章,点券=2003)
                try:
                    pos = dec_pb(proto).get(1, [0])[0]
                except Exception:
                    pos = 1
                table = {1: (2002, 10), 2: (2002, 20), 3: (2003, 300), 4: (2002, 35),
                         5: (2003, 300), 6: (2002, 60), 7: (2003, 400)}
                rid, rcnt = table.get(pos if 1 <= pos <= 7 else 1, (2002, 10))
                send(player, "RollAck", pb_v(1, 1) + pb_s(2, "恭喜获得奖励!") + pb_v(3, pos))
                gain = pb_s(1, "签到奖励") + pb_b(2, pb_v(1, rid) + pb_v(2, rcnt))
                send(player, "GainNotify", gain)
                raw_log("DRAW", proto, f"pos={pos} reward={rid}x{rcnt}")
                continue
            if name == "RollVipReq" and player:
                # 达人水晶转盘(2026-10-18 真机截获,达人模块): RollVipReq{} → 回包名两可(RollAck 族
                # 命名非对称: DrawReq→RollAck 先例)。先发 RollAck{Ok,Msg,Pos};Pos=奖品格下标(客户端本地表)。
                # 之前无处理器 → 客户端等回包转盘弹窗卡死(实证 mumu_screen_1020_now)。
                pos = 5
                send(player, "RollAck", pb_v(1, 1) + pb_s(2, "恭喜获得奖励!") + pb_v(3, pos))
                send(player, "GainNotify", pb_s(1, "达人水晶转盘") + pb_b(2, pb_v(1, 1011) + pb_v(2, 50)))
                raw_log("ROLLVIP", proto, f"pos={pos} as RollAck")
                continue
            if name == "ConvertReq" and player:
                # 兑换: ConvertReq{Id=1} → ConfirmAck{Ok=1}(schema 无 ConvertAck)
                send(player, "ConfirmAck", pb_v(1, 1))
                raw_log("CONVERT", proto, "")
                continue
            if name == "EnhanceReq" and player:
                # 芯片强化: EnhanceReq{Id,Promote,Protect,Crystal} → EnhanceAck{Ok,Msg,Chip=强化后等级}
                send(player, "EnhanceAck", pb_v(1, 1) + pb_s(2, "强化成功") + pb_v(3, 1))
                raw_log("ENHANCE", proto, "")
                continue
            if name == "UseReq" and player:
                # UseReq{Id,Notify} → UseAck{Msg=1,CloseBag=2,KeepMenu=3,Gain=4}
                # 真实样本: battle6 UseAck{f1="恭喜你39级使用了你的奖励!"}; sys1 UseAck{f2=1(close_bag)}
                send(player, "UseAck", pb_s(1, "使用成功") + pb_v(2, 1))
                raw_log("USE", proto, "")
                try:
                    _uid_item = dec_pb(proto).get(1, [0])[0]
                except Exception:
                    _uid_item = 0
                if _uid_item == 2185:
                    # §55.51 签到领取(UseReq{f1:2185}): 补推 CheckinAck 刷新面板计数
                    _n = DAILY_BATTLES.get("n", 0)
                    send(player, "CheckinAck", pb_v(1, _n) + pb_v(2, 8) + pb_v(3, 1))
                    raw_log("CHECKIN_CLAIM_ACK", b"", f"uid={player.uid} daily={_n}")
                continue
            if name == "FuseReq" and player:
                # (2026-10-18 §55.15 真机实证) 宝物合成: FuseReq{1:宝物id} → FuseAck{Ok} + GainNotify 产物
                # id 规律(f8 数据定案): 同型宝物连续 id 按等级递增(盾1063/1064/1065=Lv1/2/3,计数15/5/1 与背包吻合)
                # 合成: 4×LvN(id) → 1×LvN+1(id+1)
                try:
                    fid = dec_pb(proto).get(1, [0])[0]
                except Exception:
                    fid = 0
                bag = ITEM_LEDGER.setdefault(player.uid, {})
                have = bag.get(fid, 0)
                ok = 1 if have >= 4 else 0
                if ok:
                    bag[fid] = have - 4
                    bag[fid + 1] = bag.get(fid + 1, 0) + 1
                send(player, "FuseAck", pb_v(1, ok) + pb_s(2, "合成成功" if ok else "材料不足"))
                if ok:
                    send(player, "GainNotify", pb_s(1, "宝物合成") + pb_b(2, pb_v(1, fid + 1) + pb_v(2, 1)))
                    pd = pb_v(1, player.uid)
                    pd += pb_b(8, pb_v(1, fid) + pb_v(2, bag[fid]))
                    pd += pb_b(8, pb_v(1, fid + 1) + pb_v(2, bag[fid + 1]))
                    send(player, "PlayerDataNotify", pd)
                raw_log("FUSE", proto, f"fid={fid} have={have} ok={ok}")
                continue
            if name.startswith("Fire") and player and room and room.state == "playing" and room.rid in BATTLES:
                # 合成战斗走模拟;TNT_REPLAY_REAL 重放模式 BATTLES 无对象,落到下方 FIREREQ_LIVE 处理器
                handle_fire(BATTLES[room.rid], player, proto, seq)
                continue
            if name == "PassReq" and player and room and room.state == "playing" and room.rid in BATTLES:
                # (2026-10-18 §55.21) 合成战斗过回合→推进回合+bot 自动开炮(此前 PassReq 无路由卡回合)
                handle_pass(BATTLES[room.rid], player, seq)
                continue
            if name in ("CreateRoomReq", "JoinInReq") and player:
                room = handle_create_room(player, proto, [], seq)
                continue
            if name == "ExitRoomReq" and player:
                # 客户端退出房间等待 ExitRoomAck,否则卡转圈(2026-10-17 实测)
                if room:
                    room.players = [p for p in room.players if p is not player]
                # §55.51 空 Ack 不转屏(实证 navB/C 停房间): schema ExitRoomAck{1:Msg} 补消息字段
                send(player, "ExitRoomAck", pb_s(1, "已退出房间"))
                # §55.56 仅 Msg 仍不转屏(exitX1/2)——真录 RoomNotify=0801{1:1}(login8 pkt41)退房后
                # 补房间列表刷新促客户端退出房间屏→大厅。
                send(player, "RoomNotify", pb_v(1, 1))
                raw_log("EXIT_ROOM", b"", "ack+RoomNotify{1:1}")
                continue

            if name == "SetReadyReq" and player:
                send(player, "SetReadyAck", pb_v(1, 1))
                if room is None:
                    # 重连后客户端保留房间UI,room 已丢:自动重建(2026-10-17 实测)
                    room = handle_create_room(player, pb_v(1, 0), [])
                if room:
                    # 增量 delta(真实服模式 battle6_events):单座位更新才刷新徽章/按钮
                    # 我=State4(房主),机器人=State3(准备) → mumu_room2 真实=开始游戏按钮
                    d = pb_b(6, pb_v(1, 0) + pb_v(2, 4) + pb_v(3, player.uid))
                    d += pb_b(6, pb_v(1, 1) + pb_v(2, 3) + pb_v(3, BOT_UID))
                    room.broadcast(pack_message("RoomNotify", d))
                    # 随机匹配/自由开局: 准备点击即开始(客户端按钮状态错位,0/1 都触发)
                    try:
                        if room.state == "lobby":
                            if os.environ.get("TNT_REPLAY_REAL"):
                                # 判别实验: 只放 tnt_capture2 pkt=50/52/54/56/58 五连帧;
                                # type3 加载在 LoadingReq 里按进度回(见 LoadingReq 分支),开局帧在 100% 时发
                                room.state = "playing"
                                evfile = (ROOT / "data" / "tnt_capture2_events.jsonl")
                                G_REPLAY["frames"] = {}
                                G_REPLAY["turns"] = None
                                G_REPLAY["settled"] = False
                                G_REPLAY["trig"] = 0
                                for line in evfile.read_text(encoding="utf-8").splitlines():
                                    try:
                                        ev = json.loads(line)
                                    except Exception:
                                        continue
                                    if ev.get("direction") == "down" and 50 <= ev.get("pkt", 0) <= 135:
                                        G_REPLAY["frames"][ev["pkt"]] = (ev["name"], bytes.fromhex(ev["proto"]))
                                sent = 0
                                for pk in (51, 53, 55, 57, 59):  # events 编号(=pcap序号+1)
                                    nm, pr = G_REPLAY["frames"].get(pk, (None, None))
                                    if nm:
                                        room.broadcast(pack_message(nm, pr))
                                        sent += 1
                                raw_log("REPLAY_REAL", b"", f"burst5={sent} (type3按LoadingReq进度回)")
                                continue
                            try_start_battle(room, player, seq)
                    except Exception:
                        pass
                    # 按钮状态机(2026-10-17 修复): owner 准备(SetReadyReq{1})→全员就绪→按钮变「开始游戏」→客户端发 StartReq 走 try_start_battle
                continue
            if name == "StartReq" and player:
                # 快速加入:分配房间;≥2人即开局(真实上行 StartReq{f1=0},battle2_up)
                if os.environ.get("TNT_REPLAY_REAL"):
                    if room is None:
                        room = next((r for r in ROOMS.values() if r.state == "lobby" and len(r.players) < 6), None)
                    if room:
                        try:
                            # 与 SetReadyReq 分支相同的重放逻辑: burst5 + type3按进度 + 开局帧
                            room.state = "playing"
                            evfile = (ROOT / "data" / "tnt_capture2_events.jsonl")
                            G_REPLAY["frames"] = {}
                            for line in evfile.read_text(encoding="utf-8").splitlines():
                                try:
                                    ev = json.loads(line)
                                except Exception:
                                    continue
                                if ev.get("direction") == "down" and 50 <= ev.get("pkt", 0) <= 135:
                                    G_REPLAY["frames"][ev["pkt"]] = (ev["name"], bytes.fromhex(ev["proto"]))
                            sent = 0
                            for pk in (51, 53, 55, 57, 59):
                                nm, pr = G_REPLAY["frames"].get(pk, (None, None))
                                if nm:
                                    room.broadcast(pack_message(nm, pr))
                                    sent += 1
                            raw_log("REPLAY_REAL", b"", f"burst5={sent} via StartReq")
                        except Exception:
                            pass
                    continue
                if room is None:
                    room = next((r for r in ROOMS.values() if r.state == "lobby" and len(r.players) < 6), None)
                    if room is None:
                        room = Room(NEXT_ROOM[0]); NEXT_ROOM[0] += 1
                        ROOMS[room.rid] = room
                # (§55.48) 房间开局断点修复: ExitRoomReq 后 room 引用残留但玩家已被移出——
                # StartReq 时若玩家不在 players 里必须重新入列,否则 try_start 双 bot 无自己,
                # Begin 载荷畸形(实证 run141 Begin#1 f3/f3 两 61001)客户端忽略不进战斗。
                if player not in room.players:
                    room.players.append(player)
                try_start_battle(room, player, seq)
                continue
            if name == "LoadingReq" and player:
                # 真实(type3/f6, tnt_capture2 pkt=60-114): 服务端把每方加载进度广播给全场(11→100)
                prog = dec_pb(proto).get(1, [0])[0]
                if os.environ.get("TNT_REPLAY_REAL"):
                    # 判别: type3 不在 LoadingReq 风暴期发(可能触发 onOpen 竞争),统一延迟到 100% 后
                    ps = G_REPLAY.setdefault("t3", [])
                    if not ps:
                        for line in (ROOT / "data" / "tnt_capture2_events.jsonl").read_text(encoding="utf-8").splitlines():
                            try:
                                ev = json.loads(line)
                            except Exception:
                                continue
                            if ev.get("direction") == "down" and ev.get("name") == "BattleNotify" and "1003" in ev.get("proto", "")[:8]:
                                ps.append((ev["pkt"], bytes.fromhex(ev["proto"])))
                        ps.sort()
                    if prog >= 100:
                        async def _start_later():
                            await asyncio.sleep(2)
                            # 真实 type3 原样逐条慢发(自身 uid 已修复,验证 verbatim 全链)
                            for _ in range(20):
                                if ps:
                                    pk3, pr3 = ps.pop(0)
                                    raw_log("T3", pr3, "pkt%d" % pk3)
                                    room.broadcast(pack_message("BattleNotify", pr3))
                                    await asyncio.sleep(0.4)
                            await asyncio.sleep(2)
                            for pk in (118, 120, 121, 124):
                                nm, pr = G_REPLAY["frames"].get(pk, (None, None))
                                if nm:
                                    room.broadcast(pack_message(nm, pr))
                            raw_log("REPLAY_START", b"", "self-uid fixed; verbatim t3+frames")
                        asyncio.ensure_future(_start_later())
                    continue
                seq[0] += 1
                room.broadcast(battle_notify(seq[0], 3, pb_v(1, player.uid) + pb_v(2, prog)))
                seq[0] += 1
                room.broadcast(battle_notify(seq[0], 3, pb_v(1, BOT_UID) + pb_v(2, prog)))
                # §55.43 加固: 加载到 100% 即视为战斗加载完成(不依赖 state 时序——房间开局路径曾卡 VS 屏)
                if prog >= 100 and not getattr(room, "act_sent", False):
                    raw_log("LOAD100", b"", f"state={room.state} players={len(room.players)} rid={room.rid}")
                    room.act_sent = True
                    # 真实加载完成后: type4(空) → type8 Action(回合1) [tnt_capture2 pkt=118/124]
                    room.act_sent = True
                    seq[0] += 1
                    room.broadcast(battle_notify(seq[0], 4, b""))
                    wind_ui = random.randint(-80, 80) / 10.0
                    room.wind = wind_ui
                    first = room.players[0]
                    order = [first.uid, room.players[1].uid, 0]
                    wind_raw = make_wind_raw(wind_ui, 1, order)
                    seq[0] += 1
                    act_msg = battle_notify(seq[0], 8, build_action(1, order, 100, wind_raw))
                    raw_log("SEND BattleNotify#Action", act_msg, f"len={len(act_msg)}")
                    room.broadcast(act_msg)
                continue
            if name in ("PassReq", "FireReq", "HoldReq", "FloatReq", "SyncReq") and player:
                # 战斗上行实况(2026-10-17): HoldReq→FireReq(f1 shotId,f3 角度,f4 f64 力度,f5 16B UUID)、
                # PassReq 空包过回合、SyncReq{f1=回合号}、FloatReq{f1,f2} 位置同步
                if name == "FireReq":
                    try:
                        raw_log("FIREREQ_LIVE", proto, str(fire_decode(proto)))
                    except Exception as e:
                        raw_log("FIREREQ_LIVE", proto, "decode-err %r" % e)
                else:
                    raw_log(name.upper() + "_LIVE", proto, "")
                if os.environ.get("TNT_REPLAY_REAL") and room is not None and name in ("PassReq", "FireReq", "SyncReq"):
                    # 回合推进: 战斗动作/回合同步后回放下一组真实回合帧(以 type13 换回合帧为组界)
                    groups = G_REPLAY.get("turns")
                    if groups is None:
                        groups = []
                        cur = []
                        evfile = (ROOT / "data" / "tnt_capture2_events.jsonl")
                        for line in evfile.read_text(encoding="utf-8").splitlines():
                            try:
                                ev = json.loads(line)
                            except Exception:
                                continue
                            if ev.get("direction") != "down" or ev.get("pkt", 0) <= 124:
                                continue
                            if ev.get("name") not in ("BattleNotify", "ChatNotify"):
                                continue
                            pr = bytes.fromhex(ev.get("proto", ""))
                            if cur and ev.get("name") == "BattleNotify" and any(nm == "BattleNotify" for _, nm, _ in cur):
                                try:
                                    f2v = dec_pb(pr).get(2, [0])[0] if pr else 0
                                except Exception:
                                    f2v = 0
                                if f2v == 13:
                                    groups.append(cur)
                                    cur = []
                            cur.append((ev["pkt"], ev["name"], pr))
                        if cur:
                            groups.append(cur)
                        G_REPLAY["turns"] = groups
                        raw_log("TURN_GROUPS", b"", "count=%d sizes=%s" % (len(groups), [len(g) for g in groups]))
                    # 回合驱动(2026-10-18 死锁修复): 组尾常是敌方行动帧(type13 换回合至敌方),
                    # 客户端等待敌方动作不再发上游触发——只靠上游触发弹组会死锁(run69 实测卡第3回合)。
                    # 看门狗: 组播完 8s 无新触发 → 自动播下一组;上游触发则立即播并作废旧看门狗。
                    async def _settle():
                        if G_REPLAY.get("settled"):
                            return
                        G_REPLAY["settled"] = True
                        uids = [p.uid for p in room.players] or [player.uid]
                        me = player.uid
                        items = b""
                        for u in uids:
                            win_side = (u == me)
                            items += pb_b(3, pb_v(1, u) + pb_v(2, 3) + pb_v(3, 3) + pb_v(4, 1200 if win_side else 800)
                                          + pb_v(5, 1 if win_side else 0) + pb_v(6, 0 if win_side else 1)
                                          + pb_v(7, 1 if win_side else 0))
                        result = pb_v(1, 1) + pb_v(2, 1) + items  # Win=1 我方胜, LeftWin=1
                        seq[0] += 1
                        room.broadcast(pack_message("BattleNotify", pb_v(1, seq[0]) + pb_v(2, 2) + pb_b(5, result)))
                        await asyncio.sleep(2.5)
                        seq[0] += 1
                        room.broadcast(pack_message("BattleNotify", pb_v(1, seq[0]) + pb_v(2, 6)))
                        await asyncio.sleep(2.5)
                        for u in uids[:2]:
                            seq[0] += 1
                            if u == me:
                                card = pb_v(1, u) + pb_b(2, pb_v(1, 2000) + pb_v(2, 972) + pb_v(3, 3))
                            else:
                                card = pb_v(1, u) + pb_v(3, 1)
                            room.broadcast(pack_message("BattleNotify", pb_v(1, seq[0]) + pb_v(2, 7) + pb_b(7, card)))
                            await asyncio.sleep(2.5)
                        # 收尾帧延迟 10s: 让结算/翻牌 UI 可见可截(2026-10-18 0.6s 节奏一闪而过)
                        await asyncio.sleep(10)
                        seq[0] += 1
                        room.broadcast(pack_message("BattleNotify", pb_v(1, seq[0]) + pb_v(2, 5)))
                        room.state = "lobby"
                        raw_log("SETTLE", b"", "uids=%s" % uids)

                    def _play_next():
                        if not groups:
                            raw_log("TURN_END", b"", "no more real turn groups -> settlement")
                            asyncio.ensure_future(_settle())
                            return
                        g = groups.pop(0)
                        G_REPLAY["trig"] = G_REPLAY.get("trig", 0) + 1
                        my = G_REPLAY["trig"]

                        async def _turn_later(gg, my):
                            for pk, nm, pr in gg:
                                raw_log("TURN", pr, "pkt%d" % pk)
                                room.broadcast(pack_message(nm, pr))
                                await asyncio.sleep(0.4)
                            await asyncio.sleep(8)
                            if G_REPLAY.get("trig") == my:
                                raw_log("TURN_AUTO", b"", "watchdog advance")
                                _play_next()
                        asyncio.ensure_future(_turn_later(g, my))

                    _play_next()
                continue
            if name == "RollStarReq" and player:
                # §55.58 幸运星抽奖: 点「开始START」→RollStarReq(无参)→卡转圈等 RollStarAck。
                # Schema 无 RollStarAck;仿 RollAck{1:Ok,2:Msg,3:Pos}。真档 f8 2034:18=幸运星星数
                # (面板吻合)→抽奖消耗1星→补 PDP 增量{2034:17}解锁转圈+奖品 2001×1。
                send(player, "RollStarAck", pb_v(1, 1) + pb_s(2, "获得幸运星奖励") + pb_v(3, 5))
                _delta = pb_v(1, player.uid) + pb_b(8, pb_v(1, 2034) + pb_v(2, 17)) \
                    + pb_b(8, pb_v(1, 2001) + pb_v(2, 1))
                send(player, "PlayerDataNotify", _delta)
                raw_log("ROLL_STAR", b"", "ack pos=5 + star17 + item2001")
                continue
            if name == "ReadProgressReq" and player:
                # §55.56 签到计数源终极实验: 抽奖链(ReadProgress→Draw→Roll→Checkin)首个请求
                # ReadProgressReq{f1:Id=1}——疑=每日进度(战斗次数)读取。Ack schema{1:Ok,2:Msg}不全,
                # 试 Msg=消息形态 {1:当日战斗数, 2:8} 供面板(0/8)渲染。
                _n = DAILY_BATTLES.get("n", 0)
                send(player, "ReadProgressAck", pb_v(1, 1) + pb_b(2, pb_v(1, _n) + pb_v(2, 8)))
                raw_log("READ_PROGRESS", proto, f"n={_n}")
                continue
            if name.endswith("Req") and player:
                # 兜底:未实现的 Req 一律回 {Name}Ack{Ok=1},防止客户端 UI 卡转圈(UseReq/签到本 实测)
                ack = name[:-3] + "Ack"
                send(player, ack, pb_v(1, 1))
                raw_log("AUTOACK", proto, ack)
                continue
            if not player:
                # 真实 LoginReq(2026-10-17 实测): f1=版本号(260906), f2=账号, f3=密码(明文)
                if name == "LoginReq":
                    try:
                        lf = dec_pb(proto)
                        f1 = lf.get(1, [b""])[0]
                        f2 = lf.get(2, [b""])[0]
                        f3 = lf.get(3, [b""])[0]
                        # v261016 真机 LoginReq 实测: f1=账号, f2=密码(明文),
                        # 旧抓包模板才是 f1=版本号/f2=账号/f3=密码。
                        if isinstance(f1, bytes) and isinstance(f2, bytes) and not f3:
                            version, account, password = 0, f1.decode("utf-8", "replace"), f2.decode("utf-8", "replace")
                        else:
                            version = f1 if isinstance(f1, int) else 0
                            account = f2.decode("utf-8", "replace") if isinstance(f2, bytes) else ""
                            password = f3.decode("utf-8", "replace") if isinstance(f3, bytes) else ""
                    except Exception:
                        version, account, password = 0, "", ""
                    raw_log("LOGINREQ", proto, f"ver={version} acc={account}")
                    acc = ACCOUNTS.get(account)
                    if acc is not None and acc.password == password:
                        player = acc
                    else:
                        raw_log("LOGIN_REJECT", proto, f"acc={account}")
                        asyncio.get_running_loop().create_task(ws.send(pack_message("LoginAck", pb_v(1, 0))))
                        continue
                else:
                    # 兼容旧路径:任何首条消息都接受(格式见 §44)
                    uid_match = [u for u in ACCOUNTS if u.encode() in (proto or b"")]
                    player = ACCOUNTS[uid_match[0] if uid_match else "61001"]
                player.ws = ws
                do_login(player)
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        if player:
            player.ws = None
            if room and player in room.players:
                room.players.remove(player)


async def main():
    load_mails_from_chain()
    print(f"[mails] loaded {len(MAILS)} mails from login chain")
    port = int(os.environ.get("TNT_PORT", "8888"))
    async with serve(handle, "0.0.0.0", port, max_size=2**22):
        print("TNT private server listening on 0.0.0.0:%d" % port)
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
