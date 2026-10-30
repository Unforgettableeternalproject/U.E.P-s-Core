"""身分映射（D77）：platform user -> actor -> owner scope。

- ``actor_id`` 由 Gateway 依 Adapter 認證重建；payload 內的 ``speaker_ref`` 是不受信資料，
  本模組的 ``resolve_platform`` 只拿它查表，查不到一律是訪客，不從字串推測身分。
- 映射缺失一律視為訪客（owner_id=None）：訪客沒有私人記憶，也不能寫入任何 owner 的記憶。
- self／私人 level 的獨立性併入 D38（未定），這裡只有 owner private 一種私人範圍。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from uep.memory.namespaces import validate_owner_id


@dataclass(frozen=True)
class ResolvedIdentity:
    actor_id: str | None
    owner_id: str | None
    platform_uid: str | None = None  # 平台使用者 id（共享層的 viewer_uid），來自映射表而非 payload

    @property
    def guest(self) -> bool:
        return self.owner_id is None


GUEST = ResolvedIdentity(actor_id=None, owner_id=None)


class IdentityDirectory:
    """唯讀映射表。``platform_to_actor``：``discord:<id>`` -> actor_id；``actor_to_owner``：actor_id -> owner_id。"""

    def __init__(self, platform_to_actor: Mapping[str, str], actor_to_owner: Mapping[str, str]) -> None:
        self._platform_to_actor = dict(platform_to_actor)
        self._actor_to_owner = {actor: validate_owner_id(owner) for actor, owner in actor_to_owner.items()}
        self._actor_to_platform_uid: dict[str, str] = {}
        for ref, actor in self._platform_to_actor.items():
            self._actor_to_platform_uid.setdefault(actor, ref.split(":", 1)[-1])

    def resolve_actor(self, actor_id: str | None) -> ResolvedIdentity:
        """依 Gateway 重建的 actor_id 解析；映射缺失 -> 訪客。"""
        if not actor_id:
            return GUEST
        owner = self._actor_to_owner.get(actor_id)
        if owner is None:
            return ResolvedIdentity(actor_id=actor_id, owner_id=None)
        return ResolvedIdentity(
            actor_id=actor_id, owner_id=owner, platform_uid=self._actor_to_platform_uid.get(actor_id)
        )

    def resolve_platform(self, speaker_ref: str | None) -> ResolvedIdentity:
        """依平台字串（不受信）查表；只有映射表命中的才可能是 owner。"""
        actor = self._platform_to_actor.get(speaker_ref or "")
        return self.resolve_actor(actor)
