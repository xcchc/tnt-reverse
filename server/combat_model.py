# -*- coding: utf-8 -*-
"""TNT 战斗模型: 伤害公式 + 宝物效果引擎(§48.2 UI明文公式) + 玩家战斗属性。

真录定标(damage_reverse.py, 2026-10-18):
  - 真实直击伤害量级 ~477/发(battle5: 5发2387 vs maxHP1580);范围溅射恒定(1049→60/1050→70/1051→80)。
  - 伤害主要取决于攻击方武器/属性,受害方防御影响小(70 对 Ce2787 与 Ce9719 同值)。
  - f7 飘字 = {1:武器id, 2:{1:..., 2:"失去生命N点"}} 文本服务端下发。
公式: dmg = round(武器基伤 × 宝物增伤族 × 角度/风修正 × 目标减伤族);对齐真录量级(直击数百)。
"""
import math

# ---- 武器基伤(直击;与 weapon_dump id 对应;1049-1051=范围溅射常伤) ----
WEAPON_BASE = {
    124: 420,   # 我方默认武器(72143 +7 机枪, 攻击力221→基伤420 量级)
    117: 380,
    1049: 60, 1050: 70, 1051: 80,   # 真录溅射常伤
}
DEFAULT_WEAPON_BASE = 350

# ---- 宝物效果表 §48.2(UI明文) ----
# {名: (类型, 参数dict)}  类型: dmg_mult/def_mult/proc/aura
TREASURE_TABLE = {
    # 劳尔的狙击镜: 距离增伤 Lv4 每130px+13%(53%起) 上限200%
    "劳尔的狙击镜":   {"kind": "dist_up", "base": 0.53, "step": 0.13, "per_px": 130, "cap": 2.0,
                       "lv_scale": {1: (0.50, 0.12, 150), 2: (0.51, 0.12, 140), 3: (0.52, 0.13, 135), 4: (0.53, 0.13, 130)}},
    # 逆风局制胜宝典: 逆风增伤 每半风力+1/2/3%, 上限+100%
    "逆风局制胜宝典": {"kind": "headwind", "per_half": {1: 0.01, 2: 0.02, 3: 0.03, 4: 0.03}, "cap": 1.0},
    # 北境纹章: 冻结概率 10/15/25%, 冻结1回合
    "北境纹章":       {"kind": "freeze", "prob": {1: 0.10, 2: 0.15, 3: 0.25, 4: 0.25}},
    # 不死鸟之羽: 致死免死+最大生命15%护盾2回合, 每局1次
    "不死鸟之羽":     {"kind": "revive", "shield_pct": 0.15, "turns": 2},
    # 不屈药剂: 受伤降低8%(Lv2)
    "不屈药剂":       {"kind": "taken_down", "red": {1: 0.04, 2: 0.08, 3: 0.12, 4: 0.16}},
    # 阿斯顿芯片: 连中2回合后每回合回血7%最大生命
    "阿斯顿芯片":     {"kind": "regen", "need_hits": 2, "pct": 0.07},
    # 奥杜因之牙: 生命每低于上限1点伤害+1%, 上限+50%
    "奥杜因之牙":     {"kind": "missing_hp", "per_hp": 0.01, "cap": 0.50},
    # 近身战刀: 每100px -6%(越近越高)
    "近身战刀":       {"kind": "near_up", "per_100px": 0.06},
    # 龙牙项链: 满怒攻+10%受伤+2%; 未满怒攻-2%
    "龙牙项链":       {"kind": "rage", "full_atk": 0.10, "full_taken": 0.02, "empty_atk": -0.02},
    # 高抛秘笈残页: 角度>50 每度+1%(截图遮挡,取1%)
    "高抛秘笈残页":   {"kind": "high_arc", "over": 50.0, "per_deg": 0.01},
    # 超级节拍器: 攻-5, 每3回合攻+50(首回合起)
    "超级节拍器":     {"kind": "metronome", "flat": -5, "every": 3, "burst": 50},
    # 暮色之月: 生命<20%吸血20%(上限500)
    "暮色之月":       {"kind": "lifesteal", "hp_below": 0.20, "pct": 0.20, "cap": 500},
    # 伯恩的计数器: 每次击中攻+1%(上限25%), 每次受伤攻+1%(上限95%)
    "伯恩的计数器":   {"kind": "counter", "per_hit": 0.01, "cap_hit": 0.25, "per_taken": 0.01, "cap_taken": 0.95},
}

