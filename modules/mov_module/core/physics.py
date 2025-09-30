from __future__ import annotations
from .position import Velocity

class PhysicsEngine:
    """簡化版物理引擎。專責速度整形與基本外力，
    讓協調器決定何時應用哪種規則。"""

    def __init__(self,
                 gravity: float = 0.8,
                 damping: float = 0.978,
                 ground_friction: float = 0.95,
                 air_resistance: float = 0.99):
        self.gravity = gravity
        self.damping = damping
        self.ground_friction = ground_friction
        self.air_resistance = air_resistance

    # —— 單一職責的小步驟 ——
    def apply_gravity(self, v: Velocity, grounded: bool) -> Velocity:
        if not grounded:
            v.y += self.gravity
        return v

    def apply_damping(self, v: Velocity, factor: float | None = None) -> Velocity:
        f = factor or self.damping
        v.x *= f
        v.y *= f
        return v

    def apply_friction(self, v: Velocity, grounded: bool) -> Velocity:
        if grounded:
            v.x *= self.ground_friction
        else:
            v.x *= self.air_resistance
            v.y *= self.air_resistance
        return v

    # —— 協調器高階呼叫的封裝 ——
    def step_ground(self, v: Velocity) -> Velocity:
        v = self.apply_friction(v, grounded=True)
        return v

    def step_float(self, v: Velocity) -> Velocity:
        v = self.apply_friction(v, grounded=False)
        return v

    def step_thrown(self, v: Velocity, grounded: bool) -> Velocity:
        v = self.apply_gravity(v, grounded)
        v = self.apply_damping(v)
        return v