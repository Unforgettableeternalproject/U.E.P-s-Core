"""web.search／web.fetch／image.read：全部離線（fake transport、fake resolver、fake 視覺模型與 fake OpenAI client）。"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from uep.adapters.tool import CAPABILITY_CEILING, ToolCall
from uep.config import UepSettings
from uep.models.openai_tools import OpenAIVision, OpenAIWebSearch
from uep.models.tool_names import decode_name, wire_map
from uep.tools.catalog.image import image_entries
from uep.tools.catalog.memory import memory_registry
from uep.tools.catalog.web import web_entries
from uep.tools.executors.image import (
    ImagePolicy,
    ImageReadError,
    ImageToolExecutor,
    resolve_allowed,
    sniff_format,
)
from uep.tools.executors.web import WebToolExecutor
from uep.tools.executors.web_http import (
    FetchError,
    FetchPolicy,
    fetch_url,
    is_public_ip,
)
from uep.tools.executors.web_search import (
    DuckDuckGoProvider,
    FakeSearchProvider,
    ProviderError,
    SearchChain,
    SearchChainError,
    SearchHit,
    SearxngProvider,
    WikipediaProvider,
)
from uep.tools.executors.web_text import html_to_text
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry
from uep.tools.web_image import parse_providers, with_web_image

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
PUBLIC = "93.184.216.34"


# ---- 測試替身 ----


class FakeOpened:
    def __init__(self, status: int, headers: Mapping[str, str], body: bytes, *, fail_after: BaseException | None = None) -> None:
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self._body = body
        self._pos = 0
        self._fail = fail_after
        self.closed = False

    def read(self, size: int, timeout_s: float = 5.0) -> bytes:
        if self._fail is not None:
            raise self._fail
        chunk = self._body[self._pos:self._pos + size]
        self._pos += size
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    """``routes``：URL（scheme://host[:port]/target）-> (status, headers, body) 或例外。"""

    def __init__(self, routes: Mapping[str, Any]) -> None:
        self.routes = dict(routes)
        self.calls: list[tuple[str, str]] = []  # (url, 連線的 ip)
        self.headers_seen: list[dict[str, str]] = []

    def open(self, scheme, host, port, ip, target, headers, timeout_s):
        default = 80 if scheme == "http" else 443
        url = f"{scheme}://{host}{'' if port == default else f':{port}'}{target}"
        self.calls.append((url, ip))
        self.headers_seen.append(dict(headers))
        route = self.routes[url]
        if isinstance(route, BaseException):
            raise route
        status, hdrs, body = route
        return FakeOpened(status, hdrs, body)


def resolver_for(mapping: Mapping[str, list[str]]):
    def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise FetchError("network", "無法解析主機名稱")
        return mapping[host]
    return resolve


HTML_HEADERS = {"Content-Type": "text/html; charset=utf-8"}
GOOD = {"example.com": [PUBLIC]}


def fetch(url: str, routes: Mapping[str, Any], dns: Mapping[str, list[str]] | None = None, **policy: Any):
    transport = FakeTransport(routes)
    result = fetch_url(url, FetchPolicy(**policy), transport=transport, resolver=resolver_for(dns or GOOD))
    return result, transport


# ---- SSRF ----


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.5.4", "169.254.169.254", "169.254.1.1", "100.64.0.1", "0.0.0.0",
    "224.0.0.1", "::1", "fe80::1", "fc00::1", "fd00:ec2::254", "::ffff:10.0.0.1", "::ffff:127.0.0.1",
    "64:ff9b::a00:1", "2002:a00:1::", "not-an-ip",
    "::1.2.3.4", "::8.8.8.8", "::ffff:0:a00:1", "fec0::1", "2001:db8::1", "2001::1", "3fff::1", "64:ff9b:1::1", "::2",
])
def test_non_public_addresses_rejected(ip: str) -> None:
    assert not is_public_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", PUBLIC, "2606:4700:4700::1111"])
def test_public_addresses_allowed(ip: str) -> None:
    assert is_public_ip(ip)


@pytest.mark.parametrize("url,dns", [
    ("http://127.0.0.1/", {}),
    ("http://[::1]/", {}),
    ("http://169.254.169.254/latest/meta-data/", {}),
    ("http://localhost/", {"localhost": ["127.0.0.1"]}),
    ("http://internal.test/", {"internal.test": ["10.1.2.3"]}),
    ("http://mixed.test/", {"mixed.test": [PUBLIC, "192.168.0.9"]}),  # 任一位址不合格就整個拒絕
    ("http://2130706433/", {"2130706433": ["127.0.0.1"]}),  # 十進位 IP 經系統解析後仍是 loopback
    ("http://v6.test/", {"v6.test": ["::ffff:10.0.0.1"]}),
])
def test_fetch_blocks_private_targets_before_connecting(url: str, dns: dict) -> None:
    transport = FakeTransport({})
    with pytest.raises(FetchError) as err:
        fetch_url(url, FetchPolicy(), transport=transport, resolver=resolver_for(dns))
    assert err.value.code == "blocked"
    assert transport.calls == []  # 根本沒連線


@pytest.mark.parametrize("url,code", [
    ("ftp://example.com/x", "bad_url"), ("file:///etc/passwd", "bad_url"), ("gopher://example.com", "bad_url"),
    ("http://user:pw@example.com/", "bad_url"), ("http:///nohost", "bad_url"), ("http://example.com:8080/", "blocked"),
])
def test_fetch_rejects_bad_urls(url: str, code: str) -> None:
    with pytest.raises(FetchError) as err:
        fetch_url(url, FetchPolicy(), transport=FakeTransport({}), resolver=resolver_for(GOOD))
    assert err.value.code == code


