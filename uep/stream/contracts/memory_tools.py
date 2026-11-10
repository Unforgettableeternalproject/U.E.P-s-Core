"""記憶工具契約：Access Layer（``uep.memory.access``）與 Memory adapter（``uep.stream.adapters.memory_echo``）共用的單一來源。

N5 去重：``member_recall`` 的 schema、成員標籤常數、工具結果字數上限與簽名檢查原本在兩邊各寫一份（靠測試對照防漂移）。
這裡不 import 任何子系統，也不依賴 ``uep`` 其他部分；Access Layer 為權威消費端並再匯出，adapter 只引用。
"""
from __future__ import annotations

import inspect
from typing import Any

MEMBER_RECALL = "member_recall"
MEMBER_RECALL_DESCRIPTION = (
    "查詢同一個伺服器裡某位成員在共享記憶中可被看到的內容（例如使用者問「你認識某某嗎？"
    "他最近在玩什麼？」）。member 填成員的名稱；query 可選，填想了解的主題，留空則取最近的內容。"
    "只在使用者明確問到特定成員時呼叫；查不到只代表目前沒有可用的共享內容。"
    "Look up what is shared about one named member of this server; call only when the "
    "user asks about that member."
)
MEMBER_RECALL_SCHEMA: dict[str, Any] = {
    "name": MEMBER_RECALL,
    "description": MEMBER_RECALL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "member": {"type": "string", "description": "成員名稱（顯示名稱或暱稱）。"},
            "query": {"type": "string", "description": "想了解的主題，可留空。"},
        },
        "required": ["member"],
    },
}
UNKNOWN_MEMBER_LABEL = "這位成員"
MEMBER_LABEL_MAX_CHARS = 40
DEFAULT_TOOL_MAX_CHARS = 1500
"""工具結果回給 LLM 的字數上限。deep_recall 的 top_k 可到 20、門檻比自動注入鬆（0.2 vs 0.45），
不截會把第二段續寫的 prompt 塞爆。"""

RETRACT_ABOUT_ME = "retract_about_me"
OPT_OUT_ABOUT_ME = "opt_out_about_me"
CORRECT_ABOUT_ME = "correct_about_me"
ABOUT_ME_TOOLS = (RETRACT_ABOUT_ME, OPT_OUT_ABOUT_ME, CORRECT_ABOUT_ME)
"""「關於我」自助工具：對象永遠是說話的人本人（actor 由宿主提供，參數不得帶 uid）。"""
ABOUT_ME_REF_DESCRIPTION = (
    "要處理的旁證編號 N（member_recall 結果中「〔旁證N〕」那行的數字）。"
    "只能指定你本人那次查詢（查詢對象是目前說話的人）結果裡的旁證；查別人的結果沒有可用編號。"
)
RETRACT_ABOUT_ME_SCHEMA = {
    "name": RETRACT_ABOUT_ME,
    "description": (
        "當使用者明確要求撤回「別人說的關於他自己」的某一條旁證時呼叫（例如「那句別人說我的話，"
        "請不要再提」）。ref 填 member_recall 結果裡該行的旁證編號。只影響之後的檢索，"
        "不會改寫既有回覆。使用者沒有明確要求就不要呼叫。"
        "Call only when the user explicitly asks to retract one statement others made about them."
    ),
    "parameters": {
        "type": "object",
        "properties": {"ref": {"type": "integer", "description": ABOUT_ME_REF_DESCRIPTION}},
        "required": ["ref"],
    },
}
OPT_OUT_ABOUT_ME_SCHEMA = {
    "name": OPT_OUT_ABOUT_ME,
    "description": (
        "當使用者明確要求「不要再在伺服器裡被提起／被查詢」，或要求恢復時呼叫。"
        "enabled=true 為退出（之後其他人查詢時，關於他的旁證與「最近在玩」那行都不再出現），"
        "false 為恢復。只影響之後的檢索。使用者沒有明確要求就不要呼叫。"
        "Call only on an explicit request to opt out of (or back into) being surfaced to others."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean", "description": "true 退出；false 恢復。"},
        },
        "required": ["enabled"],
    },
}
CORRECT_ABOUT_ME_SCHEMA = {
    "name": CORRECT_ABOUT_ME,
    "description": (
        "當使用者明確指出「別人說我的某條旁證不對」並說出正確說法時呼叫：該條旁證立即被遮蔽，"
        "並把使用者的更正存下。ref 填旁證編號，correction_text 用使用者自己的話寫出更正內容。"
        "只影響之後的檢索。使用者沒有明確更正就不要呼叫。"
        "Call only when the user explicitly says a statement others made about them is wrong."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {"type": "integer", "description": ABOUT_ME_REF_DESCRIPTION},
            "correction_text": {"type": "string", "description": "使用者提供的更正內容。"},
        },
        "required": ["ref", "correction_text"],
    },
}
ABOUT_ME_SCHEMAS = {
    RETRACT_ABOUT_ME: RETRACT_ABOUT_ME_SCHEMA,
    OPT_OUT_ABOUT_ME: OPT_OUT_ABOUT_ME_SCHEMA,
    CORRECT_ABOUT_ME: CORRECT_ABOUT_ME_SCHEMA,
}
ABOUT_UNSUPPORTED = "目前記憶引擎不支援這個操作。"
ABOUT_NO_CONTEXT = "目前無法確認你的身分或所在場域，無法處理。"
ABOUT_NO_REF = "目前無法指定對象，請先查詢相關內容再試。"
ABOUT_BAD_REF = "找不到這個編號對應的內容，請先查詢相關內容再試。"
ABOUT_REASON_TEXT = {
    "already_hidden": "這條內容先前已經處理過了。",
}


def about_me_text(name: str, res: dict[str, Any] | None) -> str:
    """引擎結果 -> 給 LLM 讀的文字：分辨不支援／被拒絕（帶 reason）／成功。"""
    if res is None:
        return ABOUT_UNSUPPORTED
    reason = str(res.get("reason") or "")
    if not res.get("ok"):
        return f"被拒絕或失敗（原因：{reason or '未註明'}），沒有任何變更。"
    if name == CORRECT_ABOUT_ME and res.get("correction_stored") is False:
        # 優先於 already_hidden：遮蔽已生效但內容沒存，必須請使用者重送
        return ("那條旁證已遮蔽，但更正內容未保存，需要請使用者重送一次更正；"
                "只影響之後的檢索。")
    if reason in ABOUT_REASON_TEXT:
        return ABOUT_REASON_TEXT[reason]
    if name == OPT_OUT_ABOUT_ME:
        if res.get("enabled"):
            return ("已設定退出：之後其他人查詢時，關於使用者的內容（含「最近在玩」）"
                    "都不會再出現；只影響之後的檢索。")
        return "已恢復：之後的檢索可再包含關於使用者的內容。"
    if name == CORRECT_ABOUT_ME:
        return "那條旁證已遮蔽，使用者的更正已保存；只影響之後的檢索。"
    return "已撤回那條旁證；只影響之後的檢索。"


def declares_param(fn: Any, name: str) -> bool:
    """``fn`` 的簽名是否明確宣告名為 ``name`` 的參數；只有 ``**kwargs`` 或簽名讀不到都算否。"""
    try:
        param = inspect.signature(fn).parameters.get(name)
    except (TypeError, ValueError):
        return False
    return param is not None and param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY,
    )
