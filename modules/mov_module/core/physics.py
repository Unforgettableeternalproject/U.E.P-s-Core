from __future__ import annotations
from .position import Velocity

class PhysicsEngine:
    """簡化版物理引擎。專責速度整形與基本外力，
    讓協調器決定何時應用哪種規則。
    
    參考 TestOverlayApplication/desktop_pet.py 的物理系統實現。
    """

    def __init__(self,
                 gravity: float = 0.8,
                 damping: float = 0.978,
                 ground_friction: float = 0.95,
                 air_resistance: float = 0.99,
                 bounce_factor: float = 0.4):
        self.gravity = gravity
        self.damping = damping
        self.ground_friction = ground_friction
        self.air_resistance = air_resistance
        self.bounce_factor = bounce_factor  # 地面反彈係數

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
        """投擲狀態的物理模擬
        
        Args:
            v: 當前速度
            grounded: 是否觸地
            
        Returns:
            更新後的速度
        
        Note:
            反彈逻輯由協調器處理，這裡只負責重力和摩擦
            
        修復：
            - 空中時只對水平速度應用空氣阻力，垂直速度只受重力影響
            - 避免對垂直速度雙重應用阻力導致撞牆感
        """
        v = self.apply_gravity(v, grounded)
        # 接地時使用地面摩擦，空中時使用空氣阻力
        if grounded:
            v.x *= self.ground_friction
        else:
            # 空中只對水平速度應用阻力，垂直速度已經被重力處理
            v.x *= self.air_resistance
            # 不對 v.y 應用阻力，避免投擲軌跡不自然
        return v
    
    def apply_bounce(self, v: Velocity, factor: float | None = None) -> Velocity:
        """應用反彈，反轉垂直速度並減小
        
        Args:
            v: 當前速度
            factor: 反彈係數，預設使用 self.bounce_factor
        """
        f = factor if factor is not None else self.bounce_factor
        v.y = -v.y * f
        return v