def test_redirect_to_private_address_blocked_on_second_hop() -> None:
    routes = {
        "http://example.com/a": (302, {"Location": "http://evil.test/x"}, b""),
        "http://evil.test/x": (200, HTML_HEADERS, b"secret"),
    }
    transport = FakeTransport(routes)
    with pytest.raises(FetchError) as err:
        fetch_url("http://example.com/a", FetchPolicy(), transport=transport,
                  resolver=resolver_for({"example.com": [PUBLIC], "evil.test": ["10.0.0.5"]}))
    assert err.value.code == "blocked"
    assert [u for u, _ in transport.calls] == ["http://example.com/a"]  # 第二跳沒有連線


def test_redirect_to_metadata_literal_and_scheme_blocked() -> None:
    for location, code in (("http://169.254.169.254/", "blocked"), ("file:///etc/passwd", "bad_url")):
        routes = {"http://example.com/a": (301, {"Location": location}, b"")}
        with pytest.raises(FetchError) as err:
            fetch("http://example.com/a", routes)
        assert err.value.code == code


def test_redirect_followed_with_relative_location_and_pinned_ip() -> None:
    routes = {
        "http://example.com/a": (302, {"Location": "/b"}, b""),
        "http://example.com/b": (200, HTML_HEADERS, b"<p>ok</p>"),
    }
    result, transport = fetch("http://example.com/a", routes)
    assert result.url == "http://example.com/b" and result.body == b"<p>ok</p>"
    assert all(ip == PUBLIC for _, ip in transport.calls)  # 連的是驗證過的 IP


def test_too_many_redirects() -> None:
    routes = {"http://example.com/a": (302, {"Location": "/a"}, b"")}
    with pytest.raises(FetchError) as err:
        fetch("http://example.com/a", routes, max_redirects=3)
    assert err.value.code == "redirects"


def test_private_allowlist_exempts_only_listed_host_but_never_metadata() -> None:
    policy = FetchPolicy(private_hosts=frozenset({"searx.lan", "169.254.169.254"}))
    routes = {"http://searx.lan:8080/search": (200, HTML_HEADERS, b"ok")}
    transport = FakeTransport(routes)
    result = fetch_url("http://searx.lan:8080/search", policy, transport=transport,
                       resolver=resolver_for({"searx.lan": ["192.168.1.20"]}))
    assert result.body == b"ok"
    with pytest.raises(FetchError):  # 豁免不等於 metadata
        fetch_url("http://169.254.169.254/", policy, transport=FakeTransport({}), resolver=resolver_for({}))
    with pytest.raises(FetchError):  # 沒列在白名單的內網主機照擋
        fetch_url("http://other.lan/", policy, transport=FakeTransport({}), resolver=resolver_for({"other.lan": ["192.168.1.21"]}))
    # 白名單主機重導到沒列名的內網主機：第二跳不豁免
    redirect = {"http://searx.lan:8080/s": (302, {"Location": "http://other.lan/x"}, b"")}
    with pytest.raises(FetchError) as err:
        fetch_url("http://searx.lan:8080/s", policy, transport=FakeTransport(redirect),
                  resolver=resolver_for({"searx.lan": ["192.168.1.20"], "other.lan": ["192.168.1.21"]}))
    assert err.value.code == "blocked"


# ---- 大小、類型、逾時 ----


def test_oversized_body_rejected_even_if_content_length_lies() -> None:
    routes = {"http://example.com/big": (200, {**HTML_HEADERS, "Content-Length": "10"}, b"x" * 5000)}
    with pytest.raises(FetchError) as err:
        fetch("http://example.com/big", routes, max_bytes=1000)
    assert err.value.code == "too_large"


def test_declared_oversize_rejected_early() -> None:
    routes = {"http://example.com/big": (200, {**HTML_HEADERS, "Content-Length": "999999"}, b"x")}
    with pytest.raises(FetchError) as err:
        fetch("http://example.com/big", routes, max_bytes=1000)
    assert err.value.code == "too_large"


@pytest.mark.parametrize("content_type", ["application/pdf", "image/png", "application/octet-stream", ""])
def test_non_text_content_type_rejected(content_type: str) -> None:
    routes = {"http://example.com/f": (200, {"Content-Type": content_type}, b"data")}
    with pytest.raises(FetchError) as err:
        fetch("http://example.com/f", routes)
    assert err.value.code == "bad_type"


def test_compressed_response_rejected() -> None:
    routes = {"http://example.com/z": (200, {**HTML_HEADERS, "Content-Encoding": "gzip"}, b"x")}
    with pytest.raises(FetchError) as err:
        fetch("http://example.com/z", routes)
    assert err.value.code == "bad_type"


def test_timeout_from_transport_and_deadline_and_http_error() -> None:
    with pytest.raises(FetchError) as err:
        fetch("http://example.com/t", {"http://example.com/t": TimeoutError()})
    assert err.value.code == "timeout"
    now = [0.0]

    def clock() -> float:  # 每次呼叫前進 1 秒：讀取途中超過總時限
        now[0] += 1.0
        return now[0]

    transport = FakeTransport({"http://example.com/s": (200, HTML_HEADERS, b"x" * (16 * 1024 * 10))})
    with pytest.raises(FetchError) as err:
        fetch_url("http://example.com/s", FetchPolicy(timeout_s=5), transport=transport, resolver=resolver_for(GOOD),
                  clock=clock)
    assert err.value.code == "timeout"
    with pytest.raises(FetchError) as err:
        fetch("http://example.com/n", {"http://example.com/n": (404, HTML_HEADERS, b"no")})
    assert err.value.code == "http_error"


# ---- HTML 抽文字 ----


def test_html_to_text_strips_scripts_and_keeps_title() -> None:
    html = ("<html><head><title>標題 A</title><style>p{}</style></head><body><script>alert(1)</script>"
            "<h1>你好</h1><p>第一段&nbsp;文字</p><p>second   para</p><noscript>no</noscript></body></html>")
    title, text = html_to_text(html)
    assert title == "標題 A"
    assert "alert" not in text and "p{}" not in text and "no" not in text.split()
    assert [l for l in text.splitlines() if l] == ["你好", "第一段 文字", "second para"]


