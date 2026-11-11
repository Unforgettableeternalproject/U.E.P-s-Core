"""lore.* 原生工具：註冊表宣告、readiness、executor（fake HTTP，不打真服務）。

真服務冒煙測試只在設了 ``UEP_LORE_URL``／``UEP_LORE_TOKEN``／（``UEP_LORE_DEV_VAULT`` 或 ``UEP_LORE_VAULTS``）時才跑。
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr

from uep.adapters.tool import CAPABILITY_CEILING, ToolCall, ToolResponseLost
from uep.config import UepSettings
from uep.tools.catalog.lore import (
    LIST_INPUT_SCHEMA,
    LORE_EXECUTOR,
    LORE_READ_TOOLS,
    LORE_TOOLS,
    TOOL_DELETE,
    TOOL_GET,
    TOOL_LIST,
    TOOL_RECALL,
    TOOL_UPDATE,
    TOOL_WRITE,
    lore_entries,
    lore_write_entries,
    with_lore,
)
from uep.tools.catalog.memory import memory_registry
from uep.tools.executors.lore import (
    LoreConfig,
    LoreTimeout,
    LoreToolExecutor,
    LoreUnreachable,
    urllib_transport,
)
from uep.tools.registry import ToolRegistry, ToolRegistryError

TOKEN = "tok-secret-123"
DEV = "github.com/example/project"
LORE_VAULT = "lore/main"


def make_config(**overrides: Any) -> LoreConfig:
    base: dict[str, Any] = {
        "url": "http://lore.test", "token": TOKEN, "dev_vault": DEV, "vaults": (LORE_VAULT, "lore/other"),
        "default_space": "lore", "author": "測試角色", "timeout_s": 3.0, "max_chars": 2000, "delete_enabled": True,
    }
    base.update(overrides)
    return LoreConfig(**base)


class FakeHttp:
    """記錄請求的 fake 傳輸層：依路徑回固定回應，或依設定拋傳輸層例外。"""

    def __init__(self, responses: dict[str, tuple[int, Any]] | None = None, raises: Exception | None = None) -> None:
        self.responses = responses or {}
        self.raises = raises
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, dict(headers), json.loads(body.decode("utf-8"))))
        if self.raises is not None:
            raise self.raises
        entry = self.responses.get(url.rsplit("/", 1)[-1], (200, {}))
        if isinstance(entry, list):  # 同一路徑依序回不同回應（兩步式刪除）
            entry = entry.pop(0)
        status, payload = entry
        return status, json.dumps(payload, ensure_ascii=False).encode("utf-8")


def call(tool: str, params: dict[str, Any], action_id: str = "act_1") -> ToolCall:
    idem = "none" if tool in (TOOL_WRITE, TOOL_UPDATE) else "native"
    return ToolCall(
        action_id=action_id, tool=tool, params=params, params_hash="h",
        side_effect_class="external_effect" if idem == "none" else "internal_read", idempotency=idem,  # type: ignore[arg-type]
    )


_COUNTER = itertools.count(1)


def run(executor: LoreToolExecutor, tool: str, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    action_id = f"act_{next(_COUNTER)}"  # 每次呼叫用新的 action（native 冪等會重放同 action 的 receipt）
    receipt = executor.execute(call(tool, params, action_id))
    return receipt, executor.outcome(action_id) or {}


# ---- 註冊表宣告 ----

def test_read_entries_classification_and_strict_schema():
    entries = {e.name: e for e in lore_entries() if e.spec.capability == "lore.read"}
    assert set(entries) == set(LORE_READ_TOOLS)
    for entry in entries.values():
        assert entry.spec.side_effect_class == "internal_read"
        assert entry.spec.approval_level == "none"
        assert entry.concurrency == "parallel_safe"
        assert entry.spec.capability == "lore.read"
        assert entry.spec.executor == LORE_EXECUTOR
        assert entry.requires == ("lore",)
        assert entry.descriptor.input_schema["additionalProperties"] is False
        assert entry.output_schema is not None and entry.output_schema["additionalProperties"] is False
        assert entry.descriptor.usage_hint and entry.descriptor.description


def test_schema_space_enum_only_dev_and_lore_and_no_delete():
    for entry in lore_entries():
        space = entry.descriptor.input_schema["properties"].get("space")
        if space is not None:
            assert space["enum"] == ["dev", "lore"]
        assert "author" not in entry.descriptor.input_schema["properties"]  # 寫入者名稱只來自設定
    assert not any(name in LORE_TOOLS for name in ("lore.undelete", "lore.upload", "lore.download"))


def test_capability_tags_in_closed_set():
    assert CAPABILITY_CEILING["lore.read"] == "internal_read"
    assert "lore.write" in CAPABILITY_CEILING


def test_write_entries_registered_honestly_native():
    """寫入如實標 external_effect；免核准靠 native=True（RA-2a），不是調低分類。"""
    entries = {e.name: e for e in lore_entries()}
    assert {TOOL_WRITE, TOOL_UPDATE, TOOL_DELETE} <= set(entries)
    for name in (TOOL_WRITE, TOOL_UPDATE, TOOL_DELETE):
        spec = entries[name].spec
        assert (spec.side_effect_class, spec.approval_level, spec.native, spec.capability) == (
            "external_effect", "none", True, "lore.write",
        )
        assert spec.idempotency == "none" and entries[name].concurrency == "serial"
    ToolRegistry.build(lore_entries(), known_modules={"lore"})  # RA-2／2a／6 通過


def test_write_not_exempt_without_native():
    """native 只豁免核准，沒有 native 的 external_effect＋none 仍被 RA-2 拒絕（不能靠 lore.write 標籤繞過）。"""
    import dataclasses

    entry = lore_write_entries()[0]
    plain = dataclasses.replace(entry, spec=dataclasses.replace(entry.spec, native=False))
    with pytest.raises(ToolRegistryError) as exc:
        ToolRegistry.build([plain], known_modules={"lore"})
    assert exc.value.rule == "RA-2"


# ---- readiness / 組裝 ----

def test_unconfigured_readiness_unavailable_and_not_projected():
    registry = with_lore(memory_registry(), configured=False)
    for name in LORE_TOOLS:
        assert registry.readiness(name) == "unavailable"
    assert not {"lore.read", "lore.write"} & registry.enabled_tags()
    assert "memory.read" in registry.enabled_tags()  # 不影響既有工具


def test_configured_readiness_ready_and_idempotent():
    registry = with_lore(memory_registry(), configured=True, delete_enabled=True)
    assert all(registry.readiness(n) == "ready" for n in LORE_TOOLS)
    assert {"lore.read", "lore.write"} <= registry.enabled_tags()
    assert with_lore(registry, configured=False) is registry  # 已含 lore：原樣回傳
    assert "lore.recall" not in memory_registry().entries()  # 不改動傳入的實例


def test_delete_readiness_gated_by_separate_switch():
    off = with_lore(memory_registry(), configured=True)  # delete_enabled 預設 False
    assert off.readiness(TOOL_DELETE) == "unavailable"
    assert off.readiness(TOOL_WRITE) == "ready" and off.readiness(TOOL_RECALL) == "ready"
    assert with_lore(memory_registry(), configured=True, delete_enabled=True).readiness(TOOL_DELETE) == "ready"
    assert with_lore(memory_registry(), configured=False, delete_enabled=True).readiness(TOOL_DELETE) == "unavailable"


def test_update_and_write_guidance_mentions_supersedes():
    entries = {e.name: e for e in lore_entries()}
    update = entries[TOOL_UPDATE].descriptor
    assert "supersedes" in update.description and "supersedes" in update.usage_hint and "Bernie" in update.description
    assert "supersedes" in entries[TOOL_WRITE].descriptor.input_schema["properties"]
    assert entries[TOOL_DELETE].descriptor.input_schema["additionalProperties"] is False
    for name in (TOOL_WRITE, TOOL_UPDATE, TOOL_DELETE):
        out = entries[name].output_schema["properties"]["review_state"]
        assert out["enum"] == ["none", "pending", "pending_delete", "verified", "rejected"]


def test_config_from_settings(monkeypatch: pytest.MonkeyPatch):
    for key in ("UEP_LORE_URL", "UEP_LORE_TOKEN", "UEP_LORE_DEV_VAULT", "UEP_LORE_VAULTS"):
        monkeypatch.delenv(key, raising=False)
    empty = LoreConfig.from_settings(UepSettings(_env_file=None))
    assert not empty.configured and empty.default_space == "lore"
    settings = UepSettings(
        _env_file=None, UEP_LORE_URL="http://x", UEP_LORE_TOKEN=SecretStr("t"), UEP_LORE_VAULTS="a, b,*,",
    )
    cfg = LoreConfig.from_settings(settings)
    assert cfg.configured and cfg.vaults == ("a", "b")  # 去空白、丟掉 "*"
    assert cfg.delete_enabled and empty.delete_enabled  # lore.delete 預設開啟
    assert "token" not in repr(cfg)  # token 不進 repr
    assert not LoreConfig(url="ftp://x", token="t", dev_vault="d").configured


# ---- 唯讀工具 ----

def test_recall_success_and_request_shape():
    http = FakeHttp({"recall": (200, {"items": [
        {"id": "n1", "kind": "note", "vault": LORE_VAULT, "title": "標題", "summary": "摘要", "score": 0.5,
         "updated": "2026-11-11", "author": "a", "secret_field": "x"},
    ], "degraded": False})})
    ex = LoreToolExecutor(make_config(), transport=http)
    receipt, outcome = run(ex, TOOL_RECALL, {"query": "世界觀", "limit": 3})
    assert receipt.succeeded and receipt.meta["status"] == "executed"
    url, headers, body = http.calls[0]
    assert url == "http://lore.test/v1/recall"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert body["space"] == "lore" and body["vault"] == LORE_VAULT and body["query"] == "世界觀" and body["limit"] == 3
    data = json.loads(outcome["result"])
    assert data["items"][0]["title"] == "標題" and "secret_field" not in data["items"][0]
    assert TOKEN not in outcome["result"] and TOKEN not in repr(receipt)


def test_get_truncates_long_body_and_flags_it():
    http = FakeHttp({"get": (200, {"items": [{"id": "n1", "kind": "note", "title": "t", "body": "字" * 5000}], "missing": ["zz"]})})
    ex = LoreToolExecutor(make_config(max_chars=1000), transport=http)
    _, outcome = run(ex, TOOL_GET, {"ids": ["n1", "zz"]})
    assert len(outcome["result"]) <= 1100  # 全文有長度上限，整體不塞爆 context
    data = json.loads(outcome["result"])
    assert data["items"][0]["truncated"] is True and len(data["items"][0]["body"]) == 600
    assert data["missing"] == ["zz"]


def test_list_drops_items_over_total_budget():
    items = [{"id": f"n{i}", "title": "標" * 100, "summary": "摘" * 300} for i in range(30)]
    ex = LoreToolExecutor(make_config(max_chars=1500), transport=FakeHttp({"list": (200, {"items": items, "next_cursor": "c1"})}))
    _, outcome = run(ex, TOOL_LIST, {"limit": 20, "title": "標"})
    data = json.loads(outcome["result"])
    assert len(outcome["result"]) <= 1500 and data["truncated"] is True and 0 < len(data["items"]) < 30


def test_control_characters_stripped():
    http = FakeHttp({"recall": (200, {"items": [{"id": "n1", "title": "a\x00b\x1b[31m", "summary": "x"}]})})
    _, outcome = run(LoreToolExecutor(make_config(), transport=http), TOOL_RECALL, {"query": "q"})
    assert "\x00" not in outcome["result"] and "\x1b" not in outcome["result"]


# ---- 驗證錯誤 ----

@pytest.mark.parametrize(("tool", "params"), [
    (TOOL_RECALL, {}),
    (TOOL_RECALL, {"query": "  "}),
    (TOOL_RECALL, {"query": "q", "limit": 99}),
    (TOOL_GET, {"ids": []}),
    (TOOL_GET, {"ids": ["a"] * 6}),
    (TOOL_LIST, {"limit": 0}),
    (TOOL_LIST, {"topics": [""]}),
])
def test_invalid_args_rejected_without_http(tool: str, params: dict[str, Any]):
    http = FakeHttp()
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=http), tool, params)
    assert not receipt.succeeded and receipt.error_code == "failed" and not http.calls
    assert json.loads(outcome["result"])["error"] == "invalid_args"


def test_service_validation_error_surfaced_without_token():
    http = FakeHttp({"recall": (400, {"error": {"code": "invalid_request", "message": "query 不可為空"}})})
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=http), TOOL_RECALL, {"query": "q"})
    data = json.loads(outcome["result"])
    assert not receipt.succeeded and data["error"] == "invalid_request" and data["http_status"] == 400


def test_auth_failure_reported_as_denied():
    http = FakeHttp({"recall": (401, {"error": {"code": "unauthorized", "message": "bad"}})})
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=http), TOOL_RECALL, {"query": "q"})
    assert receipt.error_code == "denied" and TOKEN not in outcome["result"]


# ---- 服務不可達（degraded） ----

def test_unreachable_is_degraded_failure():
    ex = LoreToolExecutor(make_config(), transport=FakeHttp(raises=LoreUnreachable()))
    receipt, outcome = run(ex, TOOL_RECALL, {"query": "q"})
    data = json.loads(outcome["result"])
    assert not receipt.succeeded and data["degraded"] is True and data["error"] == "unreachable"


def test_read_timeout_is_failure_not_lost():
    ex = LoreToolExecutor(make_config(), transport=FakeHttp(raises=LoreTimeout()))
    receipt, outcome = run(ex, TOOL_LIST, {})
    assert not receipt.succeeded and json.loads(outcome["result"])["degraded"] is True


def test_real_transport_dns_failure_maps_to_unreachable():
    # .invalid 保證解析不到（Windows 對已關閉埠的拒絕連線會拖到逾時，不穩定）
    ex = LoreToolExecutor(make_config(url="http://lore.invalid", timeout_s=5.0))
    receipt, outcome = run(ex, TOOL_RECALL, {"query": "q"})
    assert not receipt.succeeded and json.loads(outcome["result"])["error"] == "unreachable"


# ---- space／vault 範圍 ----

@pytest.mark.parametrize("space", ["personal", "other", "*", ""])
def test_space_outside_allowlist_denied(space: str):
    http = FakeHttp()
    receipt, _ = run(LoreToolExecutor(make_config(), transport=http), TOOL_RECALL, {"query": "q", "space": space})
    assert receipt.error_code == "denied" and not http.calls


def test_dev_space_uses_project_vault_only():
    http = FakeHttp({"recall": (200, {"items": []})})
    ex = LoreToolExecutor(make_config(), transport=http)
    run(ex, TOOL_RECALL, {"query": "q", "space": "dev"})
    assert http.calls[0][2]["vault"] == DEV and http.calls[0][2]["space"] == "dev"
    receipt, _ = run(ex, TOOL_RECALL, {"query": "q", "space": "dev", "vault": LORE_VAULT})
    assert receipt.error_code == "denied" and len(http.calls) == 1


def test_lore_vault_whitelist_and_no_wildcard():
    http = FakeHttp({"recall": (200, {"items": []})})
    ex = LoreToolExecutor(make_config(), transport=http)
    run(ex, TOOL_RECALL, {"query": "q", "vault": "lore/other"})
    assert http.calls[0][2]["vault"] == "lore/other"
    for bad in ("lore/secret", "*"):
        receipt, _ = run(ex, TOOL_RECALL, {"query": "q", "vault": bad})
        assert receipt.error_code == "denied"
    assert len(http.calls) == 1


def test_default_space_follows_config():
    http = FakeHttp({"recall": (200, {"items": []})})
    run(LoreToolExecutor(make_config(default_space="dev"), transport=http), TOOL_RECALL, {"query": "q"})
    assert http.calls[0][2]["space"] == "dev"


def test_delete_and_unknown_tools_denied():
    http = FakeHttp()
    ex = LoreToolExecutor(make_config(), transport=http, allow_write=True)
    for tool in ("lore.undelete", "lore.upload", "recall"):
        receipt, _ = run(ex, tool, {"ids": ["n1"]})
        assert receipt.error_code == "denied" and not receipt.succeeded
    assert not http.calls


# ---- 未設定 ----

def test_unconfigured_executor_never_calls_http():
    http = FakeHttp()
    receipt, outcome = run(LoreToolExecutor(LoreConfig(), transport=http), TOOL_RECALL, {"query": "q"})
    assert not receipt.succeeded and json.loads(outcome["result"])["degraded"] is True and not http.calls


# ---- 寫入（executor 先備；工具註冊待 native 豁免合入） ----

def test_write_disabled_by_default():
    http = FakeHttp()
    receipt, _ = run(LoreToolExecutor(make_config(), transport=http), TOOL_WRITE, {"title": "t", "body": "b"})
    assert receipt.error_code == "denied" and not http.calls


def test_write_uses_config_author_and_ignores_llm_author():
    http = FakeHttp({"write": (201, {"id": "n9", "updated": "u1", "author": "測試角色", "duplicates": []})})
    ex = LoreToolExecutor(make_config(), transport=http, allow_write=True)
    receipt, outcome = run(ex, TOOL_WRITE, {"title": "t", "body": "b", "topics": ["x"], "author": "冒名"})
    body = http.calls[0][2]
    assert body["author"] == "測試角色" and body["title"] == "t" and body["topics"] == ["x"]
    assert receipt.succeeded and json.loads(outcome["result"])["id"] == "n9"


def test_write_timeout_raises_response_lost():
    ex = LoreToolExecutor(make_config(), transport=FakeHttp(raises=LoreTimeout()), allow_write=True)
    with pytest.raises(ToolResponseLost):
        ex.execute(call(TOOL_WRITE, {"title": "t", "body": "b"}))


def test_write_unreachable_is_plain_failure():
    ex = LoreToolExecutor(make_config(), transport=FakeHttp(raises=LoreUnreachable()), allow_write=True)
    receipt, outcome = run(ex, TOOL_WRITE, {"title": "t", "body": "b"})
    assert not receipt.succeeded and json.loads(outcome["result"])["degraded"] is True


def test_update_conflict_gives_hint_and_requires_a_change():
    http = FakeHttp({"update": (409, {"error": {"code": "version_conflict", "message": "已被修改"}})})
    ex = LoreToolExecutor(make_config(), transport=http, allow_write=True)
    receipt, outcome = run(ex, TOOL_UPDATE, {"id": "n1", "expected_updated": "u0", "body": "新"})
    assert not receipt.succeeded and "lore.get" in json.loads(outcome["result"])["hint"]
    assert http.calls[0][2]["expected_updated"] == "u0" and http.calls[0][2]["author"] == "測試角色"
    receipt, _ = run(ex, TOOL_UPDATE, {"id": "n1", "expected_updated": "u0"})  # 沒有任何修改欄位
    assert receipt.error_code == "failed" and len(http.calls) == 1


def test_write_scope_checked_like_reads():
    http = FakeHttp()
    ex = LoreToolExecutor(make_config(), transport=http, allow_write=True)
    receipt, _ = run(ex, TOOL_WRITE, {"title": "t", "body": "b", "space": "personal"})
    assert receipt.error_code == "denied" and not http.calls


# ---- 審核欄位（選填） ----

def test_review_markers_passed_through_on_reads_and_absent_is_fine():
    http = FakeHttp({"recall": (200, {"items": [
        {"id": "n1", "title": "a", "review": "unverified"}, {"id": "n2", "title": "b"},
    ]})})
    _, outcome = run(LoreToolExecutor(make_config(), transport=http), TOOL_RECALL, {"query": "q"})
    items = json.loads(outcome["result"])["items"]
    assert items[0]["review"] == "unverified" and "review" not in items[1]


def test_write_review_state_optional_and_validated():
    ex = LoreToolExecutor(make_config(), allow_write=True, transport=FakeHttp({"write": [
        (201, {"id": "n1"}), (201, {"id": "n2", "review_state": "pending"}), (201, {"id": "n3", "review_state": "weird"}),
    ]}))
    _, plain = run(ex, TOOL_WRITE, {"title": "t", "body": "b"})
    _, pending = run(ex, TOOL_WRITE, {"title": "t", "body": "b"})
    _, weird = run(ex, TOOL_WRITE, {"title": "t", "body": "b"})
    assert "review_state" not in plain and pending["review_state"] == "pending" and "review_state" not in weird


def test_write_supersedes_forwarded():
    http = FakeHttp({"write": (201, {"id": "n2"})})
    run(LoreToolExecutor(make_config(), transport=http, allow_write=True), TOOL_WRITE,
        {"title": "更正", "body": "b", "supersedes": "n1"})
    assert http.calls[0][2]["supersedes"] == "n1"


# ---- lore.delete（兩步式，executor 內部完成） ----

PLAN = (200, {"executed": False, "plan": {"id": "n1"}, "confirm_token": "CONFIRM-XYZ", "expires_at": "later"})


def test_delete_two_step_flow_hides_token():
    http = FakeHttp({"note_delete": [PLAN, (200, {"executed": True, "plan": {"id": "n1"}})]})
    ex = LoreToolExecutor(make_config(), transport=http, allow_write=True)
    receipt, outcome = run(ex, TOOL_DELETE, {"id": "n1", "reason": "重複", "space": "dev"})
    assert receipt.succeeded and len(http.calls) == 2
    first, second = http.calls[0][2], http.calls[1][2]
    assert "confirm_token" not in first and second["confirm_token"] == "CONFIRM-XYZ"
    assert {k: v for k, v in second.items() if k != "confirm_token"} == first  # 其餘參數完全相同
    assert first["space"] == "dev" and first["vault"] == DEV and first["reason"] == "重複"
    assert http.calls[0][0].endswith("/v1/note_delete")
    data = json.loads(outcome["result"])
    assert data["deleted"] is True and "CONFIRM-XYZ" not in outcome["result"] + repr(receipt)


def test_delete_in_lore_space_reports_pending():
    http = FakeHttp({"note_delete": [PLAN, (200, {"executed": False, "review_state": "pending_delete"})]})
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=http, allow_write=True), TOOL_DELETE, {"id": "n1"})
    data = json.loads(outcome["result"])
    assert receipt.succeeded and data["deleted"] is False and outcome["review_state"] == "pending_delete"
    assert http.calls[0][2]["space"] == "lore" and http.calls[0][2]["vault"] == LORE_VAULT
    assert "待 Bernie 核准" in data["note"] and "仍可見" in data["note"]


def test_delete_pending_delete_with_executed_true_is_not_deleted():
    # Vault 21cf10f 的實際回應：lore 的第二步 executed:true 但 review_state:pending_delete（請求受理，筆記未刪）
    http = FakeHttp({"note_delete": [PLAN, (200, {"executed": True, "plan": {"id": "n1"}, "review_state": "pending_delete"})]})
    ex = LoreToolExecutor(make_config(), transport=http, allow_write=True)
    receipt, outcome = run(ex, TOOL_DELETE, {"id": "n1"})
    data = json.loads(outcome["result"])
    assert receipt.succeeded and data["deleted"] is False and data["review_state"] == "pending_delete"
    assert data["note"] == "已送出刪除請求，待 Bernie 核准，原筆記目前仍可見。" and outcome["review_state"] == "pending_delete"
    assert "CONFIRM-XYZ" not in outcome["result"]


def test_delete_dev_space_real_delete_reports_none():
    http = FakeHttp({"note_delete": [PLAN, (200, {"executed": True, "review_state": "none"})]})
    _, outcome = run(LoreToolExecutor(make_config(), transport=http, allow_write=True), TOOL_DELETE, {"id": "n1", "space": "dev"})
    data = json.loads(outcome["result"])
    assert data["deleted"] is True and data["review_state"] == "none" and "note" not in data


# ---- UEP 審核契約：錯誤碼訊息、審核欄位、list 篩選 ----

CONTRACT_CASES = [
    (403, "uep_space_forbidden", "denied", "只能使用 dev 或 lore"),
    (403, "uep_not_author", "denied", "lore.write 另寫一篇並帶 supersedes"),
    (403, "uep_endpoint_forbidden", "denied", "不開放這個操作"),
    (409, "review_state_conflict", "failed", "已在待刪除中"),
]


@pytest.mark.parametrize(("status", "code", "kind", "text"), CONTRACT_CASES)
def test_contract_error_codes_give_clear_messages(status: int, code: str, kind: str, text: str):
    # 服務端原文帶 URL 與 token 也不得進結果：訊息是固定的繁中文字
    leaky = f"http://secret.example/v1 Bearer {TOKEN}"
    http = FakeHttp({"update": (status, {"error": {"code": code, "message": leaky}})})
    ex = LoreToolExecutor(make_config(), transport=http, allow_write=True)
    receipt, outcome = run(ex, TOOL_UPDATE, {"id": "n1", "expected_updated": "u0", "body": "新"})
    data = json.loads(outcome["result"])
    assert not receipt.succeeded and receipt.error_code == kind
    assert data["error"] == code and text in data["message"] and data["http_status"] == status
    assert "hint" not in data  # review_state_conflict 不套用版本衝突的提示
    assert TOKEN not in outcome["result"] and "secret.example" not in outcome["result"] and "http://" not in outcome["result"]


def test_review_state_conflict_on_delete_second_request():
    http = FakeHttp({"note_delete": [(409, {"error": {"code": "review_state_conflict", "message": "x"}})]})
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=http, allow_write=True), TOOL_DELETE, {"id": "n1"})
    assert not receipt.succeeded and "已在待刪除中" in json.loads(outcome["result"])["message"] and len(http.calls) == 1


def test_space_forbidden_message_on_reads_too():
    http = FakeHttp({"recall": (403, {"error": {"code": "uep_space_forbidden", "message": "personal"}})})
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=http), TOOL_RECALL, {"query": "q"})
    assert receipt.error_code == "denied" and "dev 或 lore" in json.loads(outcome["result"])["message"]


def test_version_conflict_keeps_existing_hint():
    http = FakeHttp({"update": (409, {"error": {"code": "version_conflict", "message": "x"}})})
    ex = LoreToolExecutor(make_config(), transport=http, allow_write=True)
    _, outcome = run(ex, TOOL_UPDATE, {"id": "n1", "expected_updated": "u0", "body": "新"})
    assert "lore.get" in json.loads(outcome["result"])["hint"]


def test_get_keeps_review_fields():
    item = {
        "id": "n1", "title": "t", "body": "內文", "review_state": "rejected", "reviewed_by": "Bernie",
        "reviewed_at": "2026-11-12T00:00:00Z", "review_reason": "內容不實" * 10, "review": "unverified",
    }
    http = FakeHttp({"get": (200, {"items": [item]})})
    _, outcome = run(LoreToolExecutor(make_config(), transport=http), TOOL_GET, {"ids": ["n1"]})
    got = json.loads(outcome["result"])["items"][0]
    assert got["review_state"] == "rejected" and got["reviewed_by"] == "Bernie" and got["reviewed_at"] == "2026-11-12T00:00:00Z"
    assert got["review_reason"] == item["review_reason"] and got["review"] == "unverified"


def test_list_review_state_filter_forwarded_and_schema_strict():
    prop = LIST_INPUT_SCHEMA["properties"]["review_state"]
    assert prop["type"] == "string" and prop["enum"] == ["none", "pending", "pending_delete", "verified", "rejected"]
    assert LIST_INPUT_SCHEMA["additionalProperties"] is False
    http = FakeHttp({"list": (200, {"items": [{"id": "n1", "title": "t", "review_state": "pending", "review": "unverified"}]})})
    ex = LoreToolExecutor(make_config(), transport=http)
    _, outcome = run(ex, TOOL_LIST, {"review_state": "pending"})
    assert http.calls[0][2]["review_state"] == "pending"
    assert json.loads(outcome["result"])["items"][0]["review_state"] == "pending"
    run(ex, TOOL_LIST, {})
    assert "review_state" not in http.calls[1][2]


@pytest.mark.parametrize("bad", ["bogus", ["pending"], 1, ""])
def test_list_invalid_review_state_rejected_without_http(bad: Any):
    http = FakeHttp()
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=http), TOOL_LIST, {"review_state": bad})
    assert receipt.error_code == "failed" and json.loads(outcome["result"])["error"] == "invalid_args" and not http.calls


def test_review_guidance_and_list_tool_version():
    entries = {e.name: e for e in lore_entries()}
    for name in (TOOL_UPDATE, TOOL_DELETE):
        hint = entries[name].descriptor.usage_hint
        assert "rejected" in hint and "pending_delete" in hint and "重新送審" in hint and "取消刪除請求" in hint
    assert entries[TOOL_LIST].spec.tool_version == 2 and entries[TOOL_RECALL].spec.tool_version == 1


def test_delete_enabled_by_default_in_settings_and_config():
    assert UepSettings(_env_file=None).lore_delete_enabled is True
    assert LoreConfig().delete_enabled is True


def test_delete_guards():
    http = FakeHttp({"note_delete": [PLAN]})
    ex = LoreToolExecutor(make_config(), transport=http, allow_write=True)
    for params in ({"id": "n1", "space": "personal"}, {"id": "n1", "vault": "lore/secret"}):
        receipt, _ = run(ex, TOOL_DELETE, params)
        assert receipt.error_code == "denied"
    for bad in ("doc:abc", "chunk:abc"):
        _, outcome = run(ex, TOOL_DELETE, {"id": bad})
        assert json.loads(outcome["result"])["error"] == "invalid_args"
    off = LoreToolExecutor(make_config(delete_enabled=False), transport=http, allow_write=True)
    assert run(off, TOOL_DELETE, {"id": "n1"})[0].error_code == "denied"
    assert not http.calls


def test_delete_plan_changed_missing_token_and_not_found():
    changed = FakeHttp({"note_delete": [PLAN, (409, {"error": {"code": "plan_changed", "message": "x", "confirm_token": "NEW"}})]})
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=changed, allow_write=True), TOOL_DELETE, {"id": "n1"})
    assert not receipt.succeeded and "NEW" not in outcome["result"] and "plan_changed" in outcome["result"]
    no_token = FakeHttp({"note_delete": [(200, {"executed": False})]})
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=no_token, allow_write=True), TOOL_DELETE, {"id": "n1"})
    assert not receipt.succeeded and json.loads(outcome["result"])["error"] == "bad_response"
    gone = FakeHttp({"note_delete": [(404, {"error": {"code": "not_found", "message": "不存在"}})]})
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=gone, allow_write=True), TOOL_DELETE, {"id": "nope"})
    assert not receipt.succeeded and json.loads(outcome["result"])["error"] == "not_found" and len(gone.calls) == 1


def test_delete_timeout_semantics():
    class Flaky(FakeHttp):
        def __call__(self, url, headers, body, timeout):
            if len(self.calls) == 1:  # 第二步（已送出確認）逾時：結果未明
                self.calls.append((url, dict(headers), json.loads(body.decode("utf-8"))))
                raise LoreTimeout
            return super().__call__(url, headers, body, timeout)

    ex = LoreToolExecutor(make_config(), transport=Flaky({"note_delete": [PLAN]}), allow_write=True)
    with pytest.raises(ToolResponseLost):
        ex.execute(call(TOOL_DELETE, {"id": "n1"}))
    first_timeout = LoreToolExecutor(make_config(), transport=FakeHttp(raises=LoreTimeout()), allow_write=True)
    receipt, _ = run(first_timeout, TOOL_DELETE, {"id": "n1"})  # 規劃階段逾時：沒改任何資料，一般失敗
    assert not receipt.succeeded


# ---- token 不洩漏 ----

def test_token_not_logged(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    for exc in (LoreUnreachable(), LoreTimeout(), RuntimeError(TOKEN)):
        run(LoreToolExecutor(make_config(), transport=FakeHttp(raises=exc)), TOOL_RECALL, {"query": "q"})
    assert TOKEN not in caplog.text


# ---- 真 urllib 傳輸層對本機 fake 伺服器（仍不打真服務） ----

class _Handler(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str | None, dict[str, Any]]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        _Handler.seen.append((self.path, self.headers.get("Authorization"), body))
        if self.path == "/v1/redirect":
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:1/steal")
            self.end_headers()
            return
        payload = json.dumps({"items": [{"id": "n1", "title": "本機"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        return


def test_urllib_transport_against_local_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        ex = LoreToolExecutor(make_config(url=base))
        receipt, outcome = run(ex, TOOL_RECALL, {"query": "q"})
        assert receipt.succeeded and json.loads(outcome["result"])["items"][0]["title"] == "本機"
        assert _Handler.seen[-1][0] == "/v1/recall" and _Handler.seen[-1][1] == f"Bearer {TOKEN}"
        status, _ = urllib_transport(f"{base}/v1/redirect", {"Authorization": "Bearer x"}, b"{}", 2.0)
        assert status == 307  # 不跟隨轉址
    finally:
        server.shutdown()
        server.server_close()


# ---- 可選：真服務冒煙（只有設了 UEP_LORE_* 才跑） ----

@pytest.mark.skipif(
    not (
        os.environ.get("UEP_LORE_URL") and os.environ.get("UEP_LORE_TOKEN")
        and (os.environ.get("UEP_LORE_DEV_VAULT") or os.environ.get("UEP_LORE_VAULTS"))
    ),
    reason="未設定 UEP_LORE_*，略過真服務冒煙測試",
)
def test_live_service_smoke_read_only():
    cfg = LoreConfig.from_settings(UepSettings(_env_file=None))
    assert cfg.configured
    ex = LoreToolExecutor(cfg)
    space = "dev" if cfg.dev_vault and cfg.default_space == "dev" else "lore"
    if space == "lore" and not cfg.vaults:
        space = "dev"
    receipt, outcome = run(ex, TOOL_LIST, {"space": space, "limit": 1})
    assert receipt.succeeded, outcome.get("result")
    assert json.loads(outcome["result"])["ok"] is True


# ---- 審查修正：反例 ----

@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_is_failure_not_success_for_every_tool(status: int):
    """3xx（空 body）不得被當成功；請求沒被處理，寫入也沒發生，所以是一般失敗而非 UNKNOWN。"""
    for tool, params in (
        (TOOL_WRITE, {"title": "t", "body": "b"}), (TOOL_UPDATE, {"id": "n1", "expected_updated": "u", "body": "x"}),
        (TOOL_RECALL, {"query": "q"}),
    ):
        http = FakeHttp({tool.split(".", 1)[1]: (status, {})})
        receipt, outcome = run(LoreToolExecutor(make_config(), transport=http, allow_write=True), tool, params)
        data = json.loads(outcome["result"])
        assert not receipt.succeeded and receipt.error_code == "failed" and data["error"] == "redirect"
        assert "id" not in data and outcome["success"] is False


def test_redirect_on_delete_confirm_step_is_failure():
    http = FakeHttp({"note_delete": [PLAN, (302, {})]})
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=http, allow_write=True), TOOL_DELETE, {"id": "n1"})
    assert not receipt.succeeded and json.loads(outcome["result"])["error"] == "redirect"


def test_write_result_is_length_capped_with_huge_duplicates():
    dups = [{"id": f"d{i}", "title": "標" * 300, "summary": "摘" * 300} for i in range(200)]
    http = FakeHttp({"write": (201, {"id": "n1", "updated": "u", "duplicates": dups})})
    _, outcome = run(LoreToolExecutor(make_config(max_chars=1500), transport=http, allow_write=True), TOOL_WRITE,
                     {"title": "t", "body": "b"})
    data = json.loads(outcome["result"])
    assert len(outcome["result"]) <= 1500 and data["truncated"] is True and data["id"] == "n1"


def test_invalid_review_state_dropped_from_result_text_too():
    ex = LoreToolExecutor(make_config(), allow_write=True, transport=FakeHttp({"write": [
        (201, {"id": "n1", "review_state": "weird"}), (201, {"id": "n2", "review_state": "pending"}),
    ]}))
    _, bad = run(ex, TOOL_WRITE, {"title": "t", "body": "b"})
    _, good = run(ex, TOOL_WRITE, {"title": "t", "body": "b"})
    assert "review_state" not in json.loads(bad["result"]) and "weird" not in bad["result"]
    assert json.loads(good["result"])["review_state"] == "pending"
    http = FakeHttp({"recall": (200, {"items": [{"id": "n1", "title": "a", "review_state": "bogus"}]})})
    _, read = run(LoreToolExecutor(make_config(), transport=http), TOOL_RECALL, {"query": "q"})
    assert "bogus" not in read["result"]


def test_server_json_with_executor_like_keys_is_not_mistaken_for_failure():
    """服務端 JSON 剛好有 success／result／code 鍵時，仍照服務端內容處理（失敗形狀靠型別而非鍵名）。"""
    body = {"success": False, "result": "x", "code": "failed", "items": [{"id": "n1", "title": "標題"}]}
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=FakeHttp({"recall": (200, body)})), TOOL_RECALL, {"query": "q"})
    assert receipt.succeeded and json.loads(outcome["result"])["items"][0]["title"] == "標題"
    plan = (200, {"success": True, "executed": False, "confirm_token": "T"})
    http = FakeHttp({"note_delete": [plan, (200, {"success": False, "executed": True})]})
    receipt, outcome = run(LoreToolExecutor(make_config(), transport=http, allow_write=True), TOOL_DELETE, {"id": "n1"})
    assert receipt.succeeded and json.loads(outcome["result"])["deleted"] is True


@pytest.mark.parametrize("bad", ["doc:abc", "DOC:abc", " Chunk:x", "  doc:x  "])
def test_write_tools_reject_document_ids_uniformly(bad: str):
    http = FakeHttp()
    ex = LoreToolExecutor(make_config(), transport=http, allow_write=True)
    cases = (
        (TOOL_UPDATE, {"id": bad, "expected_updated": "u", "body": "x"}),
        (TOOL_DELETE, {"id": bad}),
        (TOOL_WRITE, {"title": "t", "body": "b", "supersedes": bad}),
    )
    for tool, params in cases:
        receipt, outcome = run(ex, tool, params)
        assert not receipt.succeeded and json.loads(outcome["result"])["error"] == "invalid_args"
    assert not http.calls


def test_write_ids_are_stripped_before_sending():
    http = FakeHttp({"update": (200, {"id": "n1"}), "write": (201, {"id": "n2"})})
    ex = LoreToolExecutor(make_config(), transport=http, allow_write=True)
    run(ex, TOOL_UPDATE, {"id": " n1 ", "expected_updated": "u", "body": "x"})
    run(ex, TOOL_WRITE, {"title": "t", "body": "b", "supersedes": " n1 "})
    assert http.calls[0][2]["id"] == "n1" and http.calls[1][2]["supersedes"] == "n1"


def test_dev_vault_wildcard_treated_as_unset(monkeypatch: pytest.MonkeyPatch):
    for key in ("UEP_LORE_URL", "UEP_LORE_TOKEN", "UEP_LORE_DEV_VAULT", "UEP_LORE_VAULTS"):
        monkeypatch.delenv(key, raising=False)
    settings = UepSettings(_env_file=None, UEP_LORE_URL="http://x", UEP_LORE_TOKEN=SecretStr("t"), UEP_LORE_DEV_VAULT=" * ")
    cfg = LoreConfig.from_settings(settings)
    assert cfg.dev_vault == "" and not cfg.configured
    http = FakeHttp()
    receipt, _ = run(LoreToolExecutor(make_config(dev_vault=""), transport=http), TOOL_RECALL, {"query": "q", "space": "dev"})
    assert receipt.error_code == "denied" and not http.calls


def test_results_carry_untrusted_marker_first_with_fixed_key_order():
    http = FakeHttp({"recall": (200, {"items": [{"id": "n1", "title": "a"}]}), "write": (201, {"id": "n2"}),
                     "note_delete": [PLAN, (200, {"executed": True})]})
    ex = LoreToolExecutor(make_config(), transport=http, allow_write=True)
    for tool, params in ((TOOL_RECALL, {"query": "q"}), (TOOL_WRITE, {"title": "t", "body": "b"}), (TOOL_DELETE, {"id": "n1"})):
        _, outcome = run(ex, tool, params)
        data = json.loads(outcome["result"])
        assert list(data)[:2] == ["untrusted", "notice"] and data["untrusted"] is True and data["notice"]


def test_urllib_transport_ignores_environment_proxy(monkeypatch: pytest.MonkeyPatch):
    """環境有 proxy 時請求仍直連（Bearer token 不經 proxy）：proxy 指向會拒絕連線的位址，直連才會成功。"""
    for key in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _ = urllib_transport(f"http://127.0.0.1:{server.server_address[1]}/v1/recall", {}, b"{}", 3.0)
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()
