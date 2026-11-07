# -*- coding: utf-8 -*-
"""TNT 私服状态:玩家/房间/战斗。"""
import time

class Player:
    def __init__(self, uid, name, password="", role=""):
        self.uid = uid
        self.name = name
        self.password = password
        self.avatar = role
        self.ws = None
        self.x = 1211
        self.y = 836
        self.hp = 85
        self.team = 0
        self.level = 85
        # 货币: 2002=勇气徽章
        self.currencies = {2002: 7062}
        self.items = {}      # itemId -> expire_ts(0=永久)
        self.weapons = [117, 124]   # 拥有武器id(weapon_dump)
        self.equipped = 124
        self.treasures = {}  # treasureName -> [levels]
        self.chip_progress = 70
        self.daren = 3
        self.title = "<尚未获得称号>"
        self.logged_in = False

class Room:
    def __init__(self, rid):
        self.rid = rid
        self.players = []
        self.mode = "item"      # item/ competitive / mech...
        self.state = "lobby"    # lobby/playing/settle
        self.map_id = 4
        self.wind = 0
        self.turn_idx = 0
        self.turn_order = []
        self.started = 0.0

    def broadcast(self, payload, skip=None):
        import asyncio
        for p in self.players:
            if p is not skip and p.ws:
                try:
                    asyncio.get_running_loop().create_task(p.ws.send(payload))
                except Exception:
                    pass

class Battle:
    """一局战斗状态机(简化:回合制,ACT发风,fire收弹道,命中扣血)。"""
    def __init__(self, room: Room):
        self.room = room
        self.turn = 0
        self.cur = None      # 当前行动 Player
        self.energy = {}     # uid -> energy(每回合+10,type3)
        # §55.44 战斗模型: 宝物运行态 + 真实战绩统计(Result 条目用)
        self.combat = {}     # uid -> combat_model.CombatState
        self.stats = {}      # uid -> {shots, hits, dmg, kill, death}
        # §55.47 小机制: 连续4次未命中→下次行动获得跟踪效果(VS加载 tip 文案实证)
        self.misses = {}     # uid -> 连续未命中数
        self.tracking = {}   # uid -> True(下次必中)

    def next_turn(self):
        alive = [p for p in self.room.players if p.hp > 0]
        if len(alive) <= 1:
            return None
        self.turn += 1
        self.cur = alive[self.turn % len(alive)]
        self.room.wind = __import__("random").randint(-10, 10)  # type8 ACT 每玩家回合独立风
        for uid in self.energy:
            self.energy[uid] = min(100, self.energy.get(uid, 0) + 10)
        return self.cur