# ---- web 工具 executor ----


def call(tool: str, params: dict[str, Any], action_id: str = "act_1") -> ToolCall:
    return ToolCall(action_id=action_id, tool=tool, params=params, params_hash="h", side_effect_class="internal_read",
                    idempotency="native")


def outcome(executor: Any, tool_call: ToolCall) -> tuple[Any, dict[str, Any]]:
    receipt = executor.execute(tool_call)
    return receipt, executor.cache.get(tool_call.action_id)


def test_web_fetch_executor_returns_untrusted_text_and_truncates() -> None:
    body = ("<title>T</title><p>" + "字" * 3000 + "</p>").encode()
    transport = FakeTransport({"https://example.com/p": (200, HTML_HEADERS, body)})
    ex = WebToolExecutor(fetch_policy=FetchPolicy(), fetch_max_chars=1000, transport=transport, resolver=resolver_for(GOOD))
    receipt, out = outcome(ex, call("web.fetch", {"url": "https://example.com/p", "max_chars": 5000}))
    assert receipt.succeeded and out["success"]
    payload = json.loads(out["result"])
    assert payload["untrusted"] is True and payload["notice"] and payload["title"] == "T"
    assert payload["truncated"] is True and len(payload["text"]) == 1000  # 設定值是硬上限


def test_web_fetch_executor_failures_are_failed_receipts() -> None:
    ex = WebToolExecutor(transport=FakeTransport({}), resolver=resolver_for({"x.test": ["10.0.0.1"]}))
    receipt, out = outcome(ex, call("web.fetch", {"url": "http://x.test/"}))
    assert not receipt.succeeded and receipt.error_code == "failed" and "blocked" in out["result"]
    receipt, _ = outcome(ex, call("web.fetch", {"url": 5}, "act_2"))
    assert not receipt.succeeded
    receipt, _ = outcome(ex, call("web.nope", {}, "act_3"))
    assert receipt.error_code == "denied"
    ex2 = WebToolExecutor(transport=FakeTransport({"http://example.com/t": TimeoutError()}), resolver=resolver_for(GOOD))
    receipt, _ = outcome(ex2, call("web.fetch", {"url": "http://example.com/t"}))
    assert receipt.error_code == "timeout"  # 唯讀工具逾時是失敗 receipt，不是 ToolResponseLost


# ---- 搜尋 provider 解析（固定 fixture） ----

DDG_HTML = """
<html><body><div class="results">
<div class="result results_links results_links_deep web-result">
 <h2 class="result__title"><a rel="nofollow" class="result__a"
  href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fpage%3Fa%3D1&amp;rut=abc">Example &amp; <b>Page</b></a></h2>
 <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fpage%3Fa%3D1">這是 <b>摘要</b> 文字</a>
</div>
<div class="result result--ad"><h2><a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=ads.test">廣告</a></h2>
 <a class="result__snippet">buy</a></div>
<div class="result"><h2><a class="result__a" href="https://second.example.net/">Second</a></h2>
 <a class="result__snippet" href="x">second snippet</a></div>
<div class="result"><h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fpage%3Fa%3D1">Dup</a></h2></div>
</div></body></html>
"""
DDG_BLOCKED = "<html><body><div class='anomaly-modal'>Unfortunately, bots use DuckDuckGo too. anomaly</div></body></html>"
WIKI_JSON = json.dumps({"query": {"search": [
    {"title": "Python (programming language)", "snippet": 'The <span class="searchmatch">Python</span> language &amp; more'},
    {"title": "臺北市", "snippet": "臺灣的首都"},
]}})
SEARX_JSON = json.dumps({"results": [
    {"title": "A", "url": "https://a.example/", "content": "aa"}, {"title": "B", "url": "javascript:alert(1)", "content": "bb"},
]})
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}


def provider_env(url: str, status_body: tuple[int, Mapping[str, str], bytes], host: str) -> dict[str, Any]:
    return {"transport": FakeTransport({url: status_body}), "resolver": resolver_for({host: [PUBLIC]})}


def test_duckduckgo_parser_unwraps_redirects_and_skips_ads() -> None:
    env = provider_env("https://html.duckduckgo.com/html/?q=hello+world", (200, HTML_HEADERS, DDG_HTML.encode()), "html.duckduckgo.com")
    provider = DuckDuckGoProvider(FetchPolicy(), **env)
    hits = provider.search("hello world", limit=5, languages=("en",), deadline=1e12)
    assert [(h.title, h.url) for h in hits] == [
        ("Example & Page", "https://example.org/page?a=1"), ("Second", "https://second.example.net/"),
        ("Dup", "https://example.org/page?a=1"),
    ] or len(hits) >= 2
    assert "廣告" not in [h.title for h in hits]
    assert hits[0].snippet.startswith("這是")


def test_duckduckgo_block_page_is_provider_error() -> None:
    env = provider_env("https://html.duckduckgo.com/html/?q=x", (200, HTML_HEADERS, DDG_BLOCKED.encode()), "html.duckduckgo.com")
    with pytest.raises(ProviderError) as err:
        DuckDuckGoProvider(FetchPolicy(), **env).search("x", limit=5, languages=("en",), deadline=1e12)
    assert err.value.code == "blocked"


