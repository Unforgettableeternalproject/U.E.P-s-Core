"""
affect — 情感與潛意識評估模組

對應 OpenSoul 的 soul/affect/ 和 soul/gating/。
包含顯著性評估、潛意識評估、品質閘門等。
"""

from echo_memory.affect.signals import SalienceSignals, SubconsciousAssessment
from echo_memory.affect.salience import SalienceEvaluator

__all__ = [
    "SalienceSignals",
    "SubconsciousAssessment",
    "SalienceEvaluator",
]
