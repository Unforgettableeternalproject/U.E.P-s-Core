"""r4b 最小差異成對樣本（defer 邊界）：句型完全相同，只換一個訊號。

三個家族（都是私聊 others=0、沒有前文）：
- ``referent``（指涉對象）：``clear``＝明講對象的請求 -> respond／task_request；``vague``＝同句型把對象換成 it／her／that 等代詞（無前文）-> defer。
- ``scope``（可逆性與影響範圍）：``safe``＝低風險或可復原（Archive the old backups、Delete that draft）-> respond；
  ``risky``＝同句型但範圍大或不可復原（Delete the old backups）-> defer。模型要學影響範圍與能不能復原，不是看 delete 這個字。
- ``name``（稱呼）：同一句加不加「Yuki,」答案相同——完整請求兩句都 respond（``complete``），資訊不足兩句都 defer（``incomplete``）。
  只放私聊：群組裡有沒有點名本來就會改變答案。

生成走檔案式後端：``export`` 寫 prompt，由外部（子代理）填回應，``build`` 把回應組成成對的列。回應格式是 JSON 陣列，
每個元素依家族帶不同鍵（見 ``FAMILIES``）。成對的兩列共用 ``meta.group_id``（切分時同側）與 ``meta.pair_id``。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

from .kinds import DEFAULT_ASSISTANT_NAME, make_row
from .synth import MAX_MESSAGE_CHARS, STYLES, SYSTEM_PROMPT, TOPICS, normalize

A = DEFAULT_ASSISTANT_NAME
RESPOND = {"reply_mode": "respond", "addressed": True, "route": "task_request", "compound": False}
DEFER = {"reply_mode": "defer", "addressed": True, "route": "task_request", "compound": False}

# 家族 -> (提示、鍵、各鍵的標籤)
FAMILIES: dict[str, dict[str, Any]] = {
    "referent": {
        "keys": ("clear", "vague"),
        "labels": {"clear": RESPOND, "vague": DEFER},
        "prompt": (
            "Each example is an object with two keys: \"clear\" and \"vague\". \"clear\" is one request addressed to the assistant that carries out ONE action "
            "(send, forward, book, call, order, schedule, open, share ...) and names its recipient, object or target outright (for example 'Send the report to Lena'). "
            "\"vague\" is the SAME sentence with the same structure and the same verb, but every outright name is replaced by a bare pronoun or demonstrative "
            "(it, her, him, them, that, this, there ...), so that, with no earlier conversation, nobody could tell what or who is meant (for example 'Send it to her'). "
            "Change only the referents; keep everything else word for word."
        ),
    },
    "scope": {
        "keys": ("safe", "risky"),
        "labels": {"safe": RESPOND, "risky": DEFER},
        "prompt": (
            "Each example is an object with two keys: \"safe\" and \"risky\". Both are one request addressed to the assistant with every detail stated, built on the SAME sentence "
            "structure. \"safe\" asks for a small, low-impact or easily reversible action (archive the old backups, delete that single draft, move one file to the trash folder, "
            "cancel one trial reminder, unsubscribe from one newsletter). \"risky\" asks for the same kind of action but with wide scope or no way back "
            "(delete the old backups for good, wipe every draft and empty the trash, erase the whole folder permanently, cancel all reminders). "
            "Include some pairs where both sentences use the verb 'delete' or 'remove' (a single recoverable item versus a large or permanent deletion), so the difference is "
            "scope and reversibility, not the word. Change only what is needed to change the scope."
        ),
    },
    "name": {
        "keys": ("base",),
        "labels": {"base": RESPOND},
        "prompt": (
            "Each example is an object with one key: \"base\", one request to the assistant written WITHOUT the assistant's name (the code adds 'Yuki,' in front for the second copy). "
            "The request must be a complete, directly executable single action with all details stated (recipient, content, time, item or file). "
            "Do not start it with a name, do not include the word Yuki."
        ),
    },
    "name_incomplete": {
        "keys": ("base",),
        "labels": {"base": DEFER},
        "prompt": (
            "Each example is an object with one key: \"base\", one request to the assistant written WITHOUT the assistant's name (the code adds 'Yuki,' in front for the second copy). "
            "The request names a clear action but lacks something it cannot proceed without (no recipient, no time, no item, which one), so the assistant must ask first. "
            "Do not hint at the missing detail. Do not start it with a name, do not include the word Yuki."
        ),
    },
}


# ---- r4c：passive 三條件的最小差異成對樣本（群組）與「她剛回答完、接著沒點名提問」----
PASSIVE = {"reply_mode": "passive", "addressed": False, "route": "ignore", "compound": False}
PASS = {"reply_mode": "pass", "addressed": False, "route": "ignore", "compound": False}
CAPABILITY_KEYS = ("messaging", "calling", "booking", "files", "timer_alarm", "media_control", "host_control", "device_control")
CTX_ITEM = ("\"context\" is a list of exactly two earlier lines, [{\"speaker\": \"User\", \"text\": ...}, {\"speaker\": \"Assistant\", \"text\": ...}]: "
            "the user asked something and the assistant (Yuki) answered with one concrete piece of information (a time, an amount, a route, a price, a recipe quantity, a date)")
R4C_FAMILIES: dict[str, str] = {
    "info": (
        "Each example is an object with keys \"context\", \"live\" and \"settled\". " + CTX_ITEM + ". "
        "\"live\" and \"settled\" are two different lines that one group member says to the others (never to the assistant, never naming it) right after that answer. "
        "\"live\": someone points out that the information the assistant gave may be wrong or outdated and they are about to act on it right now (leave, order, cook, pay, submit). "
        "\"settled\": the SAME kind of remark, but they have already double-checked it themselves, already fixed it or already finished, so nothing is left to correct. "
        "Keep the two lines close in wording; only the situation (still in use vs settled) differs."
    ),
    "answerable": (
        "Each example is an object with keys \"answerable\" and \"unseen\". Both are one question asked to the room (nobody in particular, no name) by a person talking with others, with the SAME sentence shape. "
        "\"answerable\" is a factual question an online assistant could answer (opening hours, a date, a conversion, a distance, a schedule, a definition, who starred in something). "
        "\"unseen\" is the same shape but about something only the people present can see or know in the moment (where somebody left their keys, whether the stove is still on, who is at the door, how loud the street is, what time it is on the wall clock). "
        "Do not name the assistant."
    ),
    "capreq": (
        "Each example is an object with keys \"message\" and \"category\". \"message\" is a request made to the whole room (nobody in particular, no name, not addressed to the assistant) that needs exactly one kind of ability. "
        "\"category\" is one of: messaging (text or email someone), calling (phone someone), booking (reserve, book, order), files (open, save, rename, share a file), timer_alarm (timer, alarm, reminder, calendar), "
        "media_control (volume, music, mute), host_control (shut down, restart, lock the computer), device_control (a physical device such as a light, lock, thermostat or window). "
        "Spread the categories evenly, device_control included. Write the request as something a person says aloud to friends or family around them, for example 'Could someone text Dana that we are running late?'."
    ),
    "followup": (
        "Each example is an object with keys \"context\" and \"message\". " + CTX_ITEM + ". "
        "\"message\" is something a group member says to the room right after that answer, a factual question asked to nobody in particular (no name) that stays on the same topic and that an online assistant could answer "
        "(for example when something opens, how long it takes, what it costs, what the next step is). It is not aimed at a companion and not a request to anyone by name."
    ),
}
FAMILIES.update({k: {"keys": (), "labels": {}, "prompt": v} for k, v in R4C_FAMILIES.items()})


def build_prompt(family: str, style: str, topic: str, n: int) -> str:
    spec = FAMILIES[family]
    return "\n".join([
        f"The assistant is always called \"{A}\". " + ("The people are in a room together." if family in R4C_FAMILIES else "The speaker is alone with the assistant (no other people, no earlier conversation)."),
        f"Write {n} DIFFERENT examples. Topic hint: {topic}. Style: {STYLES[style]}.",
        spec["prompt"],
        f"Each sentence at most {MAX_MESSAGE_CHARS} characters. Vary people, objects and sentence shapes widely. Do not include any label or explanation.",
    ])


def export(family: str, pairs: int, per_call: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    styles = list(STYLES)
    calls = []
    left, idx = pairs, 0
    while left > 0:
        n = min(per_call, left)
        calls.append({
            "call_id": f"pairs.{family}-{idx:04d}", "family": family, "n": n,
            "system": SYSTEM_PROMPT, "user": build_prompt(family, styles[idx % len(styles)], rng.choice(TOPICS), n),
            "style": styles[idx % len(styles)],
        })
        left -= n
        idx += 1
    return calls


def _named(base: str) -> str:
    first, _, rest = base.partition(" ")
    if first not in ("I", "I'm", "I'll", "I'd") and not first.isupper():
        first = first[:1].lower() + first[1:]
    return f"{A}, {first} {rest}".strip()


def build(family: str, responses: dict[str, str], calls: list[dict[str, Any]], id_prefix: str) -> list[dict[str, Any]]:
    """回應 -> 成對的列。無法解析、含中文、過長或兩句相同的項目會被丟棄。"""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    spec = FAMILIES[family]
    if family in R4C_FAMILIES:
        return _build_r4c(family, responses, calls, id_prefix)
    pair_no = 0
    for call in calls:
        text = responses.get(call["call_id"])
        if text is None:
            continue
        try:
            raw = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
        except json.JSONDecodeError:
            continue
        for it in raw:
            if not isinstance(it, dict):
                continue
            if family in ("name", "name_incomplete"):
                base = " ".join(str(it.get("base", "")).split())
                if not base or "yuki" in base.lower():
                    continue
                msgs = {"plain": base, "named": _named(base)}
                labels = {"plain": spec["labels"]["base"], "named": spec["labels"]["base"]}
            else:
                msgs = {k: " ".join(str(it.get(k, "")).split()) for k in spec["keys"]}
                labels = spec["labels"]
            if any((not m) or len(m) > MAX_MESSAGE_CHARS or re.search(r"[一-鿿]", m) for m in msgs.values()):
                continue
            keys = [normalize(m) for m in msgs.values()]
            if len(set(keys)) < len(keys) or any(k in seen for k in keys):
                continue
            seen.update(keys)
            pid = f"{id_prefix}-{pair_no:04d}"
            pair_no += 1
            for k, m in msgs.items():
                label = dict(labels[k])
                meta = {"card": f"pair_{family}_{k}", "style": call["style"], "hard": True, "others": 0, "group_id": pid, "pair_id": pid, "pair_family": family}
                if label["reply_mode"] == "defer":
                    meta["label_rule"] = "route_follows_action"
                rows.append(make_row("turn.reply", {"message": m, "others": 0}, label, sample_id=f"{pid}-{k}", source="synthetic", meta=meta))
    return rows


def _build_r4c(family: str, responses: dict[str, str], calls: list[dict[str, Any]], id_prefix: str) -> list[dict[str, Any]]:
    from .kinds import laya_schema

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    n = 0
    for call in calls:
        text = responses.get(call["call_id"])
        if text is None:
            continue
        try:
            raw = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
        except json.JSONDecodeError:
            continue
        for it in raw:
            if not isinstance(it, dict):
                continue
            others = (1, 2, 3)[n % 3]
            pid = f"{id_prefix}-{n:04d}"
            ctx = None
            if family in ("info", "followup"):
                ctx = it.get("context")
                if (not isinstance(ctx, list) or len(ctx) != 2 or not all(isinstance(c, dict) and c.get("text") for c in ctx)
                        or [str(c.get("speaker", "")).lower() for c in ctx] != ["user", "assistant"]):
                    continue
                ctx = [{"speaker": "User", "text": " ".join(str(ctx[0]["text"]).split())},
                       {"speaker": "Assistant", "text": " ".join(str(ctx[1]["text"]).split())}]
            # (後綴, 訊息, 標籤, 前文, 能力)
            if family == "info":
                specs = [("live", str(it.get("live", "")), PASSIVE, ctx, None), ("settled", str(it.get("settled", "")), PASS, ctx, None)]
            elif family == "answerable":
                specs = [("answerable", str(it.get("answerable", "")), PASSIVE, None, None), ("unseen", str(it.get("unseen", "")), PASS, None, None)]
            elif family == "capreq":
                cat = str(it.get("category", ""))
                if cat not in CAPABILITY_KEYS:
                    continue
                msg = str(it.get("message", ""))
                with_caps = list(laya_schema.DEFAULT_CAPABILITIES) + (["device_control"] if cat == "device_control" else [])
                without = [c for c in laya_schema.DEFAULT_CAPABILITIES if c != cat]
                specs = [("with", msg, PASSIVE, None, with_caps), ("without", msg, PASS, None, without)]
            else:
                specs = [("q", str(it.get("message", "")), PASSIVE, ctx, None)]
            msgs = [" ".join(m.split()) for _, m, _, _, _ in specs]
            if any((not m) or len(m) > MAX_MESSAGE_CHARS or re.search(r"[一-鿿]", m) or "yuki" in m.lower() for m in msgs):
                continue
            keys = [normalize(m) + "|" + (normalize(" ".join(c["text"] for c in ctx)) if ctx else "") for m in msgs]
            if (family != "capreq" and len(set(keys)) < len(keys)) or any(k in seen for k in keys):
                continue
            seen.update(keys)
            for (suffix, _, label, c, caps), m in zip(specs, msgs):
                facts: dict[str, Any] = {"message": m, "others": others}
                if c:
                    facts["context"] = c
                if caps is not None:
                    facts["capabilities"] = caps
                meta = {"card": f"pair_{family}_{suffix}", "style": call["style"], "hard": True, "others": others, "group_id": pid,
                        **({"pair_id": pid, "pair_family": family} if len(specs) == 2 else {})}
                rows.append(make_row("turn.reply", facts, dict(label), sample_id=f"{pid}-{suffix}", source="synthetic", meta=meta))
            n += 1
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="r4b 最小差異成對樣本")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--family", required=True, choices=sorted(FAMILIES))
    e.add_argument("--pairs", type=int, required=True)
    e.add_argument("--per-call", type=int, default=4)
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--out", required=True)
    b = sub.add_parser("build")
    b.add_argument("--family", required=True, choices=sorted(FAMILIES))
    b.add_argument("--pairs", type=int, required=True)
    b.add_argument("--per-call", type=int, default=4)
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--responses", required=True)
    b.add_argument("--id-prefix", required=True)
    b.add_argument("--out", required=True)
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    calls = export(args.family, args.pairs, args.per_call, args.seed)
    if args.cmd == "export":
        Path(args.out).write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in calls), encoding="utf-8", newline="\n")
        print(json.dumps({"calls": len(calls), "pairs": args.pairs}))
        return 0
    responses = {}
    for line in Path(args.responses).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            responses[rec["call_id"]] = rec["response"]
    rows = build(args.family, responses, calls, args.id_prefix)
    Path(args.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    print(json.dumps({"rows": len(rows), "pairs": len(rows) // 2}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
