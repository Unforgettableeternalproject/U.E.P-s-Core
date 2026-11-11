"""web／image 工具的組裝：設定 -> provider 鏈、視覺模型、executor、併入註冊表（readiness 由 ``requires``／``known_modules`` 表達）。

``with_web_image(registry)`` 與 ``with_testkit`` 同形：回傳新的（已驗證、已凍結）註冊表與 executor 映射，不改動傳入的實例。
條目永遠註冊（lockfile 與 TG-5 雙向檢查需要 executor 在），可用與否只由 ``known_modules`` 決定：
未設定的工具 readiness 為 ``unavailable``，不投出、不貢獻 capability 標籤；組裝不會因此失敗。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from uep.config import UepSettings, get_settings
from uep.tools.catalog.image import (
    IMAGE_EXECUTOR,
    MODULE_IMAGE_READ,
    TOOL_IMAGE_READ,
    image_entries,
)
from uep.tools.catalog.web import (
    MODULE_WEB_FETCH,
    MODULE_WEB_SEARCH,
    TOOL_WEB_FETCH,
    TOOL_WEB_SEARCH,
    WEB_EXECUTOR,
    web_entries,
)
from uep.tools.executors.image import ImagePolicy, ImageToolExecutor, VisionPort
from uep.tools.executors.web import WebToolExecutor
from uep.tools.executors.web_http import FetchPolicy, Resolver, Transport
from uep.tools.executors.web_search import (
    DuckDuckGoProvider,
    SearchChain,
    SearchProvider,
    SearxngProvider,
    WikipediaProvider,
)
from uep.tools.outcomes import TurnOutcomeCache
from uep.tools.registry import ToolRegistry

logger = logging.getLogger("uep.tools.web_image")

WEB_IMAGE_TOOLS: tuple[str, ...] = (TOOL_WEB_SEARCH, TOOL_WEB_FETCH, TOOL_IMAGE_READ)
WEB_IMAGE_TAGS: frozenset[str] = frozenset(WEB_IMAGE_TOOLS)  # 能力標籤與工具名同名
_PROVIDER_ALIASES = {"duckduckgo": "duckduckgo", "ddg": "duckduckgo", "wikipedia": "wikipedia", "searxng": "searxng", "openai": "openai"}


def parse_providers(raw: str) -> tuple[list[str], bool]:
    """``(依序的自寫 provider 名稱, 清單內是否列了 openai)``；未知名稱警告並略過、重複只留第一個。"""
    names: list[str] = []
    wants_openai = False
    for token in raw.replace("；", ",").replace(";", ",").split(","):
        token = token.strip().lower()
        if not token:
            continue
        canonical = _PROVIDER_ALIASES.get(token)
        if canonical is None:
            logger.warning("UEP_WEB_SEARCH_PROVIDERS 含未知的搜尋來源 %r，已略過", token)
        elif canonical == "openai":
            wants_openai = True
        elif canonical not in names:
            names.append(canonical)
    return names, wants_openai


def _split(raw: str, sep: str) -> list[str]:
    return [item.strip() for item in raw.split(sep) if item.strip()]


def build_search_chain(
    settings: UepSettings, *, transport: Transport | None = None, resolver: Resolver | None = None,
    client_factory: Callable[[float], Any] | None = None, extra_providers: list[SearchProvider] | None = None,
) -> SearchChain:
    """依設定組出 provider 鏈。``extra_providers`` 供測試或離線 Console 注入 fake provider（排在設定的 provider 之後）。"""
    names, listed_openai = parse_providers(settings.web_search_providers)
    policy = FetchPolicy(
        timeout_s=settings.web_search_timeout_s, max_bytes=settings.web_fetch_max_bytes,
        max_redirects=settings.web_fetch_max_redirects,
    )
    kwargs: dict[str, Any] = {"transport": transport, "resolver": resolver}
    providers: list[SearchProvider] = []
    for name in names:
        if name == "duckduckgo":
            providers.append(DuckDuckGoProvider(policy, **kwargs))
        elif name == "wikipedia":
            providers.append(WikipediaProvider(policy, **kwargs))
        elif name == "searxng":
            url = (settings.web_searxng_url or "").strip()
            if not url:
                logger.warning("UEP_WEB_SEARCH_PROVIDERS 列了 searxng，但沒有設定 UEP_WEB_SEARXNG_URL，已略過")
                continue
            private = frozenset(h.lower() for h in _split(settings.web_searxng_private_hosts, ","))
            searx_policy = FetchPolicy(
                timeout_s=policy.timeout_s, max_bytes=policy.max_bytes, max_redirects=policy.max_redirects,
                private_hosts=private,
            )
            providers.append(SearxngProvider(url, searx_policy, **kwargs))
    providers.extend(extra_providers or [])
    fallback = None
    if settings.web_search_openai_fallback or listed_openai:
        from uep.models.openai_tools import OpenAIWebSearch, make_client

        think = settings.think_config()
        factory = client_factory or (lambda timeout: make_client(think, timeout))
        model = (settings.web_search_openai_model or "").strip() or think.model
        fallback = OpenAIWebSearch(
            lambda: factory(settings.web_search_timeout_s * 2), model, has_credentials=bool(think.api_key),
        )
    return SearchChain(providers, fallback=fallback, budget_s=settings.web_search_budget_s)


def build_vision(
    settings: UepSettings, *, client_factory: Callable[[float], Any] | None = None,
) -> VisionPort | None:
    """沿用 Core 的 openai 憑證；沒有金鑰回 None（image.read 因此 unavailable）。模型名走 ``UEP_IMAGE_READ_MODEL``，預設對話模型。"""
    think = settings.think_config()
    if not think.api_key and client_factory is None:
        return None
    from uep.models.openai_tools import OpenAIVision, make_client

    factory = client_factory or (lambda timeout: make_client(think, timeout))
    model = (settings.image_read_model or "").strip() or think.model
    return OpenAIVision(lambda: factory(30.0), model)


def image_roots(settings: UepSettings) -> tuple[str, ...]:
    return tuple(_split(settings.image_read_roots, os.pathsep))


def with_web_image(
    registry: ToolRegistry, settings: UepSettings | None = None, *, cache: TurnOutcomeCache | None = None,
    transport: Transport | None = None, resolver: Resolver | None = None,
    search_chain: SearchChain | None = None, vision: VisionPort | None = None,
    client_factory: Callable[[float], Any] | None = None,
) -> tuple[ToolRegistry, Mapping[str, Any]]:
    """併入 web／image 條目，回傳 ``(新註冊表, {executor 名: executor})``。已含這些工具者原樣回傳註冊表（冪等）但仍給 executor。

    ``search_chain``／``vision`` 可注入（測試與離線 Console）；省略則依設定組。"""
    settings = settings or get_settings()
    chain = search_chain if search_chain is not None else build_search_chain(
        settings, transport=transport, resolver=resolver, client_factory=client_factory,
    )
    roots = image_roots(settings)
    vision_port = vision if vision is not None else (build_vision(settings, client_factory=client_factory) if roots else None)
    fetch_policy = FetchPolicy(
        timeout_s=settings.web_fetch_timeout_s, max_bytes=settings.web_fetch_max_bytes,
        max_redirects=settings.web_fetch_max_redirects,
    )
    executors = {
        WEB_EXECUTOR: WebToolExecutor(
            search=chain, fetch_policy=fetch_policy, fetch_max_chars=settings.web_fetch_max_chars,
            transport=transport, resolver=resolver, cache=cache,
        ),
        IMAGE_EXECUTOR: ImageToolExecutor(
            vision=vision_port, transport=transport, resolver=resolver, cache=cache,
            policy=ImagePolicy(
                roots=tuple(Path(r) for r in roots), max_bytes=settings.image_read_max_bytes,
                url_timeout_s=settings.web_fetch_timeout_s, max_redirects=settings.web_fetch_max_redirects,
            ),
        ),
    }
    modules = set(getattr(registry, "_known_modules", frozenset()))
    if chain.available():
        modules.add(MODULE_WEB_SEARCH)
    if settings.web_fetch_enabled:
        modules.add(MODULE_WEB_FETCH)
    if roots and vision_port is not None:
        modules.add(MODULE_IMAGE_READ)
    existing = registry.entries()
    if any(name in existing for name in WEB_IMAGE_TOOLS):
        return registry, executors
    built = ToolRegistry.build([*existing.values(), *web_entries(), *image_entries()], known_modules=modules)
    return built, executors