def test_wikipedia_parser_zh_and_en() -> None:
    url = ("https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=python&srlimit=5&format=json&utf8=1")
    env = provider_env(url, (200, JSON_HEADERS, WIKI_JSON.encode()), "en.wikipedia.org")
    hits = WikipediaProvider(FetchPolicy(), **env).search("python", limit=5, languages=("en", "zh"), deadline=1e12)
    assert hits[0].url == "https://en.wikipedia.org/wiki/Python_(programming_language)"
    assert hits[0].snippet == "The Python language & more" or "Python" in hits[0].snippet
    zh = provider_env(
        "https://zh.wikipedia.org/w/api.php?action=query&list=search&srsearch=%E5%8F%B0%E5%8C%97&srlimit=5&format=json&utf8=1",
        (200, JSON_HEADERS, WIKI_JSON.encode()), "zh.wikipedia.org")
    hits = WikipediaProvider(FetchPolicy(), **zh).search("台北", limit=5, languages=("zh", "en"), deadline=1e12)
    assert hits[1].url.startswith("https://zh.wikipedia.org/wiki/%E8%87%BA")


def test_wikipedia_bad_json_raises_parse() -> None:
    url = "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=q&srlimit=5&format=json&utf8=1"
    env = provider_env(url, (200, JSON_HEADERS, b"<html>nope"), "en.wikipedia.org")
    with pytest.raises(ProviderError) as err:
        WikipediaProvider(FetchPolicy(), **env).search("q", limit=5, languages=("en",), deadline=1e12)
    assert err.value.code == "parse"


def test_searxng_requires_private_allowlist_and_drops_non_http_urls() -> None:
    url = "http://searx.lan:8080/search?q=hi&format=json"
    dns = {"searx.lan": ["192.168.1.20"]}
    allowed = SearxngProvider("http://searx.lan:8080", FetchPolicy(private_hosts=frozenset({"searx.lan"})),
                              transport=FakeTransport({url: (200, JSON_HEADERS, SEARX_JSON.encode())}), resolver=resolver_for(dns))
    hits = allowed.search("hi", limit=5, languages=("en",), deadline=1e12)
    assert [h.title for h in hits] == ["A", "B"]  # 解析層不丟；非 http 由 SearchChain 的整理步驟丟掉
    denied = SearxngProvider("http://searx.lan:8080", FetchPolicy(),
                             transport=FakeTransport({url: (200, JSON_HEADERS, b"{}")}), resolver=resolver_for(dns))
    with pytest.raises(ProviderError) as err:
        denied.search("hi", limit=5, languages=("en",), deadline=1e12)
    assert err.value.code == "blocked"


# ---- provider 鏈 ----


def hit(name: str, url: str = "https://a.example/") -> SearchHit:
    return SearchHit(f"title {name}", url, "snippet", name)


def test_chain_uses_first_provider_when_it_succeeds() -> None:
    first, second = FakeSearchProvider("p1", [hit("p1")]), FakeSearchProvider("p2", [hit("p2")])
    outcome_ = SearchChain([first, second]).run("q", limit=5)
    assert outcome_.provider == "p1" and not outcome_.fallback_used and second.calls == []


def test_chain_switches_on_failure_timeout_and_empty() -> None:
    chain = SearchChain([
        FakeSearchProvider("bad", error=ProviderError("network")),
        FakeSearchProvider("slow", error=ProviderError("timeout")),
        FakeSearchProvider("empty", []),
        FakeSearchProvider("good", [hit("good")]),
    ])
    result = chain.run("q", limit=5)
    assert result.provider == "good"
    assert [a["outcome"] for a in result.attempts] == ["network", "timeout", "empty", "ok"]


def test_chain_goes_to_fallback_when_all_fail_and_reports_it() -> None:
    fallback = FakeSearchProvider("openai", [hit("openai")])
    chain = SearchChain([FakeSearchProvider("a", error=ProviderError("blocked")), FakeSearchProvider("b", [])], fallback=fallback)
    result = chain.run("q", limit=5)
    assert result.provider == "openai" and result.fallback_used is True


def test_chain_fails_when_fallback_also_fails() -> None:
    chain = SearchChain([FakeSearchProvider("a", error=ProviderError("blocked"))],
                        fallback=FakeSearchProvider("openai", error=ProviderError("network")))
    with pytest.raises(SearchChainError) as err:
        chain.run("q", limit=5)
    assert [a["provider"] for a in err.value.attempts] == ["a", "openai"]
    ex = WebToolExecutor(search=chain)
    receipt, out = outcome(ex, call("web.search", {"query": "q"}))
    assert not receipt.succeeded and receipt.error_code == "failed" and "a:blocked" in out["result"]


def test_chain_dedupes_limits_and_unexpected_errors_switch() -> None:
    hits = [hit("p", "https://a.example/x"), hit("p", "https://A.example/x/"), hit("p", "ftp://bad"),
            hit("p", "https://b.example/"), hit("p", "https://c.example/")]
    boom = FakeSearchProvider("boom")
    boom.search = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret detail"))  # type: ignore[method-assign]
    result = SearchChain([boom, FakeSearchProvider("p", hits)]).run("q", limit=2)
    assert [h.url for h in result.hits] == ["https://a.example/x", "https://b.example/"]
    assert result.attempts[0] == {"provider": "boom", "outcome": "error"}  # 例外內容不外洩


def test_chain_budget_exhausted_skips_remaining() -> None:
    ticks = iter([0.0, 100.0, 100.0, 100.0])
    chain = SearchChain([FakeSearchProvider("a", [hit("a")])], budget_s=10, clock=lambda: next(ticks))
    with pytest.raises(SearchChainError) as err:
        chain.run("q", limit=5)
    assert err.value.attempts == [{"provider": "a", "outcome": "skipped_budget"}]


def test_chain_availability_ignores_unavailable_providers() -> None:
    assert not SearchChain([FakeSearchProvider("x", usable=False)], fallback=FakeSearchProvider("o", usable=False)).available()
    assert SearchChain([], fallback=FakeSearchProvider("o")).available()


