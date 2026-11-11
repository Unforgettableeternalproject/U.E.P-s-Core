"""``web.search``／``web.fetch`` 的 ``ToolExecutor``（宣告見 ``uep.tools.catalog.web``）。

- ``web.fetch``：安全抓取（見 ``web_http``）後抽純文字；只收 text 類內容，文字有長度上限。
- ``web.search``：交給 ``SearchChain``（provider 鏈）；output 帶實際用的 provider、是否走備援、各來源的嘗試結果。
- 兩者的輸出都標 ``untrusted=true`` 並附 ``notice``：外部內容只當資料，不當指令。
- 參數在這裡再驗一次（不信任上游）；錯誤訊息只放分類原因，不回放外部內容。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from uep.tools.catalog.web import (
    FETCH_CHARS_MAX,
    FETCH_CHARS_MIN,
    FETCH_URL_MAX,
    SEARCH_QUERY_MAX,
    SEARCH_RESULTS_MAX,
    TOOL_WEB_FETCH,
    TOOL_WEB_SEARCH,
)
from uep.tools.executors.base import (
    UNTRUSTED_NOTICE,
    ReadToolExecutor,
    denied,
    dump,
    failure,
)
from uep.tools.executors.web_http import (
    FetchError,
    FetchPolicy,
    Resolver,
    Transport,
    fetch_url,
)
from uep.tools.executors.web_search import SearchChain, SearchChainError
from uep.tools.executors.web_text import clip, collapse, decode_body, html_to_text

DEFAULT_SEARCH_RESULTS = 5
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def _bad_args(tool: str) -> dict[str, Any]:
    return failure("failed", f"Invalid arguments for {tool!r}.")


class WebToolExecutor(ReadToolExecutor):
    def __init__(
        self, *, search: SearchChain | None = None, fetch_policy: FetchPolicy | None = None, fetch_max_chars: int = 8000,
        transport: Transport | None = None, resolver: Resolver | None = None, **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._search = search
        self._policy = fetch_policy or FetchPolicy()
        self._max_chars = fetch_max_chars
        self._transport = transport
        self._resolver = resolver

    def _run(self, tool: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if tool == TOOL_WEB_SEARCH:
            return self._do_search(params)
        if tool == TOOL_WEB_FETCH:
            return self._do_fetch(params)
        return denied(tool)

    # ---- web.search ----

    def _do_search(self, params: Mapping[str, Any]) -> dict[str, Any]:
        query, limit, language = params.get("query"), params.get("max_results", DEFAULT_SEARCH_RESULTS), params.get("language")
        if not isinstance(query, str) or not query.strip() or len(query) > SEARCH_QUERY_MAX:
            return _bad_args(TOOL_WEB_SEARCH)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SEARCH_RESULTS_MAX:
            return _bad_args(TOOL_WEB_SEARCH)
        if language is not None and language not in ("zh", "en"):
            return _bad_args(TOOL_WEB_SEARCH)
        if self._search is None or not self._search.available():
            return failure("denied", "web.search 沒有可用的搜尋來源。")
        try:
            outcome = self._search.run(" ".join(query.split()), limit=limit, language=language)
        except SearchChainError as exc:
            summary = "、".join(f"{a['provider']}:{a['outcome']}" for a in exc.attempts) or "無"
            return failure("failed", f"所有搜尋來源都失敗或沒有結果（{summary}）。")
        return {"success": True, "result": dump({
            "untrusted": True, "notice": UNTRUSTED_NOTICE, "provider": outcome.provider,
            "fallback_used": outcome.fallback_used, "attempts": outcome.attempts,
            "results": [
                {"title": h.title, "url": h.url, "snippet": h.snippet, "provider": h.provider} for h in outcome.hits
            ],
        })}

    # ---- web.fetch ----

    def _do_fetch(self, params: Mapping[str, Any]) -> dict[str, Any]:
        url, max_chars = params.get("url"), params.get("max_chars", self._max_chars)
        if not isinstance(url, str) or not url.strip() or len(url) > FETCH_URL_MAX:
            return _bad_args(TOOL_WEB_FETCH)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not FETCH_CHARS_MIN <= max_chars <= FETCH_CHARS_MAX:
            return _bad_args(TOOL_WEB_FETCH)
        try:
            result = fetch_url(url, self._policy, transport=self._transport, resolver=self._resolver)
        except FetchError as exc:
            code = "timeout" if exc.code == "timeout" else "failed"
            return failure(code, f"無法抓取網頁：{exc.message}（{exc.code}）")
        text = decode_body(result.body, result.charset)
        if result.content_type in _HTML_TYPES:
            title, text = html_to_text(text)
        else:
            title, text = "", collapse(text)
        text, truncated = clip(text, min(max_chars, self._max_chars))  # 設定值是硬上限，模型只能要更少
        return {"success": True, "result": dump({
            "untrusted": True, "notice": UNTRUSTED_NOTICE, "url": result.url, "title": title[:200],
            "content_type": result.content_type, "text": text, "truncated": truncated,
        })}
