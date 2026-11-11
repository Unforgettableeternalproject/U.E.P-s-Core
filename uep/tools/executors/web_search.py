"""web.search 的 provider 介面、內建 provider 與 provider 鏈。

- ``SearchProvider``：``name``／``available()``／``search(query, limit, languages, deadline)``，回 ``SearchHit`` 清單；
  失敗拋 ``ProviderError``（``code`` 為分類，不含外部內容）。要換供應商只需實作這個介面。
- 內建（都走 ``web_http.fetch_url``，遵守 SSRF、大小、逾時規則）：``DuckDuckGoProvider``（解析 HTML 結果頁）、
  ``WikipediaProvider``（zh／en 搜尋 API）、``SearxngProvider``（設了 URL 才建立；自架內網要在設定白名單明列主機）、
  ``FakeSearchProvider``（測試與離線 Console 用）。OpenAI Responses 內建 web_search 的備援在 ``uep.models.openai_tools``。
- ``SearchChain``：依序嘗試，前一個失敗、逾時或結果為空就換下一個；備援永遠最後。全部失敗拋 ``SearchChainError``。
  結果統一成 ``{title, url, snippet, provider}``，去重、限制筆數。
"""
from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit

from uep.tools.executors.web_http import (
    JSON_TYPES,
    FetchError,
    FetchPolicy,
    Resolver,
    Transport,
    fetch_url,
)
from uep.tools.executors.web_text import collapse, decode_body

TITLE_MAX = 200
SNIPPET_MAX = 400
URL_MAX = 1000
_CJK = re.compile(r"[㐀-鿿぀-ヿ豈-﫿]")
_TAGS = re.compile(r"<[^>]+>")


class ProviderError(Exception):
    """單一 provider 失敗；``code`` 是分類（blocked／parse／timeout／network／http_error／…）。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    provider: str


class SearchProvider(Protocol):
    name: str

    def available(self) -> bool:
        """設定面是否可用（例如備援需要金鑰）；不做網路 I/O。"""
        ...

    def search(self, query: str, *, limit: int, languages: Sequence[str], deadline: float) -> list[SearchHit]: ...


@dataclass
class SearchOutcome:
    hits: list[SearchHit]
    provider: str
    fallback_used: bool
    attempts: list[dict[str, str]] = field(default_factory=list)


class SearchChainError(Exception):
    def __init__(self, attempts: list[dict[str, str]]) -> None:
        super().__init__("所有搜尋來源都失敗或沒有結果")
        self.attempts = attempts


def preferred_languages(query: str, hint: str | None) -> tuple[str, ...]:
    """搜尋語言順序：明確指定的語言優先，否則含中日文字元就 zh 先、其餘 en 先。"""
    order = ("zh", "en") if _CJK.search(query) else ("en", "zh")
    if hint in ("zh", "en"):
        return (hint, "en" if hint == "zh" else "zh")
    return order


def clean_text(text: str, limit: int) -> str:
    text = collapse(html.unescape(_TAGS.sub("", text)))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _norm_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _http_url(url: str) -> str | None:
    url = url.strip()
    if len(url) > URL_MAX:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return url if parts.scheme in ("http", "https") and parts.netloc else None


def tidy_hits(hits: Sequence[SearchHit], limit: int) -> list[SearchHit]:
    """正規化欄位、丟掉非 http(s) 或缺標題的項目、依 URL 去重、限制筆數。"""
    seen: set[str] = set()
    out: list[SearchHit] = []
    for hit in hits:
        url = _http_url(hit.url)
        title = clean_text(hit.title, TITLE_MAX)
        if url is None or not title:
            continue
        key = _norm_url(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(SearchHit(title, url, clean_text(hit.snippet, SNIPPET_MAX), hit.provider))
        if len(out) >= limit:
            break
    return out


def _json_policy(policy: FetchPolicy) -> FetchPolicy:
    """同一組限制，只把可接受的 content-type 換成 JSON（保留私網白名單與標頭）。"""
    return replace(policy, accept_types=JSON_TYPES)


class _HttpProvider:
    """走 ``fetch_url`` 的 provider 共用部分。"""

    name = ""

    def __init__(
        self, policy: FetchPolicy, *, transport: Transport | None = None, resolver: Resolver | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy
        self._transport = transport
        self._resolver = resolver
        self._clock = clock

    def available(self) -> bool:
        return True

    def _get(self, url: str, deadline: float, policy: FetchPolicy | None = None) -> tuple[str, str]:
        """``(解碼後內文, content_type)``；抓取失敗轉成 ``ProviderError``。"""
        try:
            result = fetch_url(
                url, policy or self._policy, transport=self._transport, resolver=self._resolver, clock=self._clock,
                deadline=deadline,
            )
        except FetchError as exc:
            raise ProviderError(exc.code, exc.message) from exc
        return decode_body(result.body, result.charset), result.content_type


# ---- DuckDuckGo HTML ----


class _DdgParser(HTMLParser):
    """解析 html.duckduckgo.com 結果頁：``a.result__a`` 為標題連結、``a.result__snippet`` 為摘要。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.raw: list[dict[str, str]] = []
        self._target: str | None = None  # 目前正在收文字的欄位

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = {k: v or "" for k, v in attrs}
        classes = values.get("class", "").split()
        if "result__a" in classes:
            self.raw.append({"href": values.get("href", ""), "title": "", "snippet": ""})
            self._target = "title"
        elif "result__snippet" in classes and self.raw:
            self._target = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._target = None

    def handle_data(self, data: str) -> None:
        if self._target and self.raw:
            self.raw[-1][self._target] += data


