"""composite 引用 text 的持久來源介面（D110，方案 B）。

``$.steps[k].output.text`` 取自回合範圍結果來源（D33，只在記憶體）。為了讓待核准的子步驟跨重啟後仍能重建參數，
組裝層可注入一個可刪除的 blob store：Loop 只把 composite 宣告實際引用到的 ``(k, text)`` 存成 blob，
cursor 的逐步狀態列只記 ``{ref, hash, size}``，checkpoint／receipt 不放原文（D33 不變）。

續跑時讀 blob 並驗雜湊；blob 遺失、雜湊不符、超過單筆上限都維持 fail-closed（Run 以 ``intent_rejected`` 結束、
核准不消耗、不喚醒模型）。Run 進終態即刪除該 Run 的 blob，TTL 是兜底。

TTL 不得短於核准有效期：核准在 TTL 之後才到，blob 已過期，續跑會 fail-closed（核准不消耗、Run 失敗）。
核准的有效期由呼叫端在 ``record_approval(expiry=...)`` 給，Core 沒有預設值；目前唯一的 production 呼叫端（Console）給 10 分鐘。未注入 store 時行為與先前完全相同。

本模組只定義介面與常數；DB 實作在 ``uep.events.composite_blobs``，離線實作在 ``uep.fakes.composite_blobs``。
"""
from __future__ import annotations

from typing import Protocol

BLOB_FIELD = "text"  # 目前只有 text 取自非持久的回合範圍來源；result_ref 在 receipt（持久）
# 逐步狀態列中記錄 blob 引用的選用鍵（攤平成三個純量：checkpoint 的 check_safe_value 限巢狀 <= 4 層、字串 <= 64 字元的 ID／短碼）
BLOB_REF_KEY = "text_blob_ref"
BLOB_HASH_KEY = "text_blob_hash"
BLOB_SIZE_KEY = "text_blob_size"
BLOB_ROW_KEYS = (BLOB_REF_KEY, BLOB_HASH_KEY, BLOB_SIZE_KEY)
BLOB_REF_PREFIX = "cblob:"


def run_ref_prefix(run_id: str) -> str:
    """ref 的前綴約定：``cblob:<run_id>:``。Loop 讀 blob 前會驗 ref 屬於本 Run，store 的 ``delete_run`` 也依此前綴刪除。"""
    return f"{BLOB_REF_PREFIX}{run_id}:"
DEFAULT_BLOB_MAX_BYTES = 65536  # 單筆上限的預設（UTF-8 位元組）；超過者不落地、續跑時 fail-closed


class CompositeBlobStore(Protocol):
    """可刪除 blob 的最小介面；實作負責雜湊（HMAC，D42）與 TTL。"""

    def put(self, run_id: str, data: bytes) -> tuple[str, str]:
        """寫入並回傳 ``(ref, hash)``；失敗（例如沒有雜湊密鑰）拋例外，呼叫端視為未持久化。
        ``ref`` 必須以 ``run_ref_prefix(run_id)`` 開頭（``cblob:<run_id>:``）；``ref``／``hash`` 都要能進 checkpoint：
        <= 64 字元、字元集 ``[A-Za-z0-9_.:-]``。"""
        ...

    def get(self, ref: str) -> bytes | None:
        """讀回內容；已刪除、已過期或不存在回 None。"""
        ...

    def verify(self, data: bytes, stored_hash: str) -> bool:
        """內容是否與已存雜湊確定相符。"""
        ...

    def delete_run(self, run_id: str) -> int:
        """刪除該 Run 的所有 blob（冪等），回傳新刪除的筆數。"""
        ...
