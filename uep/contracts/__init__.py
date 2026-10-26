"""Core 與端點共用的契約型別（D28）：只含純資料型別，不依賴 uep 其他套件。"""
from uep.contracts.presence import (
    AcquireDenyReason,
    AcquireResult,
    AuthorizationTuple,
    ConfirmResult,
    GateDecision,
    PresenceLocation,
    PresenceStatus,
    ReleaseResult,
    RenewResult,
    TransferPhase,
)

__all__ = [
    "AcquireDenyReason",
    "AcquireResult",
    "AuthorizationTuple",
    "ConfirmResult",
    "GateDecision",
    "PresenceLocation",
    "PresenceStatus",
    "ReleaseResult",
    "RenewResult",
    "TransferPhase",
]