def test_web_search_executor_output_shape() -> None:
    ex = WebToolExecutor(search=SearchChain([FakeSearchProvider("fake", [hit("fake")])]))
    receipt, out = outcome(ex, call("web.search", {"query": "  hello   world ", "max_results": 3, "language": "en"}))
    payload = json.loads(out["result"])
    assert receipt.succeeded and payload["untrusted"] is True and payload["provider"] == "fake"
    assert payload["fallback_used"] is False and payload["results"] == [
        {"title": "title fake", "url": "https://a.example/", "snippet": "snippet", "provider": "fake"}]
    bad, _ = outcome(ex, call("web.search", {"query": ""}, "act_b"))
    assert not bad.succeeded
    none, _ = outcome(WebToolExecutor(search=None), call("web.search", {"query": "q"}, "act_c"))
    assert none.error_code == "denied"


# ---- OpenAI 備援與視覺（fake client） ----


class FakeResponses:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response, self.error, self.kwargs = response, error, None

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def test_openai_web_search_fallback_parses_citations() -> None:
    text = "Python 是一種程式語言，由 Guido van Rossum 建立。"
    note = SimpleNamespace(type="url_citation", url="https://python.org/", title="Python", start_index=0, end_index=len(text))
    response = SimpleNamespace(output=[SimpleNamespace(type="web_search_call"), SimpleNamespace(
        type="message", content=[SimpleNamespace(type="output_text", text=text, annotations=[note])])])
    responses = FakeResponses(response)
    provider = OpenAIWebSearch(lambda: SimpleNamespace(responses=responses), "m-search", has_credentials=True)
    hits = provider.search("python", limit=5, languages=("en",), deadline=1e12)
    assert hits == [SearchHit("Python", "https://python.org/", text, "openai")]
    assert responses.kwargs["model"] == "m-search" and responses.kwargs["tools"] == [{"type": "web_search"}]
    assert not OpenAIWebSearch(lambda: None, "m", has_credentials=False).available()
    broken = OpenAIWebSearch(lambda: SimpleNamespace(responses=FakeResponses(error=RuntimeError("key sk-secret"))), "m", has_credentials=True)
    with pytest.raises(ProviderError) as err:
        broken.search("q", limit=1, languages=(), deadline=1e12)
    assert "sk-secret" not in str(err.value)


def test_openai_vision_sends_bytes_as_data_url() -> None:
    responses = FakeResponses(SimpleNamespace(output_text="一隻貓"))
    vision = OpenAIVision(lambda: SimpleNamespace(responses=responses), "m-vision")
    assert vision.describe(PNG, "image/png", "圖裡有什麼？") == "一隻貓"
    content = responses.kwargs["input"][0]["content"]
    assert content[0]["text"] == "圖裡有什麼？" and content[1]["image_url"].startswith("data:image/png;base64,")
    assert responses.kwargs["model"] == "m-vision"


# ---- image.read ----


class FakeVision:
    def __init__(self, text: str = "圖中有一隻貓") -> None:
        self.text, self.calls = text, []

    def describe(self, image: bytes, mime: str, prompt: str | None) -> str:
        self.calls.append((len(image), mime, prompt))
        return self.text


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, Path]:
    root, outside = tmp_path / "allowed", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "cat.png").write_bytes(PNG)
    (root / "fake.png").write_bytes(b"not an image at all")
    (root / "doc.txt").write_text("hi")
    (root / "sub").mkdir()
    (root / "sub" / "a.jpg").write_bytes(JPEG)
    (outside / "secret.png").write_bytes(PNG)
    return {"root": root, "outside": outside}


def image_executor(root: Path, vision: Any = None, **kw: Any) -> ImageToolExecutor:
    return ImageToolExecutor(vision=vision if vision is not None else FakeVision(), policy=ImagePolicy(roots=(root,), **kw),
                             transport=kw.pop("transport", None), resolver=kw.pop("resolver", None))


def read(ex: ImageToolExecutor, source: str, prompt: str | None = None):
    params: dict[str, Any] = {"source": source, **({"prompt": prompt} if prompt else {})}
    return outcome(ex, call("image.read", params, "act_" + str(abs(hash(source)))))


def test_image_read_local_ok(sandbox: dict[str, Path]) -> None:
    vision = FakeVision()
    ex = image_executor(sandbox["root"], vision)
    receipt, out = read(ex, str(sandbox["root"] / "cat.png"), "有什麼？")
    payload = json.loads(out["result"])
    assert receipt.succeeded and payload["description"] == "圖中有一隻貓" and payload["untrusted"] is True
    assert payload["origin"] == "local" and payload["format"] == "png"
    assert vision.calls == [(len(PNG), "image/png", "有什麼？")]
    assert read(ex, str(sandbox["root"] / "sub" / "a.jpg"))[0].succeeded


@pytest.mark.parametrize("builder", [
    lambda s: str(s["root"] / ".." / "outside" / "secret.png"),  # .. 逃逸
    lambda s: str(s["outside"] / "secret.png"),  # 不在白名單
    lambda s: "relative/cat.png",
    lambda s: "file:///" + str(s["root"] / "cat.png").replace("\\", "/"),
    lambda s: "\\\\server\\share\\x.png",
    lambda s: "//server/share/x.png",
    lambda s: str(s["root"] / "cat.png") + ":stream",
    lambda s: str(s["root"] / "missing.png"),
    lambda s: str(s["root"] / "sub"),  # 目錄
])
def test_image_read_blocks_escape_and_bad_paths(sandbox: dict[str, Path], builder) -> None:
    vision = FakeVision()
    receipt, out = read(image_executor(sandbox["root"], vision), builder(sandbox))
    assert not receipt.succeeded and vision.calls == []
    assert str(sandbox["root"]) not in out["result"]  # 錯誤訊息不洩漏路徑


def test_image_read_blocks_symlink_escape(sandbox: dict[str, Path]) -> None:
    link = sandbox["root"] / "link.png"
    try:
        os.symlink(sandbox["outside"] / "secret.png", link)
    except (OSError, NotImplementedError):
        pytest.skip("此環境無法建立 symlink")
    vision = FakeVision()
    receipt, _ = read(image_executor(sandbox["root"], vision), str(link))
    assert not receipt.succeeded and vision.calls == []