def _ddg_target(href: str) -> str | None:
    """DDG 的結果連結包在 ``/l/?uddg=<實際網址>``；解開，其餘原樣。"""
    full = urljoin("https://html.duckduckgo.com/html/", href.strip())
    parts = urlsplit(full)
    if parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg", [""])[0]
        return target or None
    return full


class DuckDuckGoProvider(_HttpProvider):
    name = "duckduckgo"
    ENDPOINT = "https://html.duckduckgo.com/html/"

    def search(self, query: str, *, limit: int, languages: Sequence[str], deadline: float) -> list[SearchHit]:
        body, _ = self._get(f"{self.ENDPOINT}?{urlencode({'q': query})}", deadline)
        parser = _DdgParser()
        parser.feed(body)
        hits: list[SearchHit] = []
        for item in parser.raw:
            target = _ddg_target(item["href"])
            host = urlsplit(target).hostname or "" if target else ""
            if not target or host.endswith("duckduckgo.com"):  # 廣告（y.js）與站內連結
                continue
            hits.append(SearchHit(item["title"], target, item["snippet"], self.name))
        if not hits and re.search(r"anomaly|captcha|challenge|unusual traffic", body, re.IGNORECASE):
            raise ProviderError("blocked", "搜尋站要求驗證，暫時無法使用")
        return hits


# ---- Wikipedia ----


class WikipediaProvider(_HttpProvider):
    name = "wikipedia"

    def __init__(self, policy: FetchPolicy, **kwargs: object) -> None:
        super().__init__(_json_policy(policy), **kwargs)  # type: ignore[arg-type]

    def search(self, query: str, *, limit: int, languages: Sequence[str], deadline: float) -> list[SearchHit]:
        last_error: ProviderError | None = None
        for lang in languages or ("en",):
            if lang not in ("zh", "en"):
                continue
            params = {
                "action": "query", "list": "search", "srsearch": query, "srlimit": str(limit), "format": "json",
                "utf8": "1",
            }
            try:
                body, _ = self._get(f"https://{lang}.wikipedia.org/w/api.php?{urlencode(params)}", deadline)
                rows = json.loads(body)["query"]["search"]
                if not isinstance(rows, list):
                    raise TypeError
            except ProviderError as exc:
                last_error = exc
                continue
            except (ValueError, KeyError, TypeError):
                last_error = ProviderError("parse", "回應格式無法解析")
                continue
            hits = [
                SearchHit(
                    str(row.get("title", "")), f"https://{lang}.wikipedia.org/wiki/{quote(str(row.get('title', '')).replace(' ', '_'), safe='_()')}",
                    str(row.get("snippet", "")), self.name,
                )
                for row in rows if isinstance(row, dict) and row.get("title")
            ]
            if hits:
                return hits
        if last_error is not None:
            raise last_error
        return []


