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
    LORE_EXECUTOR,
    LORE_READ_TOOLS,
    LORE_TOOLS,
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
        "default_space": "lore", "author": "測試角色", "timeout_s": 3.0, "max_chars": 2000,
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
        status, payload = self.responses.get(url.rsplit("/", 1)[-1], (200, {}))
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
    assert not any("delete" in name for name in LORE_TOOLS)


def test_capability_tags_in_closed_set():
    assert CAPABILITY_CEILING["lore.read"] == "internal_read"
    assert "lore.write" in CAPABILITY_CEILING


def test_write_entries_registered_honestly_native():
    """寫入如實標 external_effect；免核准靠 native=True（RA-2a），不是調低分類。"""
    entries = {e.name: e for e in lore_entries()}
    assert {TOOL_WRITE, TOOL_UPDATE} <= set(entries)
    for name in (TOOL_WRITE, TOOL_UPDATE):
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
    registry = with_lore(memory_registry(), configured=True)
    assert all(registry.readiness(n) == "ready" for n in LORE_TOOLS)
    assert {"lore.read", "lore.write"} <= registry.enabled_tags()
    assert with_lore(registry, configured=False) is registry  # 已含 lore：原樣回傳
    assert "lore.recall" not in memory_registry().entries()  # 不改動傳入的實例


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
    for tool in ("lore.delete", "lore.undelete", "recall"):
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
