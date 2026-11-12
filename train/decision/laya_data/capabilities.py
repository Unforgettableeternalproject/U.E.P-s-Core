"""能力清單（state.available_capabilities）的標註處理：預設清單、成對樣本、隨機清單、她自己說的話。

艾斯維爾裁決（r4a 修訂）：能力是模型的輸入，不是標註規則；route 照意圖分。
- 直接對她說的請求：route 一律照意圖標（「關燈」＝task_request），不看清單；能不能做由工具層判斷。成對樣本只差清單、標籤相同。
- 群組裡沒點名的請求：清單有對應能力 → passive（第 3 條），沒有 → pass。成對樣本只差清單、標籤切換。
多數樣本使用現行預設清單（不含 device_control，含其他系統內與通訊能力）；少數樣本隨機抽清單（只抽不影響標籤的變動）。

這個模組只做資料轉換，不改 ``resolve`` 的盲標邏輯；``resolve.run`` 在切分前呼叫 ``apply_capabilities``。
"""
from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Mapping
from typing import Any

import json
from pathlib import Path

from .kinds import laya_schema

CATEGORIES: tuple[str, ...] = laya_schema.CAPABILITY_CATEGORIES
DEFAULT: tuple[str, ...] = laya_schema.DEFAULT_CAPABILITIES
DEVICE = "device_control"

# 句子需要某類能力的關鍵字（粗略；只用來避免「隨機抽掉清單項目」時抽到句子需要的那一項）
NEEDS: dict[str, re.Pattern[str]] = {
    "calling": re.compile(r"\b(call|phone|ring|dial)\b", re.I),
    "booking": re.compile(r"\b(book|reserve|reservation|sign (me|us|them) up|register|registration|tickets?|order)\b", re.I),
    "messaging": re.compile(r"\b(text|e-?mail|message|tell|let .{1,20} know|inform|send|reply|dm)\b", re.I),
    "files": re.compile(r"\b(files?|folders?|documents?|downloads?|attachment|notes?|spreadsheet|pdf|save|rename|archive|draft|link|list)\b", re.I),
    "media_control": re.compile(r"\b(volume|music|playlist|play|pause|mute|song|track|podcast|speaker|tv)\b", re.I),
    "timer_alarm": re.compile(r"\b(timer|alarm|remind|reminder|calendar|schedule|appointment|wake)\b", re.I),
    "host_control": re.compile(r"\b(shut ?down|restart|reboot|sleep mode|log ?out|lock (the )?screen|computer|pc)\b", re.I),
}
# 實體裝置請求（動詞＋裝置詞）；只用來排除「隨機加上 device_control」的候選與掃描她自己說的話
_DEV = (r"(light|lamp|thermostat|heater|heating|air.?condition|\bac\b|\bfan\b|\bdoor|garage|oven|stove|cooker|microwave|window|curtain|blind|"
        r"vacuum|\btv\b|television|speaker|kettle|washer|washing machine|dryer|coffee maker|humidifier|projector|printer|plug|sprinkler|"
        r"camera|heat\b|temperature|porch|lock)")
_VERB = r"(turn|switch|shut|put on|put out|set|lock|unlock|open|close|dim|raise|lower|start|stop|run|kill|flip|crank|adjust)"
DEVICE_ACTION = re.compile(_VERB + r"\W+(?:\w+\W+){0,7}?" + _DEV, re.I)

SID = "synthetic-en-turn.reply-"
# 對她說、要求實體裝置動作的請求（逐筆確認）。每筆配成對：原句＝有 device_control（respond／task_request），
# 孿生句＝預設清單（respond／direct_chat，她回答做不到）。
DEVICE_REQUEST_IDS: frozenset[str] = frozenset(
    ["r4-mtturn-00014"] + [SID + n for n in (
        "00014", "00054", "00145", "00185", "00241", "00276", "00315", "00371", "00408", "00501", "00568",
        "00660", "00786", "00822", "00825", "single-00000", "single-00002", "single-00012", "single-00013",
    )]
)
# 裝置請求夾帶別的系統內動作，或是 defer／agent_loop：不做孿生（沒有單一乾淨的「做不到」標籤），只補上 device_control。
DEVICE_COMPOUND_IDS: frozenset[str] = frozenset(
    SID + n for n in (
        "compound-00002", "compound-00004", "compound-00005", "compound-00007", "compound-00009", "compound-00012",
        "compound-00013", "compound-00016", "compound-00019", "compound-00026", "compound-00029", "00768",
    )
)
# 群組裡沒點名、要求實體動作的請求（原標 pass）：配有 device_control 的孿生句標 passive（第 3 條）。
UNNAMED_DEVICE_IDS: frozenset[str] = frozenset(f"r4-pass-{n}" for n in ("00045", "00068", "00069", "00071", "00095"))
# 前文裡她主動提議或答應做實體操作的句子不改寫，補上 device_control 讓清單與前文一致（逐筆確認，排除泛稱句）。
_NOT_PROMISE = frozenset({SID + "00102", SID + "00350", SID + "00360"})

