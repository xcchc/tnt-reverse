# -*- coding: utf-8 -*-
"""TNT 弹道模型 —— 服务器权威轨迹（客户端只渲染）。

每帧 @30fps，**先更新位置、再更新速度**：

    v0 = V0K * (force/100)                 px/帧
    vx = v0*cos(theta)                     theta 为拨盘角：0=向右，90=向上
    vy = -v0*sin(theta)                    屏幕坐标 y 向下
    we = WIND_MULT * wind                  风按水平目标速度处理

    每帧：
        x  += vx
        y  += vy
        vx += K * (we - vx)
        vy += K * (0  - vy) + G

首点 = 炮口 = 角色坐标 + MUZZLE*(cos, -sin)，真实 Fire 广播 track 的第一点就是炮口。

顺序很关键：把上面两步对调，本仓库两发样本的残差会从 12.6px 涨到 35.3px。
闭式解（见 track_closed_form）与「先位置后速度」等价，与「先速度后位置」不等价。

常数 = libgame 静态常量（K/G/V0K/WIND_MULT）。残差见 `python tools/ballistics.py`；
用 `python tools/fit_ballistics.py` 可对仓库内样本重新拟合（样本只有两发顺风弹，
风系数欠定，拟合值仅作参考，不要覆盖静态常量）。
"""
from __future__ import annotations

import math

K = 0.0186951412
G = 4.28938354
V0K = 144.787175
WIND_MULT = 1.46643562
MUZZLE = 28.0

PX_PER_JU = 82.5    # 1 个显示距离单位 = 82.5 px
TY_UP = 20.0        # 命中判定中心 = 脚底上方 20 px
GROUND_Y = 900.0    # 地形高度图缺省基线（真实地图按地图表替换）

_Q = 1.0 - K

_WEAPON_DUMP = None


def blast_of(weapon_id: int, default: int = 120) -> int:
    """爆炸半径（px），取 data/weapon_dump.json 的 col7；缺失用 default。"""
    import json
    import os

    global _WEAPON_DUMP
    if _WEAPON_DUMP is None:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "weapon_dump.json")
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data = data.get("weapons") or data.get("rows") or list(data.values())
            _WEAPON_DUMP = {r["id"]: r for r in data}
        except Exception:
            _WEAPON_DUMP = {}
    row = _WEAPON_DUMP.get(weapon_id)
    if row and isinstance(row.get("col7"), (int, float)):
        return int(row["col7"])
    return default


def muzzle(x: float, y: float, theta_deg: float):
    """炮口坐标。"""
    r = math.radians(theta_deg)
    return x + MUZZLE * math.cos(r), y - MUZZLE * math.sin(r)


def velocity(theta_deg: float, force: float):
    """(vx, vy)；force 为 0-100 的力度百分比。"""
    r = math.radians(theta_deg)
    f = max(0.0, min(100.0, force)) / 100.0
    v0 = V0K * f
    return v0 * math.cos(r), -v0 * math.sin(r)


def track(x0: float, y0: float, theta_deg: float, force: float, wind: float,
          n_max: int = 2400, ground_y=GROUND_Y):
    """逐帧积分，返回 [(x, y), ...]；首点是炮口。ground_y 可为常数或 fn(x)->y。"""
    vx, vy = velocity(theta_deg, force)
    mx, my = muzzle(x0, y0, theta_deg)
    pts = [(mx, my)]
    we = WIND_MULT * wind
    for _ in range(1, n_max):
        mx += vx
        my += vy
        vx += K * (we - vx)
        vy += K * (0.0 - vy) + G
        pts.append((mx, my))
        gy = ground_y(mx) if callable(ground_y) else ground_y
        if my > gy or mx < -500 or mx > 3000:
            break
    return pts


def track_closed_form(x0, y0, theta_deg, force, wind, n):
    """第 n 个轨迹点（n=0 为炮口）的闭式解，与 track() 逐字节等价。

        A = (1 - q^n)/K,  B = n - A,  q = 1 - K
        x = x0 + MUZZLE*cos + vx0*A + we*B
        y = y0 - MUZZLE*sin + vy0*A + (G/K)*B
    """
    r = math.radians(theta_deg)
    vx0, vy0 = velocity(theta_deg, force)
    mx, my = muzzle(x0, y0, theta_deg)
    A = (1.0 - _Q ** n) / K
    B = n - A
    we = WIND_MULT * wind
    return (mx + vx0 * A + we * B,
            my + vy0 * A + (G / K) * B)


