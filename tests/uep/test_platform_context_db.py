"""PresenceManager.peek 與 provider 的 DB 整合（需 PostgreSQL）：peek 真的唯讀，provider 跟著真實 Presence 變動。"""
from __future__ import annotations

import pytest

from tests.uep.presence_support import activate, make_rig
from uep.contracts.presence import PresenceStatus
from uep.echo.platform_context import PlatformContextProvider
from uep.presence.manager import PresenceManager

pytestmark = pytest.mark.usefixtures("session_factory")


def test_peek_before_start_raises(session_factory, tmp_path):
    from uep.presence.epoch import HostEpochStore
    from uep.presence.manager import PresenceNotStarted

    manager = PresenceManager(session_factory, epoch_store=HostEpochStore(tmp_path / "e.json"))
    with pytest.raises(PresenceNotStarted):
        manager.peek()


def test_peek_does_not_write_even_when_lease_is_expired(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    activate(rig)
    before = rig.manager.peek()
    rig.clock.advance(31)  # Lease 過期但尚未被掃描
    stale = rig.manager.peek()
    assert stale.status is PresenceStatus.ACTIVE and stale.row_version == before.row_version  # peek 不處理過期、不寫入
    assert rig.manager.peek().row_version == before.row_version
    after = rig.manager.current()  # current() 才會進寫入交易轉換
    assert after.status is PresenceStatus.NONE and after.row_version > before.row_version


def test_provider_follows_real_presence(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    provider = PlatformContextProvider(rig.manager, clock=rig.clock)
    none_ctx = provider.snapshot("web")
    assert none_ctx.current_platform is None
    activate(rig, location="discord")
    active = provider.snapshot("web")
    assert active.current_platform == "discord" and active.session_platform == "website"
    assert active.version == none_ctx.version + 1 and active.available_executors == ("discord",)
    rig.clock.advance(31)  # peek 不轉換，但 provider 以 expires_at 判為 NONE
    expired = provider.snapshot("web")
    assert expired.current_platform is None and expired.presence_status is PresenceStatus.NONE
