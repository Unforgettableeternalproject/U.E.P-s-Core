"""Journal replay／tailing 的安全水位讀取（D41、契約 E-14）。

``journal_seq`` 只保證唯一與單調分配，不保證提交可見順序：後號可能先可見，以
``journal_seq > last`` 推進游標會永久漏掉較早分配、較晚提交的列。這裡改以寫入交易 id 當水位：

- 每列在寫入時記下 ``events.txid``（``txid_current()``）。
- 水位 ``xmin = pg_snapshot_xmin(pg_current_snapshot())``：所有 ``txid < xmin`` 的交易都已提交或中止，
  因此 ``txid < xmin`` 的列集合是**完整且不會再增加**的。
- 游標是 ``[low_txid, high_txid)`` 的交易區間加區間內的 ``journal_seq``：先取得 ``high_txid``（當時水位），
  區間內依 ``journal_seq`` 排序分頁讀完後才把 ``low_txid`` 推進到 ``high_txid``。區間內的列一次完整，
  不漏、不重。

順序保證：每個區間內依 ``journal_seq`` 遞增；跨區間時，較晚提交的交易所分配的較小 ``journal_seq`` 會出現在
較後的區間（E-14 本來就不保證全域提交順序）。消費者若需要因果順序，依 ``aggregate_version``（E-8）。

``safety_lag``：水位採「至少 ``safety_lag`` 秒前觀察到的 xmin」，容忍短暫的觀察落差（例如讀取端時鐘或複本延遲）；
因 xmin 單調不減，延遲採用較舊的水位仍然正確，只是晚一點才看到新列。

運作限制：
- **idle-in-transaction 會卡水位**：任何長時間未結束的交易（包含只讀、停在 idle in transaction 的連線）都會把
  xmin 釘住，tailing 在它結束前不會前進（事件不遺失，只是延遲）。使用方須避免長交易，並監控水位落後量。
- **txid 綁定叢集**：``events.txid`` 是該 PostgreSQL 叢集的交易號。還原備份、換叢集或 dump／restore 後
  txid 空間不同，舊游標不再有意義，**必須重置游標**（``TailCursor()`` 從頭，或 ``from_now``）。
  游標的 ``low_txid`` 若大於目前水位（xmin 單調不減，不可能自然發生），``poll`` 拋 ``TailCursorMismatch``，
  不再靜默回空。``watermark_lag`` 回報水位落後量供監控。
- ``safety_lag`` 只延後水位的採用時間，不能取代正確性機制；0 即為立即採用目前水位。
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from uep.events.envelope import UEPEvent
from uep.events.journal import row_to_event
from uep.events.models import EventRow

_XMIN_SQL = text("SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint")


class TailCursorMismatch(RuntimeError):
    """游標的 ``low_txid`` 大於目前水位：同一叢集 xmin 單調不減，代表叢集已變（還原／換庫），游標必須重置。"""


@dataclass(frozen=True)
class TailCursor:
    """可持久化的游標。``low_txid`` 之前的所有交易已處理完；``high_txid`` 非 None 表示區間處理中。"""

    low_txid: int = 0
    high_txid: int | None = None
    last_seq: int = 0


def current_xmin(session: Session) -> int:
    """目前的安全水位（所有 txid 小於它的交易都已結束）。"""
    return int(session.execute(_XMIN_SQL).scalar_one())


def watermark_lag(session: Session, cursor: TailCursor) -> int:
    """水位落後量（L7 觀測）：目前 xmin 與游標 ``low_txid`` 的差；持續變大代表有長交易釘住水位或消費者落後。

    負值代表游標來自其他叢集（``poll`` 會拋 ``TailCursorMismatch``），此處原值回傳以利監控判讀。
    """
    return current_xmin(session) - cursor.low_txid


class JournalTailer:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        cursor: TailCursor | None = None,
        safety_lag: timedelta = timedelta(0),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._cursor = cursor or TailCursor()
        self._lag = safety_lag.total_seconds()
        self._clock = clock
        self._observed: deque[tuple[float, int]] = deque()  # (觀察時刻, xmin)

    @classmethod
    def from_now(cls, session_factory: sessionmaker[Session], **kwargs) -> JournalTailer:
        """從目前水位開始（略過既有歷史，只收之後提交的列）。"""
        with session_factory() as session:
            low = current_xmin(session)
        return cls(session_factory, cursor=TailCursor(low_txid=low), **kwargs)

    @property
    def cursor(self) -> TailCursor:
        """目前游標；消費者處理完 ``poll`` 的結果後存下它，重啟時傳回建構子。"""
        return self._cursor

    def _eligible_xmin(self, xmin: int) -> int | None:
        now = self._clock()
        self._observed.append((now, xmin))
        eligible: int | None = None
        while self._observed and self._observed[0][0] <= now - self._lag:
            eligible = self._observed.popleft()[1]
        if eligible is not None:
            self._observed.appendleft((now - self._lag, eligible))  # 保留，下次仍有水位可用
        return eligible

    def poll(self, limit: int = 100) -> list[UEPEvent]:
        """回傳下一批低於安全水位的事件並推進游標；沒有新事件回空 list。"""
        if limit < 1:
            raise ValueError("limit 必須 >= 1")
        cursor = self._cursor
        if cursor.high_txid is None:
            with self._session_factory() as session:
                xmin = current_xmin(session)
            watermark = self._eligible_xmin(xmin)
            if watermark is not None and watermark < cursor.low_txid:
                raise TailCursorMismatch(
                    f"水位 {watermark} 小於游標 low_txid {cursor.low_txid}：叢集已變，請重置游標"
                )
            if watermark is None or watermark == cursor.low_txid:
                return []
            cursor = replace(cursor, high_txid=watermark, last_seq=0)
        with self._session_factory() as session:
            rows = session.scalars(
                select(EventRow)
                .where(
                    EventRow.txid >= cursor.low_txid,
                    EventRow.txid < cursor.high_txid,
                    EventRow.journal_seq > cursor.last_seq,
                )
                .order_by(EventRow.journal_seq)
                .limit(limit)
                .execution_options(populate_existing=True)
            ).all()
            events = [row_to_event(row) for row in rows]
        if len(rows) < limit:  # 區間已讀完：整段推進
            self._cursor = TailCursor(low_txid=cursor.high_txid)
        else:
            self._cursor = replace(cursor, last_seq=rows[-1].journal_seq)
        return events