# 成對樣本只取「明確對她下的單一請求」卡片；依前文才成立的 respond（取消承諾、接續追問）不做孿生
PAIR_CARDS = frozenset({"private_task", "group_named_task", "filler_then_task", "respond_complete_request", "respond_lowrisk_request",
                        "respond_now_request", "host_control_request"})
# 實體裝置的 defer／task_request 請求（r3 能力審核挑出）：另產生一個配有 device_control 的孿生句，標籤相同（route 照意圖）。
DEVICE_SAME_LABEL_PAIRS: frozenset[str] = frozenset(
    ["r4-defer-00072", "r4-defer-00073", "r4-defer-00075", "r4-defer-00011", "r4-defer-00034", "r4-defer-00083", "r4-mtturn-00003"]
    + [SID + n for n in ("00696", "00109", "00766", "00769", "00767")]
)
SAME_LABEL_PER_CATEGORY = 4
GROUP_PASS_PER_CATEGORY = 6
RANDOM_DEVICE_RATE = 0.08
RANDOM_DROP_RATE = 0.08

# 盲標審核的人工紀錄（逐筆 sample_id，可重現）：drop＝盲標員之間不一致而剔除、
# twin_reject＝孿生句盲標與標籤不一致而不產生。檔案與程式一起入版控。
_OVERRIDES_PATH = Path(__file__).with_name("capability_overrides.json")


def load_overrides() -> dict[str, frozenset[str]]:
    data = json.loads(_OVERRIDES_PATH.read_text(encoding="utf-8")) if _OVERRIDES_PATH.exists() else {}
    return {k: frozenset(data.get(k, ())) for k in ("drop", "twin_reject")}


def unnamed_group_request(row: Mapping[str, Any]) -> bool:
    """群組裡沒點名也沒有前文的請求（在場有其他人、沒有 Yuki 稱呼、沒有可接續的前文）。"""
    st = row["state"]
    return (st.get("other_people_present", "none") != "none" and "yuki" not in str(st.get("message", "")).lower()
            and st.get("recent_context", "(none)") == "(none)")


def _hash01(key: str) -> float:
    return int.from_bytes(hashlib.sha1(key.encode()).digest()[:4], "big") / 2**32


def assistant_device_lines(row: Mapping[str, Any]) -> list[str]:
    """前文裡她（Assistant）主動提議／答應的實體裝置操作句。"""
    if row["sample_id"] in _NOT_PROMISE:
        return []
    ctx = str(row["state"].get("recent_context", ""))
    return [ln for ln in ctx.split("\n") if ln.startswith("Assistant:") and DEVICE_ACTION.search(ln)]


def needed_categories(row: Mapping[str, Any]) -> set[str]:
    text = f"{row['state'].get('message', '')} {row['state'].get('recent_context', '')}"
    return {c for c, pat in NEEDS.items() if pat.search(text)}


def set_caps(row: dict[str, Any], caps: list[str] | tuple[str, ...]) -> dict[str, Any]:
    state = {k: v for k, v in row["state"].items() if k != "available_capabilities"}
    # 欄位順序與 build_state_dict 一致：recent_context 之後、message 之前
    ordered: dict[str, Any] = {}
    for k, v in state.items():
        if k == "message":
            ordered["available_capabilities"] = laya_schema.format_capabilities(caps)
        ordered[k] = v
    return {**row, "state": ordered}


def caps_of(row: Mapping[str, Any]) -> set[str]:
    text = str(row["state"].get("available_capabilities", ""))
    return set() if text in ("", laya_schema.NO_CAPABILITIES) else {c.strip() for c in text.split(",")}


def _twin(row: dict[str, Any], suffix: str, caps: list[str], expected: dict[str, Any], rule: str) -> dict[str, Any]:
    twin = set_caps(row, caps)
    meta = {**row["meta"], "label_rule": rule, "pair_of": row["sample_id"]}
    return {**twin, "sample_id": f"{row['sample_id']}-{suffix}", "expected": expected, "meta": meta}