# 默认宝物配置(实测持有: §48.2 12种 Lv1-Lv4;两号不同等级)
DEFAULT_TREASURES = {
    "劳尔的狙击镜": 4, "逆风局制胜宝典": 3, "北境纹章": 3, "不死鸟之羽": 1, "不屈药剂": 2,
    "阿斯顿芯片": 2, "奥杜因之牙": 2, "近身战刀": 1, "龙牙项链": 2, "高抛秘笈残页": 2,
    "超级节拍器": 1, "暮色之月": 1, "伯恩的计数器": 1,
}


class CombatState:
    """每场战斗每人的宝物运行态。"""

    def __init__(self, player):
        self.uid = player.uid
        self.t = dict(getattr(player, "treasures", None) or DEFAULT_TREASURES)
        self.hit_streak = 0        # 连续命中回合(阿斯顿芯片)
        self.hits_landed = 0       # 伯恩的计数器(命中)
        self.taken_count = 0       # 伯恩的计数器(受伤)
        self.frozen_turns = 0      # 北境纹章
        self.shield_hp = 0         # 不死鸟之羽护盾剩余
        self.shield_turns = 0
        self.revive_used = False
        self.rage = 0              # 0-100(龙牙项链)

    def lv(self, name, default=1):
        return int(self.t.get(name, 0) or 0) if name in self.t else 0

    def has(self, name):
        return name in self.t

    def turn_buffs(self):
        """回合开始: 回血/冻结衰减/护盾衰减。返回事件描述列表。"""
        ev = []
        if self.shield_turns > 0:
            self.shield_turns -= 1
            if self.shield_turns == 0:
                self.shield_hp = 0
                ev.append("shield_expired")
        if self.frozen_turns > 0:
            self.frozen_turns -= 1
            ev.append("freeze_thaw" if self.frozen_turns == 0 else "frozen")
        return ev


def _tre_lv(state, name):
    return int(state.t.get(name, 0))


def compute_damage(att: "CombatState", vic: "CombatState",
                   dist_px: float, angle_deg: float, wind_ui: float,
                   weapon_id: int, atk_missing_hp: float, max_hp_atk: float):
    """伤害公式: 返回 (dmg, logdict)。"""
    base = WEAPON_BASE.get(weapon_id, DEFAULT_WEAPON_BASE)
    log = {"base": base, "mults": {}}
    m = 1.0

    # ---- 攻击方增伤族 ----
    name = "劳尔的狙击镜"
    if att.has(name):
        lv = _tre_lv(att, name)
        b, step, per = TREASURE_TABLE[name]["lv_scale"].get(lv, (0.53, 0.13, 130))
        mult = min(TREASURE_TABLE[name]["cap"], b + step * (dist_px / per))
        log["mults"]["sniper"] = round(mult, 3)
        m *= mult

    name = "逆风局制胜宝典"
    if att.has(name) and wind_ui < 0:
        # 逆风(风阻方向)按半风力增伤
        mult = 1.0 + min(TREASURE_TABLE[name]["cap"],
                         TREASURE_TABLE[name]["per_half"].get(_tre_lv(att, name), 0.01) * abs(wind_ui) / 2.0)
        log["mults"]["headwind"] = round(mult, 3)
        m *= mult

    name = "奥杜因之牙"
    if att.has(name):
        mult = 1.0 + min(TREASURE_TABLE[name]["cap"], TREASURE_TABLE[name]["per_hp"] * atk_missing_hp)
        log["mults"]["dragon_tooth"] = round(mult, 3)
        m *= mult

    name = "近身战刀"
    if att.has(name):
        mult = max(0.1, 1.0 - TREASURE_TABLE[name]["per_100px"] * dist_px / 100.0)
        log["mults"]["melee"] = round(mult, 3)
        m *= mult

    name = "高抛秘笈残页"
    if att.has(name) and angle_deg > TREASURE_TABLE[name]["over"]:
        mult = 1.0 + TREASURE_TABLE[name]["per_deg"] * (angle_deg - TREASURE_TABLE[name]["over"])
        log["mults"]["high_arc"] = round(mult, 3)
        m *= mult

    name = "伯恩的计数器"
    if att.has(name):
        mult = 1.0 + min(TREASURE_TABLE[name]["cap_hit"], TREASURE_TABLE[name]["per_hit"] * att.hits_landed) \
                   + min(TREASURE_TABLE[name]["cap_taken"], TREASURE_TABLE[name]["per_taken"] * att.taken_count) * 0.5
        log["mults"]["counter"] = round(mult, 3)
        m *= mult

    name = "超级节拍器"
    if att.has(name):
        mult = 1.0 + TREASURE_TABLE[name]["flat"] / max(base, 1) \
                   + (TREASURE_TABLE[name]["burst"] / max(base, 1) if att.hits_landed % TREASURE_TABLE[name]["every"] == 0 else 0.0)
        log["mults"]["metronome"] = round(mult, 3)
        m *= mult

    name = "龙牙项链"
    if att.has(name):
        mult = 1.0 + (TREASURE_TABLE[name]["full_atk"] if att.rage >= 100 else TREASURE_TABLE[name]["empty_atk"])
        log["mults"]["rage_neck"] = round(mult, 3)
        m *= mult

    # ---- 目标减伤族 ----
    name = "不屈药剂"
    if vic.has(name):
        mult = 1.0 - TREASURE_TABLE[name]["red"].get(_tre_lv(vic, name), 0.08)
        log["mults"]["unyield"] = round(mult, 3)
        m *= mult

    name = "龙牙项链"
    if vic.has(name) and vic.rage >= 100:
        mult = 1.0 + TREASURE_TABLE[name]["full_taken"]
        log["mults"]["rage_taken"] = round(mult, 3)
        m *= mult

    dmg = int(round(base * m))
    if dmg < 1:
        dmg = 1
    return dmg, log


