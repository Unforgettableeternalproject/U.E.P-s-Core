"""全域停止服務（D36、T-10、CC-4 revoke）。

入口只有已認證的本機端點（``uep.stop.api`` 的本機 HTTP API、``python -m uep.stop`` CLI），不經 LLM。
``trigger`` 的順序與理由：

1. 寫入暫停旗標（DB，跨重啟存活）：旗標成立後，``PresenceManager.acquire`` 回 ``DENIED``、Task 不得轉回
   RUNNING，直到 ``release`` 明確解除。旗標先於撤銷，端點收到 revoke 後立刻重 acquire 也會被擋。
2. ``PresenceManager.revoke("global_stop")``：撤銷目前 Lease（冪等）。
3. **廣播** ``control.revoke`` 給所有已訂閱端點：不依賴 presence_current 狀態——NONE、RECOVERING、
   Lease 剛過期或根本沒有 row，都照樣通知（端點可能仍在播放）。控制通道直達端點，不經 outbox／Bus。

任何一步失敗不影響後續步驟（停止要盡最大可能送達）；失敗記入結果 ``errors``。
token 比對用常數時間；未設定 token 時一律拒絕（fail-closed）。
"""
from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from uep.config import get_settings
from uep.control.channel import ControlChannel
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.publisher import EventPublisher
from uep.ids import new_id
from uep.output.intent import ControlKind, ControlScope, ControlSignal
from uep.presence.manager import PresenceManager
from uep.presence.models import PresenceCurrentRow
from uep.stop.models import GlobalStopFlagRow

logger = logging.getLogger("uep.stop.service")

MIN_TOKEN_BYTES = 16  # token 最短長度；不足視同未設定（fail-closed）
SOURCE = "core.global_stop"
SCOPE = "scope_private_owner"
STOP_REASON = "global_stop"
# 讀不到 presence_current 時用的 generation：必然不小於任何端點的本地值，停止不會被當成「晚到」而忽略
MAX_GENERATION = 2**62