# ---- SearXNG ----


class SearxngProvider(_HttpProvider):
    name = "searxng"

    def __init__(self, base_url: str, policy: FetchPolicy, **kwargs: object) -> None:
        super().__init__(_json_policy(policy), **kwargs)  # type: ignore[arg-type]
        self._base = base_url.strip().rstrip("/")

    def available(self) -> bool:
        return _http_url(self._base) is not None

    def search(self, query: str, *, limit: int, languages: Sequence[str], deadline: float) -> list[SearchHit]:
        body, _ = self._get(f"{self._base}/search?{urlencode({'q': query, 'format': 'json'})}", deadline)
        try:
            rows = json.loads(body)["results"]
            if not isinstance(rows, list):
                raise TypeError
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("parse", "回應格式無法解析") from exc
        return [
            SearchHit(str(r.get("title", "")), str(r.get("url", "")), str(r.get("content", "")), self.name)
            for r in rows if isinstance(r, dict)
        ]


# ---- fake ----


class FakeSearchProvider:
    """離線 provider：固定回傳（或丟出指定錯誤）；測試與離線 Console 用。"""

    def __init__(
        self, name: str = "fake", hits: Sequence[SearchHit] = (), *, error: ProviderError | None = None,
        usable: bool = True,
    ) -> None:
        self.name = name
        self._hits = list(hits)
        self._error = error
        self._usable = usable
        self.calls: list[str] = []

    def available(self) -> bool:
        return self._usable

    def search(self, query: str, *, limit: int, languages: Sequence[str], deadline: float) -> list[SearchHit]:
        self.calls.append(query)
        if self._error is not None:
            raise self._error
        return list(self._hits)


# ---- 鏈 ----


class SearchChain:
    def __init__(
        self, providers: Sequence[SearchProvider], *, fallback: SearchProvider | None = None, budget_s: float = 40.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._providers = [p for p in providers if p.available()]
        self._fallback = fallback if fallback is not None and fallback.available() else None
        self._budget_s = budget_s
        self._clock = clock

    def available(self) -> bool:
        return bool(self._providers) or self._fallback is not None

    @property
    def names(self) -> list[str]:
        return [p.name for p in self._providers] + ([self._fallback.name] if self._fallback else [])

    def run(self, query: str, *, limit: int, language: str | None = None) -> SearchOutcome:
        deadline = self._clock() + self._budget_s
        languages = preferred_languages(query, language)
        attempts: list[dict[str, str]] = []
        sequence: list[tuple[SearchProvider, bool]] = [(p, False) for p in self._providers]
        if self._fallback is not None:
            sequence.append((self._fallback, True))
        for provider, is_fallback in sequence:
            if self._clock() >= deadline:
                attempts.append({"provider": provider.name, "outcome": "skipped_budget"})
                continue
            try:
                raw = provider.search(query, limit=limit, languages=languages, deadline=deadline)
            except ProviderError as exc:
                attempts.append({"provider": provider.name, "outcome": exc.code})
                continue
            except Exception:  # noqa: BLE001 - provider 的任何失敗都只換下一個
                attempts.append({"provider": provider.name, "outcome": "error"})
                continue
            hits = tidy_hits(raw, limit)
            if not hits:
                attempts.append({"provider": provider.name, "outcome": "empty"})
                continue
            attempts.append({"provider": provider.name, "outcome": "ok"})
            return SearchOutcome(hits, provider.name, is_fallback, attempts)
        raise SearchChainError(attempts)
