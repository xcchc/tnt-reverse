# -*- coding: utf-8 -*-
"""对 data/traj_samples.json 里的真实 track 重新拟合弹道常数。

仅用于研究：默认参数（libgame 静态常量）已经在 2 发样本上通过回归，
拟合值偏差主要来自样本太少（只有顺风弹，风系数欠定）。

用法：
    python tools/fit_ballistics.py
需要 numpy + scipy。
"""
from __future__ import annotations

import json
import math
import os

import numpy as np
from scipy.optimize import least_squares


def simulate(p, shot):
    K, G, WM, V0, MU, vy = p
    r = math.radians(shot["angle"])
    v0 = V0 * (shot["force"] / 100.0)
    vx = v0 * math.cos(r)
    vyv = -v0 * math.sin(r) * vy
    x = shot["pos"][0] + MU * math.cos(r)
    y = shot["pos"][1] - MU * math.sin(r)
    pts = [(x, y)]
    we = WM * shot["wind"]
    for _ in range(len(shot["pts"]) - 1):
        x += vx
        y += vyv
        vx += K * (we - vx)
        vyv += K * (0.0 - vyv) + G
        pts.append((x, y))
    return pts


def residuals(p, shots):
    out = []
    for s in shots:
        for a, b in zip(simulate(p, s), s["pts"]):
            out.append(a[0] - b[0])
            out.append(a[1] - b[1])
    return np.array(out)


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", "traj_samples.json")
    with open(p, encoding="utf-8") as fh:
        d = json.load(fh)
    shots = [s for s in d["shots"]
             if s.get("wind") is not None and s.get("pts") and len(s["pts"]) >= 4]
    if not shots:
        print("没有可用样本")
        return
    guess = [0.0186951412, 4.28938354, 1.46643562, 144.787175, 28.0, 1.0]
    lo = [0.001, 0.1, 0.0, 80.0, 10.0, 0.5]
    hi = [0.2, 20.0, 5.0, 400.0, 60.0, 1.5]
    r = least_squares(residuals, guess, args=(shots,), bounds=(lo, hi), max_nfev=4000)
    err = np.abs(r.fun)
    print("shots = %d" % len(shots))
    print("params: K=%.6f G=%.4f WIND_MULT=%.4f V0K=%.4f MUZZLE=%.3f vy_scale=%.4f"
          % tuple(r.x))
    print("residual: max=%.3fpx rms=%.3fpx" % (err.max(), float((err ** 2).mean() ** 0.5)))
    print("默认参数残差见 `python tools/ballistics.py`；样本只有顺风弹，")
    print("WIND_MULT 在此不可信，实现里请沿用 1.46643562。")


if __name__ == "__main__":
    main()
