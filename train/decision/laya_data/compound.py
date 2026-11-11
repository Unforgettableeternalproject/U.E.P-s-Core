"""複合指示（一句裡有兩個以上獨立指示）的標記。

艾斯維爾考慮讓 Laya 先判斷「是否為複合指示」，複合句再交給拆分與連鎖處理，標法可能改變；
目前標註照原規則「先取第一個」，只在 ``meta.compound = true`` 標記，不刪、不拆。

- ``request_two_places`` 卡：叫她去兩個地方（地點已由 first_place 規則取第一個）。
- ``COMPOUND_NUMBERS``：人工掃描挑出的句子（turn.reply 的多步驟任務句，以及 absent 的「提醒＋轉告」混合句）。
  單一指示加上延後（「…, tell me later」）、同一封信的兩段內容、「remind me, ping me」這類同一提醒的重複寫法不算複合。
  掃描是正則找候選再人工確認，新增資料後需重掃。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

COMPOUND_CARDS = frozenset({"absent_request_two_places"})
_TURN = (
    56, 58, 149, 187, 188, 189, 190, 279, 280, 281, 282, 317, 318, 319, 320, 410, 446, 447,
    570, 571, 572, 573, 662, 664, 665, 698, 700, 701, 790, 791, 792, 827, 829,
)
COMPOUND_NUMBERS: frozenset[str] = frozenset(
    {f"synthetic-en-turn.reply-{n:05d}" for n in _TURN} | {"synthetic-en-presence.absent_intent-00405"}
)


def is_compound(row: Mapping[str, Any]) -> bool:
    return row["sample_id"] in COMPOUND_NUMBERS or str((row.get("meta") or {}).get("card", "")) in COMPOUND_CARDS