def make_flag_blocker(persona_id: str, *, lock: bool = False) -> Callable[[Session], str | None]:
    """暫停旗標檢查函式（用呼叫者的 session 讀，populate_existing 取得最新提交值）。

    獨立於 ``GlobalStop`` 建立，讓 ``PresenceManager`` 的 acquire_guard 不必等 GlobalStop 先建好
    （GlobalStop 需要 PresenceManager，避免循環）。旗標成立回 ``global_stop_engaged``，否則 None。

    ``lock=True``（execute_action 階段 1 用）：在呼叫者交易內對旗標列取共享鎖（FOR SHARE），持有到該交易結束。
    旗標列不存在時先 ``INSERT ... ON CONFLICT DO NOTHING`` 建一列 engaged=false：若 engage 正好也在插入，
    唯一索引會讓這個 INSERT 等 engage 提交，之後讀到的就是 engaged=true；因此「不存在」也被鎖住，
    不留窗口。engage 以 FOR UPDATE 取同一列，會等所有持共享鎖的階段 1 提交。
    """

    def blocker(session: Session) -> str | None:
        if lock:
            session.execute(
                pg_insert(GlobalStopFlagRow)
                .values(persona_id=persona_id, engaged=False, version=0)
                .on_conflict_do_nothing(index_elements=["persona_id"])
            )
            row = session.execute(
                select(GlobalStopFlagRow)
                .where(GlobalStopFlagRow.persona_id == persona_id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            ).scalar_one()
        else:
            row = session.get(GlobalStopFlagRow, persona_id, populate_existing=True)
        return "global_stop_engaged" if row is not None and row.engaged else None

    return blocker


class StopAuthError(PermissionError):
    """token 缺漏、錯誤，或伺服端未設定 token（fail-closed）。"""


@dataclass(frozen=True)
class GlobalStopResult:
    control_id: str
    flag_engaged: bool  # 暫停旗標已成立（含本來就成立）
    presence_revoked: bool  # 本次 revoke 實際撤銷了一個 Lease
    delivered_to: tuple[str, ...]  # 控制通道已送達（放入信箱）的端點
    errors: tuple[str, ...] = field(default_factory=tuple)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GlobalStop:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        presence: PresenceManager,
        control: ControlChannel,
        *,
        publisher: EventPublisher | None = None,
        token: SecretStr | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._sf = session_factory
        self._presence = presence
        self._control = control
        self._publisher = publisher
        self._token = token  # None -> 每次驗證時讀 settings（UEP_STOP_TOKEN）
        self._clock = clock
        self._persona = presence.persona_id
        self.warn_if_token_unusable()

    @property
    def persona_id(self) -> str:
        return self._persona

    # ---- 認證 ----

    def _configured_token(self) -> str:
        secret = self._token if self._token is not None else get_settings().stop_token
        return secret.get_secret_value() if secret is not None else ""

    def _expected_token(self) -> str:
        value = self._configured_token()
        if len(value.encode("utf-8")) < MIN_TOKEN_BYTES:  # 含空值；過短的 token 等同沒有
            raise StopAuthError(f"未設定全域停止 token（UEP_STOP_TOKEN，至少 {MIN_TOKEN_BYTES} 位元組）；入口拒絕所有請求")
        return value

    def warn_if_token_unusable(self) -> bool:
        """token 未設定或過短時告警（不輸出內容）；回傳 token 是否可用。啟動時與 API 啟動時各呼叫一次。"""
        value = self._configured_token()
        if len(value.encode("utf-8")) < MIN_TOKEN_BYTES:
            logger.warning("全域停止 token 未設定或短於 %d 位元組：本機停止入口將拒絕所有請求", MIN_TOKEN_BYTES)
            return False
        return True

    def authenticate(self, token: str | None) -> None:
        expected = self._expected_token()
        if not token or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            raise StopAuthError("全域停止 token 不正確")

    # ---- 旗標查詢 ----

    def is_engaged(self) -> bool:
        with self._sf() as s:
            return self.blocked_reason(s) is not None

    def blocked_reason(self, session: Session) -> str | None:
        """暫停旗標成立時回原因碼，否則 None。給 ``PresenceManager`` 的 acquire_guard 與 Task guard 使用。

        用呼叫者的 session 讀（populate_existing 取得最新提交值），使 acquire 的判斷與其交易一致。
        """
        return make_flag_blocker(self._persona)(session)

    # ---- 觸發與解除 ----

    def trigger(self, token: str | None, *, reason: str = STOP_REASON, operator: str = "local") -> GlobalStopResult:
        """已認證的全域停止入口。"""
        self.authenticate(token)
        return self.engage(reason=reason, operator=operator)

    def release(self, token: str | None, *, operator: str = "local") -> bool:
        """明確解除暫停旗標（需 token）。"""
        self.authenticate(token)
        return self.disengage(operator=operator)

    def resolve_recovery(self, *, operator: str = "local") -> bool:
        """操作員確認各端已停止：RECOVERING／未證實停止標記 -> NONE（不驗 token；對外入口先 ``authenticate``）。

        不碰全域停止旗標：旗標仍成立時端點照樣無法重 acquire。回傳是否有狀態變更。
        """
        return self._presence.resolve_recovery(f"operator:{operator[:100]}")

    def disengage(self, *, operator: str = "local") -> bool:
        """解除旗標（不驗 token）；回傳旗標是否由成立變為解除。解除不會自動恢復任何 Presence 或 Task。"""
        with self._sf() as session, session.begin():
            row = self._lock_flag(session)
            if not row.engaged:
                return False
            row.engaged, row.released_by, row.released_at = False, operator[:128], self._clock()
            row.version += 1
            recorded = self._record(session, "core.global_stop_released", operator, "released")
        self._dispatch(recorded)
        return True

    def engage(self, *, reason: str = STOP_REASON, operator: str = "local") -> GlobalStopResult:
        """停止的實際動作（不驗 token；對外入口請用 ``trigger``）。每一步獨立嘗試，不因前一步失敗而中止。"""
        errors: list[str] = []
        flag_engaged = False
        try:
            flag_engaged = self._engage_flag(reason, operator)
        except Exception as exc:
            logger.exception("全域停止：寫入暫停旗標失敗")
            errors.append(f"flag:{type(exc).__name__}")
        revoked = False
        try:
            if self._presence.started:
                revoked = self._presence.revoke(STOP_REASON).lease_id is not None
        except Exception as exc:
            logger.exception("全域停止：撤銷 Presence 失敗")
            errors.append(f"revoke:{type(exc).__name__}")
        core_epoch, generation = self._fence(errors)
        signal = ControlSignal(
            control_id=new_id("ctl"), kind=ControlKind.REVOKE, scope=ControlScope.PROCESS,
            target_fence={"core_epoch": core_epoch, "presence_generation": generation},
            issued_at=self._clock(),
        )
        delivered: tuple[str, ...] = ()
        try:
            # 廣播給所有已訂閱端點，不看 presence_current 狀態（NONE／RECOVERING 也要通知）
            delivered = self._control.send(signal).delivered_to
        except Exception as exc:
            logger.exception("全域停止：控制通道廣播失敗")
            errors.append(f"broadcast:{type(exc).__name__}")
        return GlobalStopResult(signal.control_id, flag_engaged, revoked, delivered, tuple(errors))

    # ---- 內部 ----

    def _lock_flag(self, session: Session) -> GlobalStopFlagRow:
        session.execute(
            pg_insert(GlobalStopFlagRow)
            .values(persona_id=self._persona, engaged=False, version=0)
            .on_conflict_do_nothing(index_elements=["persona_id"])
        )
        return session.execute(
            select(GlobalStopFlagRow)
            .where(GlobalStopFlagRow.persona_id == self._persona)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()

    def _engage_flag(self, reason: str, operator: str) -> bool:
        with self._sf() as session, session.begin():
            row = self._lock_flag(session)
            already = row.engaged
            if not already:
                row.engaged, row.reason, row.engaged_by = True, reason[:128], operator[:128]
                row.engaged_at, row.released_by, row.released_at = self._clock(), None, None
                row.version += 1
                recorded = self._record(session, "core.global_stop_engaged", operator, reason)
        if not already:
            self._dispatch(recorded)
        return True

    def _record(self, session: Session, event_type: str, operator: str, reason: str):
        if self._publisher is None:
            return None
        draft = EventDraft(event_type=event_type, occurred_at=self._clock(), payload={"reason_code": reason[:128]})
        trusted = TrustedContext(
            source=SOURCE, actor_id=f"operator:{operator[:100]}", scope_ref=SCOPE, persona_id=self._persona
        )
        return self._publisher.record(session, draft, trusted)

    def _dispatch(self, recorded) -> None:
        if recorded is None or self._publisher is None:
            return
        try:
            self._publisher.dispatch(recorded)
        except Exception:
            logger.exception("全域停止事件投遞失敗")

    def _fence(self, errors: list[str]) -> tuple[int, int]:
        """廣播用的 (core_epoch, generation)：直接讀 presence_current，不看 status。

        generation 取目前值：端點本地 generation 不會大於它，故 ``signal.generation >= 本地`` 成立。
        讀不到時退回 Manager 持有的 core_epoch 與 ``MAX_GENERATION``，並記入 errors。
        """
        fallback_epoch = self._presence.core_epoch if self._presence.started else 0
        try:
            with self._sf() as s:
                row = s.get(PresenceCurrentRow, self._persona, populate_existing=True)
                if row is None:
                    return fallback_epoch, 0
                return row.core_epoch, row.generation
        except Exception as exc:
            logger.exception("全域停止：讀取 presence_current 失敗")
            errors.append(f"fence:{type(exc).__name__}")
            return fallback_epoch, MAX_GENERATION