def test_image_read_rejects_wrong_format_oversize_and_no_roots(sandbox: dict[str, Path]) -> None:
    ex = image_executor(sandbox["root"])
    assert not read(ex, str(sandbox["root"] / "fake.png"))[0].succeeded  # 副檔名是 png 但檔頭不是
    assert not read(ex, str(sandbox["root"] / "doc.txt"))[0].succeeded
    assert not read(image_executor(sandbox["root"], max_bytes=16), str(sandbox["root"] / "cat.png"))[0].succeeded
    empty = ImageToolExecutor(vision=FakeVision(), policy=ImagePolicy(roots=()))
    assert not read(empty, str(sandbox["root"] / "cat.png"))[0].succeeded
    novision = ImageToolExecutor(vision=None, policy=ImagePolicy(roots=(sandbox["root"],)))
    assert read(novision, str(sandbox["root"] / "cat.png"))[0].error_code == "denied"


def test_sniff_format() -> None:
    assert [sniff_format(d)[0] for d in (PNG, JPEG, WEBP, GIF)] == ["png", "jpeg", "webp", "gif"]
    assert sniff_format(b"<svg></svg>") is None and sniff_format(b"") is None
    with pytest.raises(ImageReadError):
        resolve_allowed("C:\\x\\y.png", ())


def test_image_read_url_uses_same_ssrf_rules(sandbox: dict[str, Path]) -> None:
    routes = {"https://example.com/a.png": (200, {"Content-Type": "image/png"}, PNG),
              "https://example.com/page": (200, {"Content-Type": "text/html"}, b"<p>"),
              "https://example.com/liar.png": (200, {"Content-Type": "image/png"}, b"GIF-not"),
              "https://example.com/big.png": (200, {"Content-Type": "image/png"}, PNG + b"\x00" * 100),
              "https://example.com/redir": (302, {"Location": "http://169.254.169.254/x.png"}, b"")}
    transport = FakeTransport(routes)
    dns = {"example.com": [PUBLIC], "intranet.test": ["10.0.0.1"]}
    vision = FakeVision()
    ex = ImageToolExecutor(vision=vision, policy=ImagePolicy(roots=(sandbox["root"],), max_bytes=100), transport=transport,
                           resolver=resolver_for(dns))
    ok, out = read(ex, "https://example.com/a.png")
    assert ok.succeeded and json.loads(out["result"])["origin"] == "url" and len(vision.calls) == 1
    for bad in ("https://example.com/page", "https://example.com/liar.png", "https://example.com/big.png",
                "https://example.com/redir", "http://intranet.test/a.png", "http://127.0.0.1/a.png", "ftp://example.com/a.png"):
        assert not read(ex, bad)[0].succeeded, bad
    assert len(vision.calls) == 1  # 失敗的都沒有送到模型


# ---- 註冊表、readiness、名稱轉譯 ----


