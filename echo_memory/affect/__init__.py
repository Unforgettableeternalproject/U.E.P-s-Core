"""
affect — 情感與潛意識評估模組

對應 OpenSoul 的 soul/affect/ 和 soul/gating/。
包含顯著性評估、潛意識評估、品質閘門等。
"""

from echo_memory.affect.signals import SalienceSignals, SubconsciousAssessment
from echo_memory.affect.salience import SalienceEvaluator
from echo_memory.affect.verifier import ResponseVerifier, VerificationResult
from echo_memory.affect.inhibitor import (
    SubconsciousInhibitor,
    InhibitionAction,
    GateDecision,
)
from echo_memory.affect.judge import (
    StorageJudge,
    JudgeAgent,  # backward-compatible alias
    StorageDecision,
    ConceptSeed,
    JudgeGateResult,
    JudgeRecommendation,
)

__all__ = [
    "SalienceSignals",
    "SubconsciousAssessment",
    "SalienceEvaluator",
    "ResponseVerifier",
    "VerificationResult",
    "SubconsciousInhibitor",
    "InhibitionAction",
    "GateDecision",
    "StorageJudge",
    "JudgeAgent",
    "StorageDecision",
    "ConceptSeed",
    "JudgeGateResult",
    "JudgeRecommendation",
]
