# -*- coding: utf-8 -*-
"""TNT 地形模型: 可变形高度图(弹坑雕刻) + 弹道碰撞。

设计(§55.44):
  - 每图一张高度图(每列地面 y, 5px 分辨率);爆炸在落点雕出圆弧弹坑(半径=武器爆炸范围 blast_of)。
  - 弹道 track/first_hit 用地形碰撞(可传 ground_fn),后续炮弹会落进坑里(真实玩法)。
  - 客户端本地渲染弹坑: 服务端 Fire 帧轨迹末点 + 伤害条目 f22 落点 → 客户端自行雕坑(待多发实测确认)。
  - 地面基线: 旧 GROUND_Y=900 占位;角色立于 y≈320 平台。高度图基线=各图地面采样,可后续按图精调。
"""
from ballistics import GROUND_Y

RES = 5  # px/格


class Terrain:
    def __init__(self, map_id: int, w: int = 2090, h: int = 1844, base_y: float = GROUND_Y):
        self.map_id = map_id
        self.w, self.h = w, h
        self.base_y = base_y
        self.n = w // RES + 2
        self.ground = [float(base_y)] * self.n   # 每格地面 y(向下为正)
        self.craters = []                        # (x, y, r) 已雕弹坑(客户端同步证据用)
        self.platforms = []                      # §55.50 悬浮平台 (cx, y, half_width)

    def add_platform(self, cx: float, y: float, half_width: float = 200):
        """§55.50 出生平台: 角色站立的悬浮台面(钻石平台)。弹着台面=可见爆炸/弹坑点。"""
        for i in range(self.n):
            x = i * RES
            if abs(x - cx) <= half_width and self.ground[i] > y:
                self.ground[i] = y
        self.platforms.append((cx, y, half_width))

    def _idx(self, x: float) -> int:
        i = int(x) // RES
        return max(0, min(self.n - 1, i))

    def surface_y(self, x: float) -> float:
        """x 处当前地面 y。"""
        return self.ground[self._idx(x)]

    def collide(self, x: float, y: float) -> bool:
        return y >= self.surface_y(x)

    def carve(self, x: float, y: float, radius: float):
        """爆炸雕坑: 坑为圆弧下凹(y 增大),坑缘微隆+2px。只挖不低于坑深限。"""
        if radius <= 0:
            return
        r = float(radius)
        i0 = self._idx(x - r - RES)
        i1 = self._idx(x + r + RES)
        for i in range(i0, i1 + 1):
            gx = i * RES
            d = abs(gx - x)
            if d >= r:
                # 坑缘微隆(真实爆炸挤压)
                self.ground[i] = min(self.base_y + 40, max(self.ground[i], y + 2))
                continue
            depth = (r * r - d * d) ** 0.5
            floor_y = y + depth * 0.85
            if floor_y > self.ground[i]:
                self.ground[i] = min(floor_y, self.base_y + 220)  # 坑深上限
        self.craters.append((int(x), int(y), int(r)))

    def snapshot(self):
        """调试/日志: 弹坑清单。"""
        return {"map": self.map_id, "craters": self.craters}
