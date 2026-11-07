# -*- coding: utf-8 -*-
"""私服弹道入口 = tools/ballistics.py（仓库内唯一真相）。

不在 server/ 下再放一份物理实现：历史上两份实现漂移，
导致服务端轨迹与客户端渲染差 30px 以上。这里只做 re-export。
"""
import sys
from pathlib import Path

_TOOLS = str(Path(__file__).resolve().parent.parent / "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "_tnt_ballistics", str(Path(_TOOLS) / "ballistics.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

G = _mod.G
K = _mod.K
MUZZLE = _mod.MUZZLE
V0K = _mod.V0K
WIND_MULT = _mod.WIND_MULT
blast_of = _mod.blast_of
first_hit = _mod.first_hit
muzzle = _mod.muzzle
solve_angle_force = _mod.solve_angle_force
solve_target = _mod.solve_target
track = _mod.track
track_closed_form = _mod.track_closed_form
velocity = _mod.velocity
GROUND_Y = _mod.GROUND_Y
PX_PER_JU = _mod.PX_PER_JU
TY_UP = _mod.TY_UP