def first_hit(pts, targets: dict, blast_radius: float, ground_y=GROUND_Y):
    """targets: {uid: (x, y)}。返回 (命中 uid 或 None, 地面落点)。"""
    ground = pts[-1]
    for x, y in pts:
        gy = ground_y(x) if callable(ground_y) else ground_y
        if y >= gy:
            ground = (x, gy)
            break
        for uid, (tx, ty) in targets.items():
            if (x - tx) ** 2 + (y - (ty - TY_UP)) ** 2 <= blast_radius ** 2:
                return uid, (x, y)
    return None, ground


# ---------------------------------------------------------------- 反解力度

def solve_target(theta_deg, dist_ju, height_ju=0.0, wind=0.0):
    """给定角度，求命中目标碰撞中心所需力度（%）；无解返回 None。

    相对坐标：射手为原点，+x 指向射击方向，
    目标 = (dist_ju*82.5, -(height_ju*82.5 + 20))。
    """
    th = math.radians(theta_deg)
    mx = MUZZLE * math.cos(th)
    my = -MUZZLE * math.sin(th)
    tx = dist_ju * PX_PER_JU
    ty = -(height_ju * PX_PER_JU + TY_UP)
    we = WIND_MULT * wind
    cos_t, sin_t = math.cos(th), math.sin(th)

    def y_err(n: float) -> float:
        A = (1.0 - _Q ** n) / K
        B = n - A
        vx0 = V0K * cos_t
        # 由 x 方程解出所需 v0 的系数（力度隐含在 v0 里）
        denom = vx0 * A
        if abs(denom) < 1e-12:
            return float("inf")
        v0 = (tx - mx - we * B) / (A * cos_t)
        y = my + (-v0 * sin_t) * A + (G / K) * B
        return y - ty

    lo, hi = 1.0, 4000.0
    flo, fhi = y_err(lo), y_err(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        fm = y_err(mid)
        if flo * fm <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    n = (lo + hi) / 2.0
    A = (1.0 - _Q ** n) / K
    B = n - A
    denom = V0K * cos_t * A
    if abs(denom) < 1e-9:
        return None
    v0 = (tx - mx - we * B) / denom
    return v0 / V0K * 100.0


def solve_angle_force(x0, y0, x1, y1, wind, theta_lo=20.0, theta_hi=80.0,
                      step_deg=1.0, force_step=1.0, ground_y=None):
    """粗搜：返回 (theta, force, miss_px)。ground_y 缺省取目标下方 200px。"""
    gy = ground_y if ground_y is not None else (y1 + 200)
    best = None
    th = theta_lo
    while th <= theta_hi:
        fo = 5.0
        while fo <= 100.0:
            pts = track(x0, y0, th, fo, wind, ground_y=gy)
            hx, hy = pts[-1]
            miss = math.hypot(hx - x1, hy - (y1 - TY_UP))
            if best is None or miss < best[2]:
                best = (th, fo, miss)
            fo += force_step
        th += step_deg
    return best


# ---------------------------------------------------------------- 自检

def _selftest(sample="data/traj_samples.json", tol_px=15.0) -> None:
    """对真实 track 样本回归；样本不存在时跳过。"""
    import json
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, sample)
    if not os.path.exists(p):
        print("skip: %s 不存在" % p)
        return
    with open(p, encoding="utf-8") as fh:
        d = json.load(fh)
    worst = 0.0
    used = 0
    for s in d["shots"]:
        pts, wind = s.get("pts"), s.get("wind")
        if not pts or wind is None or len(pts) < 4:
            continue
        used += 1
        got = track(s["pos"][0], s["pos"][1], s["angle"], s["force"], wind,
                    n_max=len(pts))
        errs = [math.dist(a, b) for a, b in zip(got, pts)]
        worst = max(worst, max(errs))
        print("%-10s n=%-3d angle=%-6s force=%-6s max_err=%.2fpx mean_err=%.2fpx"
              % (s.get("battle"), len(pts), s["angle"], s["force"],
                 max(errs), sum(errs) / len(errs)))
    if not used:
        print("skip: 样本里没有带 wind 的完整轨迹")
        return
    ok = worst <= tol_px
    print("shots=%d worst=%.2fpx tol=%.0fpx  %s"
          % (used, worst, tol_px, "PASS" if ok else "FAIL"))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    _selftest()
