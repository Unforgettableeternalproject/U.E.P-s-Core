from dataclasses import dataclass
import math

try:
    from PyQt5.QtCore import QPoint  # 型別相容
except Exception:
    class QPoint:  # 避免無 PyQt5 時爆掉
        def __init__(self, x: int = 0, y: int = 0):
            self.x = x
            self.y = y


@dataclass
class Position:
    x: float
    y: float

    def to_qpoint(self) -> "QPoint":
        return QPoint(int(self.x), int(self.y))

    def distance_to(self, other: "Position") -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


@dataclass
class Velocity:
    x: float
    y: float

    def magnitude(self) -> float:
        return math.hypot(self.x, self.y)

    def normalize(self) -> "Velocity":
        mag = self.magnitude()
        if mag > 0:
            return Velocity(self.x / mag, self.y / mag)
        return Velocity(0.0, 0.0)