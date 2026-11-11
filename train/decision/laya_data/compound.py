"""複合指示（compound）的既有資料標記。

定義（艾斯維爾定稿）：compound=yes＝一句裡有好幾個彼此獨立的意圖，各自交給不同的處理路線或對象
（例：remind me at four…, and tell Rob…）。compound=no：
  a. 為了同一個目標的多個步驟，交給 agent_loop；
  b. 一個意圖裡有好幾項內容（tell Rob I'm late and sorry、remind me to buy milk and eggs）；
  c. 段落之間有條件或資料依賴（查天氣，晴天就跟 Rob 說去爬山）→ 在場走 agent_loop，不在場照意圖標（多為 task）。

既有合成資料預設 no；只有下列例外為 yes（其餘句子由 resolve 的盲標抽查補驗）：
- ``request_two_places`` 卡：叫她去兩個地方（地點已由 first_place 規則取第一個）。
- ``COMPOUND_IDS``：人工確認的「提醒＋轉告」混合句。
先前掃出的 32 筆 turn.reply agent_loop 多步驟句已全數重判為 no（同一目標的步驟或有資料依賴）。
標註為複合句時，其餘欄位照退回優先序：absent_intent presence_request > task > message > other；
turn.reply 的 route agent_loop > task_request > direct_chat，reply_mode 與 addressed 只要有一段對她說就 respond／True；
request_two_places 的 target_location 填第一個字面地點，其餘留到拆分時處理。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

COMPOUND_CARDS = frozenset({"absent_request_two_places"})
COMPOUND_IDS: frozenset[str] = frozenset({"synthetic-en-presence.absent_intent-00405"})
COMPOUND_QID = {"turn.reply": "compound", "presence.absent_intent": "absent.compound"}


def is_compound(row: Mapping[str, Any]) -> bool:
    return row["sample_id"] in COMPOUND_IDS or str((row.get("meta") or {}).get("card", "")) in COMPOUND_CARDS