def apply_hit(att: "CombatState", vic: "CombatState", dmg: int, victim_max_hp: int):
    """命中后的状态结算: 返回 (final_dmg, actual_hp_delta, events)。events 供 UI 文案。"""
    ev = []
    actual = dmg

    # 不屈/护盾吸收
    if vic.shield_hp > 0:
        absorbed = min(vic.shield_hp, actual)
        vic.shield_hp -= absorbed
        actual -= absorbed
        ev.append(f"shield_absorb:{absorbed}")

    # 不死鸟之羽: 致死免死
    if actual >= 1 and not vic.revive_used and vic.has("不死鸟之羽"):
        # 致死判定由调用方传入(victim.hp - actual <= 0);此处先标记能力
        pass

    att.hits_landed += 1
    att.hit_streak += 1
    vic.taken_count += 1

    # 北境纹章冻结
    if att.has("北境纹章"):
        import random as _r
        if _r.random() < TREASURE_TABLE["北境纹章"]["prob"].get(_tre_lv(att, "北境纹章"), 0.10):
            vic.frozen_turns = 1
            ev.append("frozen")

    # 暮色之月吸血(攻击方血量<20%)
    heal = 0
    if att.has("暮色之月"):
        ev.append("lifesteal_check")
        heal = -1  # 由调用方按攻击方当前血判定
    return actual, ev, heal


def maybe_revive(vic: "CombatState", victim_max_hp: int):
    """致死时调用: 不死鸟之羽 → 免死+护盾2回合。返回 (revived, new_hp)。"""
    if vic.revive_used or not vic.has("不死鸟之羽"):
        return False, 0
    vic.revive_used = True
    shield = int(victim_max_hp * TREASURE_TABLE["不死鸟之羽"]["shield_pct"])
    vic.shield_hp = shield
    vic.shield_turns = TREASURE_TABLE["不死鸟之羽"]["turns"]
    return True, shield


def regen_tick(state: "CombatState", max_hp: int, cur_hp: int):
    """阿斯顿芯片: 连中2回合后每回合回血7%。返回新hp。"""
    if state.has("阿斯顿芯片") and state.hit_streak >= TREASURE_TABLE["阿斯顿芯片"]["need_hits"]:
        return min(max_hp, cur_hp + int(max_hp * TREASURE_TABLE["阿斯顿芯片"]["pct"]))
    return cur_hp