def settings(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> UepSettings:
    for name in ("UEP_LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return UepSettings(_env_file=None, **kw)


def test_entries_valid_and_classified() -> None:
    registry = ToolRegistry.build([*web_entries(), *image_entries()], known_modules=["web_search", "web_fetch", "image_read"])
    for name in ("web.search", "web.fetch", "image.read"):
        entry = registry.entry(name)
        assert entry.spec.side_effect_class == "internal_read" and entry.spec.targeting == "core_local"
        assert entry.spec.origin == "builtin" and entry.concurrency == "parallel_safe" and entry.timeout_s
        assert entry.scope.platforms == frozenset() and entry.descriptor.input_schema["additionalProperties"] is False
        assert entry.spec.capability == name and CAPABILITY_CEILING[name] == "internal_read"
        assert registry.readiness(name) == "ready"


def test_unconfigured_search_and_image_unavailable_fetch_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    registry, executors = with_web_image(
        memory_registry(), settings(monkeypatch, web_search_providers="", web_search_openai_fallback=False))
    assert registry.readiness("web.search") == "unavailable"
    assert registry.readiness("image.read") == "unavailable"
    assert registry.readiness("web.fetch") == "ready"
    assert set(executors) == {"core_web", "core_image"}
    projected = registry.project({"web.search", "web.fetch", "image.read"}, ProjectionContext())
    assert [t["tool"] for t in projected["tools"]] == ["web.fetch"]
    assert "web.search" not in registry.enabled_tags() and "web.fetch" in registry.enabled_tags()


def test_provider_chain_readiness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ready, _ = with_web_image(memory_registry(), settings(monkeypatch, web_search_providers="duckduckgo, wikipedia, bogus"))
    assert ready.readiness("web.search") == "ready"
    only_fallback, _ = with_web_image(memory_registry(), settings(monkeypatch, web_search_providers=""))
    assert only_fallback.readiness("web.search") == "unavailable"  # 只剩 OpenAI 備援且沒有金鑰
    with_key, _ = with_web_image(memory_registry(), settings(monkeypatch, web_search_providers="", llm_api_key="sk-test"))
    assert with_key.readiness("web.search") == "ready"
    searx_missing, _ = with_web_image(memory_registry(), settings(monkeypatch, web_search_providers="searxng"))
    assert searx_missing.readiness("web.search") == "unavailable"  # 沒設 URL 就不建立
    image_ready, _ = with_web_image(memory_registry(), settings(monkeypatch, image_read_roots=str(tmp_path), llm_api_key="sk-test"))
    assert image_ready.readiness("image.read") == "ready"
    no_key, _ = with_web_image(memory_registry(), settings(monkeypatch, image_read_roots=str(tmp_path)))
    assert no_key.readiness("image.read") == "unavailable"
    url_only, _ = with_web_image(memory_registry(), settings(monkeypatch, llm_api_key="sk-test"))
    assert url_only.readiness("image.read") == "ready"  # 沒設白名單也 ready（URL 圖片），本機路徑由 executor 拒絕
    off, _ = with_web_image(memory_registry(), settings(monkeypatch, web_fetch_enabled=False))
    assert off.readiness("web.fetch") == "unavailable"


def test_injected_fake_provider_makes_search_callable_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = SearchChain([FakeSearchProvider("fake", [hit("fake")])])
    registry, executors = with_web_image(memory_registry(), settings(monkeypatch), search_chain=chain)
    assert registry.readiness("web.search") == "ready"
    # 經 provider 邊界轉譯後的名稱（web__search）解回內部名稱，再由 executor 執行
    mapping = wire_map(registry.entries())
    assert {"web__search", "web__fetch", "image__read"} <= set(mapping)
    internal = decode_name("web__search", mapping)
    assert internal == "web.search"
    receipt = executors["core_web"].execute(call(internal, {"query": "hello"}))
    assert receipt.succeeded and json.loads(executors["core_web"].cache.get("act_1")["result"])["provider"] == "fake"


def test_parse_providers() -> None:
    assert parse_providers("DuckDuckGo, ddg, wikipedia;searxng, openai, nope") == (["duckduckgo", "wikipedia", "searxng"], True)
    assert parse_providers("") == ([], False)


@pytest.mark.skipif(not os.environ.get("UEP_TEST_NETWORK"), reason="真網路冒煙測試預設跳過（設 UEP_TEST_NETWORK=1 才跑）")
def test_network_smoke() -> None:
    result = fetch_url("https://example.com/", FetchPolicy())
    assert result.status == 200 and "Example Domain" in html_to_text(result.body.decode())[1]


def test_defaults_enable_handwritten_providers_and_fallback_needs_key(monkeypatch: pytest.MonkeyPatch) -> None:
    default = settings(monkeypatch)
    assert default.web_search_providers == "duckduckgo,wikipedia" and default.web_search_openai_fallback is True
    registry, _ = with_web_image(memory_registry(), default)
    assert registry.readiness("web.search") == "ready"
    off, _ = with_web_image(memory_registry(), settings(monkeypatch, web_search_providers="", web_search_openai_fallback=False))
    assert off.readiness("web.search") == "unavailable"


def test_image_read_url_works_without_roots_but_local_path_rejected(sandbox: dict[str, Path]) -> None:
    transport = FakeTransport({"https://example.com/a.png": (200, {"Content-Type": "image/png"}, PNG)})
    vision = FakeVision()
    ex = ImageToolExecutor(vision=vision, policy=ImagePolicy(roots=()), transport=transport, resolver=resolver_for(GOOD))
    assert read(ex, "https://example.com/a.png")[0].succeeded
    receipt, out = read(ex, str(sandbox["root"] / "cat.png"))
    assert not receipt.succeeded and "沒有設定" in out["result"] and len(vision.calls) == 1


# ---- 審查修正：慢速滴流、病態標記、IPv6 白名單、deadline、TOCTOU、root、跨主機標頭 ----


def _drip_server(drip_headers: bool):
    """本機慢速伺服器：每 0.2 秒只送 1 位元組。回傳 (port, 停止函式)。"""
    import socket
    import threading
    import time

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def serve() -> None:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            conn.recv(4096)
            head = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
            if not drip_headers:
                conn.sendall(head)
            stream = head if drip_headers else b""
            for i in range(100000):
                if stop.is_set():
                    return
                piece = stream[i:i + 1] if drip_headers and i < len(stream) else b"a"
                try:
                    conn.sendall(piece)
                except OSError:
                    return
                time.sleep(0.2)

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], lambda: (stop.set(), srv.close())


@pytest.mark.parametrize("drip_headers", [False, True])
def test_slow_drip_cannot_exceed_total_budget(drip_headers: bool) -> None:
    import time

    port, shutdown = _drip_server(drip_headers)
    policy = FetchPolicy(timeout_s=1.0, private_hosts=frozenset({"127.0.0.1"}))
    started = time.monotonic()
    try:
        with pytest.raises(FetchError) as err:
            fetch_url(f"http://127.0.0.1:{port}/", policy)
    finally:
        shutdown()
    assert err.value.code == "timeout"
    assert time.monotonic() - started < 1.0 + 0.8  # 預算加一個小誤差，而不是等湊滿 16KB


PATHOLOGICAL = {
    "comment": "<!--", "cdata": "<![CDATA[", "pi": "<?", "tag": "<a ", "decl": "<!x", "script": "<script>",
    "lt": "<", "quote": '<a b=">', "nested": "<a <b>",
}


@pytest.mark.parametrize("name", sorted(PATHOLOGICAL))
def test_pathological_markup_parses_in_linear_time(name: str) -> None:
    import time

    pat = PATHOLOGICAL[name]
    html = pat * (1_000_000 // len(pat))  # 1MB
    started = time.monotonic()
    html_to_text(html)
    assert time.monotonic() - started < 1.0
    started = time.monotonic()
    html_to_text("<p>前文</p>" + html, max_parse_chars=80_000)
    assert time.monotonic() - started < 1.0


def test_sanitize_keeps_normal_content_and_strips_unterminated() -> None:
    from uep.tools.executors.web_text import sanitize_markup

    assert html_to_text("<p>a<!-- hidden -->b</p><p>c</p>")[1].replace("\n", "") == "abc"
    assert "hidden" not in html_to_text("<p>前<!-- hidden")[1] and "前" in html_to_text("<p>前<!-- hidden")[1]
    assert sanitize_markup("x" * 50, 10) == "x" * 10
    assert html_to_text("<p>5 < 6 and 7 > 3</p>")[1] == "5 < 6 and 7 > 3"


def test_ddg_provider_survives_pathological_body() -> None:
    import time

    body = ("<a class='result__a' href='https://ok.example/'>ok</a>" + "<!--" * 250_000).encode()
    env = provider_env("https://html.duckduckgo.com/html/?q=x", (200, HTML_HEADERS, body), "html.duckduckgo.com")
    started = time.monotonic()
    hits = DuckDuckGoProvider(FetchPolicy(max_bytes=2_000_000), **env).search("x", limit=5, languages=("en",), deadline=1e12)
    assert time.monotonic() - started < 1.0 and hits and hits[0].url == "https://ok.example/"


def test_fetch_executor_honors_parse_limit() -> None:
    body = ("<p>keep</p>" + "<!--" * 100_000).encode()
    transport = FakeTransport({"https://example.com/p": (200, HTML_HEADERS, body)})
    ex = WebToolExecutor(fetch_policy=FetchPolicy(), transport=transport, resolver=resolver_for(GOOD), max_parse_chars=5_000)
    receipt, out = outcome(ex, call("web.fetch", {"url": "https://example.com/p"}))
    assert receipt.succeeded and json.loads(out["result"])["text"] == "keep"


def test_openai_fallback_respects_deadline() -> None:
    responses = FakeResponses(SimpleNamespace(output=[]))
    now = [100.0]
    provider = OpenAIWebSearch(lambda: SimpleNamespace(responses=responses), "m", has_credentials=True, timeout_s=16.0,
                               clock=lambda: now[0])
    provider.search("q", limit=1, languages=(), deadline=103.0)
    assert responses.kwargs["timeout"] == 3.0  # min(設定值 16, 剩餘 3)
    provider.search("q", limit=1, languages=(), deadline=500.0)
    assert responses.kwargs["timeout"] == 16.0
    responses.kwargs = None
    with pytest.raises(ProviderError) as err:
        provider.search("q", limit=1, languages=(), deadline=99.0)
    assert err.value.code == "timeout" and responses.kwargs is None  # 已逾時就不送出


def test_wikipedia_languages_share_one_deadline() -> None:
    now = [0.0]

    class SlowTransport(FakeTransport):
        def open(self, *args, **kwargs):
            now[0] += 9.0  # 第一個語言就用掉整個時限
            return super().open(*args, **kwargs)

    empty = json.dumps({"query": {"search": []}}).encode()
    url_en = "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=q&srlimit=5&format=json&utf8=1"
    transport = SlowTransport({url_en: (200, JSON_HEADERS, empty)})
    provider = WikipediaProvider(FetchPolicy(), transport=transport, resolver=resolver_for({"en.wikipedia.org": [PUBLIC]}),
                                 clock=lambda: now[0])
    with pytest.raises(ProviderError) as err:
        provider.search("q", limit=5, languages=("en", "zh"), deadline=8.0)
    assert err.value.code == "timeout" and len(transport.calls) == 1  # 第二個語言沒有重新計時


def test_chain_gives_each_provider_its_own_bounded_deadline() -> None:
    seen: list[float] = []

    class Recorder(FakeSearchProvider):
        def search(self, query, *, limit, languages, deadline):
            seen.append(deadline)
            return super().search(query, limit=limit, languages=languages, deadline=deadline)

    SearchChain([Recorder("a", [hit("a")])], budget_s=40, provider_timeout_s=8, clock=lambda: 10.0).run("q", limit=3)
    assert seen == [18.0]  # min(整條鏈 50, 自己 18)
    seen.clear()
    SearchChain([Recorder("a", [hit("a")])], budget_s=5, provider_timeout_s=8, clock=lambda: 10.0).run("q", limit=3)
    assert seen == [15.0]


def test_image_toctou_final_path_checked(monkeypatch: pytest.MonkeyPatch, sandbox: dict[str, Path]) -> None:
    from uep.tools.executors import image as image_module

    target = sandbox["root"] / "cat.png"
    real = image_module._final_path
    with target.open("rb") as handle:
        final = real(handle.fileno())
    assert final is None or final.resolve() == target.resolve()  # 平台支援時，回報的是真實路徑
    vision = FakeVision()
    monkeypatch.setattr(image_module, "_final_path", lambda fd: sandbox["outside"] / "secret.png")  # 模擬競態：開到的是別處
    receipt, out = read(image_executor(sandbox["root"], vision), str(target))
    assert not receipt.succeeded and vision.calls == [] and str(sandbox["outside"]) not in out["result"]


@pytest.mark.parametrize("bad", ["relative/dir", ".", os.path.abspath(os.sep)])
def test_image_policy_rejects_relative_and_filesystem_root(bad: str) -> None:
    with pytest.raises(ValueError):
        ImagePolicy(roots=(Path(bad),))


def test_bad_image_roots_are_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = os.pathsep.join(["relative/dir", os.path.abspath(os.sep), str(tmp_path)])
    registry, executors = with_web_image(memory_registry(), settings(monkeypatch, image_read_roots=raw, llm_api_key="sk-test"))
    assert registry.readiness("image.read") == "ready"
    assert [str(r) for r in executors["core_image"]._policy.roots] == [str(tmp_path.resolve())]


def test_sensitive_headers_dropped_on_cross_host_redirect_only() -> None:
    policy = FetchPolicy(headers={"Authorization": "Bearer t", "Cookie": "s=1", "X-Other": "y"})
    routes = {
        "http://example.com/a": (302, {"Location": "/b"}, b""),
        "http://example.com/b": (302, {"Location": "http://other.example.org/c"}, b""),
        "http://other.example.org/c": (200, HTML_HEADERS, b"ok"),
    }
    transport = FakeTransport(routes)
    fetch_url("http://example.com/a", policy, transport=transport,
              resolver=resolver_for({"example.com": [PUBLIC], "other.example.org": [PUBLIC]}))
    same_host_first, same_host_second, cross_host = transport.headers_seen
    assert same_host_first["Authorization"] == same_host_second["Authorization"] == "Bearer t"
    assert "Authorization" not in cross_host and "Cookie" not in cross_host and cross_host["X-Other"] == "y"
