"""CC-4 共用型別：授權六元組、Output Gate 結果碼、操作結果碼（契約 §4.1–§4.4、D27、D28）。

Core 與端點（Adapter）同源引用，避免各自維護一份碼表。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PresenceStatus(StrEnum):
    """presence_current.status 只有三態；移轉階段只存在於 transfer.phase。"""

    NONE = "NONE"
    ACTIVE = "ACTIVE"
    RECOVERING = "RECOVERING"


class PresenceLocation(StrEnum):
    DESKTOP = "desktop"
    DISCORD = "discord"
    WEBSITE = "website"


class TransferPhase(StrEnum):
    PREPARING = "PREPARING"
    QUIESCING = "QUIESCING"
    CHECKPOINTED = "CHECKPOINTED"
    ACTIVATING = "ACTIVATING"
    DONE = "DONE"
    ABORTED = "ABORTED"


class GateDecision(StrEnum):
    """Output Gate 結果碼（D28）。"""

    ADMIT = "ADMIT"
    REJECT_STALE = "REJECT_STALE"
    REJECT_NO_PRESENCE = "REJECT_NO_PRESENCE"
    REJECT_AUDIENCE = "REJECT_AUDIENCE"
    REJECT_EXPIRED = "REJECT_EXPIRED"
    REJECT_REVOKED = "REJECT_REVOKED"

    @property
    def admitted(self) -> bool:
        return self is GateDecision.ADMIT


class AcquireResult(StrEnum):
    GRANTED = "GRANTED"
    BUSY = "BUSY"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    TRANSFER_PENDING = "TRANSFER_PENDING"
    NOT_QUIESCED = "NOT_QUIESCED"
    DENIED = "DENIED"


class RenewResult(StrEnum):
    RENEWED = "RENEWED"
    STALE = "STALE"
    EXPIRED = "EXPIRED"


class ReleaseResult(StrEnum):
    RELEASED = "RELEASED"
    STALE = "STALE"


class ConfirmResult(StrEnum):
    ACCEPTED = "ACCEPTED"
    STALE = "STALE"


@dataclass(frozen=True)
class AuthorizationTuple:
    """授權六元組（契約 §0／§4.3）；任何一值落後即為舊授權（P-3）。"""

    persona_id: str
    endpoint_id: str
    lease_id: str
    fencing_epoch: int
    core_epoch: int
    presence_generation: int
