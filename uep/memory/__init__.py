"""P3-M MemoryHost：engine registry、啟動／關閉契約、Memory Access Layer、sink 轉譯、做夢 Activity。"""
from uep.memory.access import GuildMembership, MemoryAccessLayer
from uep.memory.dream_loop import DreamLoop, ForegroundProbe
from uep.memory.dream_planner import DreamLoopConfig, DreamPlanner, FixedWindowPolicy
from uep.memory.host import (
    MemoryHost,
    MemoryHostError,
    NamespaceSpec,
    StartReport,
)
from uep.memory.identity import GUEST, IdentityDirectory, ResolvedIdentity
from uep.memory.namespaces import ForeignClaim
from uep.memory.protocol import (
    MemoryEngineLike,
    MemoryPort,
    RetrieveResult,
    StoreResult,
)
from uep.memory.sink import MemorySink, translate_draft

__all__ = [
    "GUEST", "DreamLoop", "DreamLoopConfig", "DreamPlanner", "FixedWindowPolicy", "ForegroundProbe", "ForeignClaim", "GuildMembership", "IdentityDirectory", "MemoryAccessLayer", "MemoryEngineLike",
    "MemoryHost", "MemoryHostError", "MemoryPort", "MemorySink", "NamespaceSpec", "ResolvedIdentity",
    "RetrieveResult", "StartReport", "StoreResult", "translate_draft",
]
