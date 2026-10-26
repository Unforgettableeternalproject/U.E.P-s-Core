"""背景重連監督（J1；純邏輯，不 sleep）。"""
from __future__ import annotations

from uep.endpoint.endpoint import ReconnectOutcome
from uep.endpoint.reconnect import ReconnectSupervisor


class Link:
    def __init__(self) -> None:
        self.connected = True
        self.connect_fails = 0
        self.outcomes: list = []
        self.connects = 0
        self.syncs = 0

    def connect(self) -> None:
        self.connects += 1
        if self.connect_fails:
            self.connect_fails -= 1
            raise ConnectionError("連不上")
        self.connected = True

    def sync(self) -> ReconnectOutcome:
        self.syncs += 1
        outcome = self.outcomes.pop(0) if self.outcomes else ReconnectOutcome.RESUMED
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make(link: Link) -> ReconnectSupervisor:
    return ReconnectSupervisor(lambda: link.connected, link.connect, link.sync, base=0.5, cap=4.0, poll=1.0)


def test_idle_when_connected_and_synced():
    link = Link()
    sup = make(link)
    assert sup.step() == 1.0 and link.connects == 0 and link.syncs == 0


def test_reconnects_with_exponential_backoff_then_syncs():
    link = Link()
    link.connected, link.connect_fails = False, 3
    sup = make(link)
    assert [sup.step() for _ in range(3)] == [0.5, 1.0, 2.0]  # 退避加倍
    assert sup.step() == 1.0 and link.connected and link.syncs == 1
    assert sup.step() == 1.0 and link.syncs == 1  # 同步完成後不重複


def test_backoff_is_capped():
    link = Link()
    link.connected, link.connect_fails = False, 10
    sup = make(link)
    assert max(sup.step() for _ in range(8)) == 4.0


def test_sync_failure_or_retry_outcome_is_retried_without_reconnecting():
    link = Link()
    link.connected = False
    link.outcomes = [OSError("快照失敗"), ReconnectOutcome.RETRY, ReconnectOutcome.RESUMED]
    sup = make(link)
    assert sup.step() == 0.5 and link.connects == 1
    assert sup.step() == 1.0 and link.connects == 1 and link.syncs == 2  # 已連線，只重做同步
    assert sup.step() == 1.0 and link.syncs == 3  # RESUMED：完成
    assert sup.step() == 1.0 and link.syncs == 3


def test_drop_after_success_triggers_new_sync():
    link = Link()
    sup = make(link)
    link.connected = False
    sup.step()
    assert link.connects == 1 and link.syncs == 1
    link.connected = False
    sup.step()
    assert link.syncs == 2
