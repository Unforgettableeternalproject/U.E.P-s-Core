"""P3 S5 政策快照與熱路徑（不需資料庫）：讓位判定資料表（bot test_listener／test_voice_mode 的讓位案例）、
快照同版推送、stale 退保守、ModeRules 由快照決定。時間由事件時間戳提供，不 sleep。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

pytest.importorskip("echo_stream")

from echo_stream.contracts.turn import VoiceEvent, VoiceState

from uep.echo.policy import (
    YIELD_REASON,
    CoreModeRules,
    CoreYieldPolicy,
    HostPolicySource,
)
from uep.turn.decision_config import ListenerConfig
from uep.turn.engagement import ChannelMode
from uep.turn.policy import PolicySnapshot, YieldAccumulator, mode_flags, yieldable


@dataclass
class View:
    turn_id: str = "turn_a"
    owner_id: str | None = "u1"
    reply_mode: str | None = "respond"
    audio_started: bool = False
    behind_playing: bool = False
    sink_active: bool = False
    barge_users: set[str] = field(default_factory=lambda: {"u1"})
    meta: dict = field(default_factory=dict)


def voiced(t: float, seconds: float, speaker: str | None = "u1", *, is_voiced: bool = True) -> VoiceEvent:
    return VoiceEvent(
        state=VoiceState.SPEECH if is_voiced else VoiceState.SILENCE, timestamp=t, duration_s=seconds,
        speaker_id=speaker, is_voiced=is_voiced, chunk_s=seconds,
    )


def active_source(**kw) -> HostPolicySource:
    src = HostPolicySource("discord", **kw)
    src.publish_channel_mode(ChannelMode.ACTIVE)
    return src


def feed(policy: CoreYieldPolicy, live, t0: float, seconds: float, speaker: str = "u1", step: float = 0.02):
    """每 20ms 一個有聲事件，共 seconds 秒；回傳第一次讓位的 (已餵秒數, actions)，沒有則 None。"""
    n = round(seconds / step)
    for i in range(n):
        actions = policy.on_voiced(voiced(t0 + i * step, step, speaker), live)
        if actions:
            return round((i + 1) * step, 2), actions
    return None


# ---------------------------------------------------------------------------
# 設定預設與 bot 一致
# ---------------------------------------------------------------------------


def test_listener_defaults_match_bot():
    cfg = ListenerConfig()
    assert (cfg.yield_unstarted, cfg.yield_min_speech_s, cfg.yield_min_speech_s_pending, cfg.speech_gap_reset_s) == (
        True, 1.5, 0.7, 0.5,
    )


# ---------------------------------------------------------------------------
# 讓位判定資料表
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name, live, seconds, fires", [
    # 有尚未開播的回覆：0.7s 門檻（bot test_unstarted_reply_voided_after_0_7s／test_shorter_than_threshold_is_kept）
    ("pending 0.76s", [View()], 0.76, True),
    ("pending 0.5s 擋嗯與咳嗽", [View()], 0.5, False),
    # 沒有待讓位的回覆：沒有東西可作廢（累積照常進行，見下一個測試）
    ("no live 1.6s", [], 1.6, False),
    # 已開播的回覆不讓位（bot test_started_reply_is_not_voided；交給 barge-in）
    ("audio_started 3s", [View(audio_started=True)], 3.0, False),
    # 排在正在播的前一輪後面：這段聲音可能是在打斷前一輪（bot test_queued_behind_playing_reply_is_not_yielded）
    ("behind_playing 3s", [View(behind_playing=True)], 3.0, False),
    # 別人的回覆不因這位說話者而讓位
    ("other owner", [View(owner_id="u2")], 0.76, False),
])
def test_yield_decision_table(name, live, seconds, fires):
    policy = CoreYieldPolicy(active_source())
    result = feed(policy, live, 100.0, seconds)
    assert (result is not None) is fires, name
    if fires and live:
        assert [a.turn_id for a in result[1]] == [v.turn_id for v in live if yieldable(v.owner_id, "u1", v.audio_started, v.behind_playing)]
        assert all(a.reason == YIELD_REASON for a in result[1])


def test_yield_pending_threshold_fires_at_exactly_0_7_and_resets_after_firing():
    policy = CoreYieldPolicy(active_source())
    live = [View()]
    assert policy.on_voiced(voiced(1.0, 0.69), live) == []
    assert [a.turn_id for a in policy.on_voiced(voiced(1.01, 0.01), live)] == ["turn_a"]
    # 觸發後歸零重新累積：不是每個封包都作廢一次
    assert policy.on_voiced(voiced(1.02, 0.5), live) == []
    assert [a.turn_id for a in policy.on_voiced(voiced(1.03, 0.3), live)] == ["turn_a"]


def test_yield_speech_gap_over_half_second_resets_accumulation():
    policy = CoreYieldPolicy(active_source())
    live = [View()]
    assert policy.on_voiced(voiced(10.0, 0.5), live) == []
    assert policy.on_voiced(voiced(10.4, 0.5), live) != []  # 間隔 0.4s <= 0.5：累積 1.0 >= 0.7
    policy = CoreYieldPolicy(active_source())
    assert policy.on_voiced(voiced(10.0, 0.5), live) == []
    assert policy.on_voiced(voiced(10.6, 0.5), live) == []  # 間隔 0.6s > 0.5：歸零，只有 0.5


def test_yield_no_pending_turn_uses_the_longer_threshold():
    acc = YieldAccumulator()
    snap = PolicySnapshot(channel_mode=ChannelMode.ACTIVE)
    assert not acc.observe("u1", 1.4, 1.0, snap, pending=False)
    assert acc.observe("u1", 0.2, 1.1, snap, pending=False)  # 累積 1.6 >= 1.5
    assert acc.observe("u1", 1.4, 5.0, snap, pending=True)  # 有待讓位回覆：0.7s 門檻，1.4s 觸發


def test_yield_threshold_switches_when_a_pending_turn_appears():
    """沒有回覆時照樣累積；開始有尚未開播的回覆時改用 0.7s 門檻（宿主補餵 live=[] 讓累積不中斷）。"""
    policy = CoreYieldPolicy(active_source())
    assert policy.on_voiced(voiced(5.0, 0.5), []) == []
    assert policy.on_voiced(voiced(5.1, 0.3), [View()]) != []  # 累積 0.8 >= pending 門檻 0.7


def test_yield_only_in_active_channel_mode():
    """bot：``middleware.reply_mode != ACTIVE`` 時不讓位（test_passive_reply_is_not_voided）。"""
    src = HostPolicySource("discord")  # 預設被動
    policy = CoreYieldPolicy(src)
    assert feed(policy, [View()], 0.0, 3.0) is None
    src.publish_channel_mode(ChannelMode.ACTIVE)  # 熱路徑讀到新快照
    assert feed(policy, [View()], 10.0, 3.0) is not None
    src.publish_channel_mode(ChannelMode.PASSIVE)
    assert feed(policy, [View()], 20.0, 3.0) is None


def test_yield_disabled_by_listener_config():
    src = HostPolicySource("discord", listener=ListenerConfig(yield_unstarted=False))
    src.publish_channel_mode(ChannelMode.ACTIVE)
    assert feed(CoreYieldPolicy(src), [View()], 0.0, 3.0) is None


@pytest.mark.parametrize("event", [
    voiced(1.0, 5.0, speaker=None),  # 沒有說話者
    voiced(1.0, 5.0, is_voiced=False),  # 非有聲
])
def test_yield_ignores_unvoiced_or_anonymous_events(event):
    assert CoreYieldPolicy(active_source()).on_voiced(event, [View()]) == []


def test_yield_uses_duration_when_chunk_seconds_missing():
    policy = CoreYieldPolicy(active_source())
    ev = VoiceEvent(state=VoiceState.SPEECH, timestamp=1.0, duration_s=0.8, speaker_id="u1", is_voiced=True)
    assert [a.turn_id for a in policy.on_voiced(ev, [View()])] == ["turn_a"]


def test_yield_multiple_pending_turns_of_the_same_speaker_all_yield():
    policy = CoreYieldPolicy(active_source())
    live = [View("turn_a"), View("turn_b"), View("turn_c", owner_id="u2")]
    assert sorted(a.turn_id for a in policy.on_voiced(voiced(1.0, 0.8), live)) == ["turn_a", "turn_b"]


def test_yield_is_conservative_when_snapshot_is_stale_q5():
    """Q-5：快照過期時讓位停用（不累積、不讓位）；新快照推送後恢復。"""
    src = active_source(ttl_s=0.05)
    policy = CoreYieldPolicy(src)
    time.sleep(0.08)
    assert src.core_current().is_stale()
    assert policy.on_voiced(voiced(1.0, 5.0), [View()]) == []
    src.publish()  # 推送新版（issued_at 重新起算）
    assert not src.core_current().is_stale()
    assert policy.on_voiced(voiced(2.0, 5.0), [View()]) != []


def test_yield_accumulator_is_per_speaker():
    acc = YieldAccumulator()
    snap = PolicySnapshot(channel_mode=ChannelMode.ACTIVE)
    assert not acc.observe("u1", 0.5, 1.0, snap, True)
    assert not acc.observe("u2", 0.5, 1.0, snap, True)
    assert acc.observe("u1", 0.3, 1.1, snap, True)
    acc.reset("u2")
    assert not acc.observe("u2", 0.3, 1.2, snap, True)


# ---------------------------------------------------------------------------
# 快照：同版推送、ModeRules、熱路徑不阻塞
# ---------------------------------------------------------------------------


def test_publish_bumps_es_and_core_snapshots_to_the_same_version():
    src = HostPolicySource("discord")
    assert src.version == src.core_current().version == 1
    src.publish()
    assert src.version == src.core_current().version == 2
    src.publish_channel_mode(ChannelMode.ACTIVE)
    assert src.version == src.core_current().version == 3
    assert src.core_current().channel_mode is ChannelMode.ACTIVE
    src.publish(platform="web")
    assert src.version == src.core_current().version == 4 and src.current().platform == "web"
    assert src.core_current().channel_mode is ChannelMode.ACTIVE  # 換平台不重置頻道模式


def test_core_snapshot_is_immutable_and_publish_swaps_the_reference():
    src = HostPolicySource("discord")
    old = src.core_current()
    src.publish_channel_mode(ChannelMode.ACTIVE)
    assert old.channel_mode is ChannelMode.PASSIVE and src.core_current() is not old
    with pytest.raises(AttributeError):
        old.channel_mode = ChannelMode.ACTIVE  # type: ignore[misc]


def test_hot_path_never_blocks_on_the_publisher_lock():
    """熱路徑只讀 frozen 參照：即使有人持有推送鎖，``current``／``core_current``／讓位判定照常回得來。"""
    src = active_source()
    policy = CoreYieldPolicy(src)
    done: list[bool] = []
    with src._lock:
        import threading

        t = threading.Thread(target=lambda: done.append(
            bool(src.current()) and bool(src.core_current()) and policy.on_voiced(voiced(1.0, 0.8), [View()]) != []
        ))
        t.start()
        t.join(timeout=2.0)
    assert done == [True]


@pytest.mark.parametrize("reply_mode, speak, barge", [
    ("respond", True, True),
    ("passive", False, False),  # D80 逾時預設：只旁聽，不出聲、不可打斷
    (None, True, True),  # Web 既有行為
    ("unknown_mode", True, True),
])
def test_mode_rules_default_table(reply_mode, speak, barge):
    flags = CoreModeRules().flags_for(reply_mode)
    assert (flags.speak, flags.barge_in) == (speak, barge)
    assert (speak, barge) == mode_flags(PolicySnapshot(), reply_mode)


def test_mode_rules_follow_the_snapshot_not_a_copy():
    src = HostPolicySource("discord")
    rules = src.current().mode_rules
    assert rules.flags_for("respond").barge_in
    src._core = PolicySnapshot(version=9, no_barge_in_modes=frozenset({"respond", "passive"}),
                               silent_modes=frozenset({"passive"}))
    flags = rules.flags_for("respond")
    assert flags.speak and not flags.barge_in and not flags.supersede_same_owner


def test_yieldable_requires_owner_unstarted_and_not_behind_playing():
    assert yieldable("u1", "u1", False, False)
    assert not yieldable("u1", "u2", False, False)
    assert not yieldable(None, "u1", False, False)
    assert not yieldable("u1", "u1", True, False)
    assert not yieldable("u1", "u1", False, True)