def apply_capabilities(rows: list[dict[str, Any]], seed: int = 0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """turn.reply 列加上能力清單，並產生成對樣本。回傳 (列, 統計)。absent 列不帶這個欄位。"""
    rng = random.Random(seed)
    ov = load_overrides()
    rows = [r for r in rows if r["sample_id"] not in ov["drop"]]
    stats: dict[str, Any] = {"default": 0, "device_pairs": 0, "device_compound_caps": 0, "assistant_device_context": 0,
                             "unnamed_device_pairs": 0, "same_label_device_pairs": 0, "category_pairs": {}, "group_pass_pairs": {}, "random_device": 0, "random_drop": 0}
    base: list[dict[str, Any]] = []
    twins: list[dict[str, Any]] = []
    assistant_ids: list[str] = []
    cat_pool: dict[str, list[dict[str, Any]]] = {c: [] for c in NEEDS}
    group_pool: dict[str, list[dict[str, Any]]] = {c: [] for c in NEEDS}
    for r in rows:
        if r["kind"] != "turn.reply":
            base.append(r)
            continue
        sid = r["sample_id"]
        dev_lines = assistant_device_lines(r)
        if sid in DEVICE_REQUEST_IDS:
            twins.append(_twin(r, "nodev", list(DEFAULT), dict(r["expected"]), "capability_pair"))  # 只差清單、標籤相同
            r = set_caps(r, [*DEFAULT, DEVICE])
            stats["device_pairs"] += 1
        elif sid in DEVICE_SAME_LABEL_PAIRS:
            twins.append(_twin(r, "dev", [*DEFAULT, DEVICE], dict(r["expected"]), "capability_pair"))
            r = set_caps(r, list(DEFAULT))
            stats["same_label_device_pairs"] += 1
        elif sid in DEVICE_COMPOUND_IDS:
            r = set_caps(r, [*DEFAULT, DEVICE])
            stats["device_compound_caps"] += 1
        elif dev_lines:
            r = set_caps(r, [*DEFAULT, DEVICE])
            stats["assistant_device_context"] += 1
            assistant_ids.append(sid)
        elif sid in UNNAMED_DEVICE_IDS:
            twins.append(_twin(r, "dev", [*DEFAULT, DEVICE], {**r["expected"], "reply_mode": "passive", "addressed": False, "route": "ignore"}, "capability_pair"))
            r = set_caps(r, list(DEFAULT))
            stats["unnamed_device_pairs"] += 1
        else:
            r = set_caps(r, list(DEFAULT))
            stats["default"] += 1
            exp = r["expected"]
            need = needed_categories(r)
            if len(need) == 1 and not DEVICE_ACTION.search(r["state"]["message"]):
                cat = next(iter(need))
                if exp.get("reply_mode") == "respond" and exp.get("route") == "task_request" and r["meta"].get("card") in PAIR_CARDS:
                    cat_pool[cat].append(r)
                elif exp.get("reply_mode") == "passive" and r["meta"].get("card") == "passive_unnamed_request_do" and cat != "query":
                    group_pool[cat].append(r)
        base.append(r)
    # 其他能力類別的成對樣本。直接對她說的請求：配上沒有該能力的清單，標籤不變（route 照意圖）；
    # 群組裡沒點名的請求（passive）：配上沒有該能力的清單 -> pass。
    picked: set[str] = set()
    for cat, pool in cat_pool.items():
        pool = sorted(pool, key=lambda x: _hash01(x["sample_id"] + cat))[:SAME_LABEL_PER_CATEGORY]
        for r in pool:
            if f"{r['sample_id']}-no-{cat}" in ov["twin_reject"]:
                continue
            twins.append(_twin(r, f"no-{cat}", [c for c in DEFAULT if c != cat], dict(r["expected"]), "capability_pair"))
            picked.add(r["sample_id"])
        stats["category_pairs"][cat] = len(pool)
    for cat, pool in group_pool.items():
        pool = sorted(pool, key=lambda x: _hash01(x["sample_id"] + cat))[:GROUP_PASS_PER_CATEGORY]
        for r in pool:
            if f"{r['sample_id']}-no-{cat}" in ov["twin_reject"]:
                continue
            twins.append(_twin(r, f"no-{cat}", [c for c in DEFAULT if c != cat], {**r["expected"], "reply_mode": "pass", "addressed": False, "route": "ignore"}, "capability_pair"))
            picked.add(r["sample_id"])
        stats["group_pass_pairs"][cat] = len(pool)
    # 隨機清單：只做不影響標籤的變動（加上無關的 device_control，或抽掉句子用不到的類別）
    out: list[dict[str, Any]] = []
    for r in base:
        if r["kind"] != "turn.reply" or r["sample_id"] in picked or r["meta"].get("label_rule") == "capability_pair":
            out.append(r)
            continue
        if caps_of(r) != set(DEFAULT):
            out.append(r)
            continue
        u = rng.random()
        text = f"{r['state']['message']} {r['state'].get('recent_context', '')}"
        if u < RANDOM_DEVICE_RATE and not DEVICE_ACTION.search(text) and r["sample_id"] not in UNNAMED_DEVICE_IDS:
            r = set_caps(r, [*DEFAULT, DEVICE])
            stats["random_device"] += 1
        elif u < RANDOM_DEVICE_RATE + RANDOM_DROP_RATE:
            free = [c for c in DEFAULT if c not in needed_categories(r) and c != "query"]
            if free:
                drop = rng.choice(free)
                r = set_caps(r, [c for c in DEFAULT if c != drop])
                stats["random_drop"] += 1
        out.append(r)
    twins = [t for t in twins if t["sample_id"] not in ov["twin_reject"]]
    stats["assistant_device_context_ids"] = assistant_ids
    stats["twins"] = len(twins)
    return out + twins, stats
