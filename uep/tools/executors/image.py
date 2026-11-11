"""``image.read`` 的 ``ToolExecutor``（宣告見 ``uep.tools.catalog.image``）。

- 本機路徑：必須是絕對路徑，經 ``resolve(strict=True)`` canonical 化（消掉 ``..``、跟隨 symlink 到真實位置）後，
  落在 ``UEP_IMAGE_READ_ROOTS`` 白名單目錄之內才讀；UNC／裝置路徑／``file:`` 與其他 scheme 一律拒絕。
- URL 圖片：走與 ``web.fetch`` 相同的安全抓取（SSRF、redirect 逐跳檢查、大小與逾時），只收 png／jpeg／webp／gif。
  由本程式抓位元組再交給視覺模型，不把 URL 交給模型（避免模型端去連內網）。
- 格式以檔頭 magic bytes 判定，不信副檔名或 content-type；大小有上限。
- 視覺模型是 ``VisionPort`` 協定（真實實作在 ``uep.models.openai_tools``）；描述結果標 ``untrusted=true``，
  圖片內的文字不是指令。錯誤訊息不含路徑原文。
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from uep.tools.catalog.image import PROMPT_MAX, SOURCE_MAX, TOOL_IMAGE_READ
from uep.tools.executors.base import (
    UNTRUSTED_NOTICE,
    ReadToolExecutor,
    denied,
    dump,
    failure,
)
from uep.tools.executors.web_http import (
    IMAGE_TYPES,
    FetchError,
    FetchPolicy,
    Resolver,
    Transport,
    fetch_url,
)

DESCRIPTION_MAX = 4000
_URL = re.compile(r"^https?://", re.IGNORECASE)
_OTHER_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]+://")


class VisionPort(Protocol):
    def describe(self, image: bytes, mime: str, prompt: str | None) -> str:
        """把圖片位元組交給視覺模型，回傳文字描述；失敗拋例外。"""
        ...


class ImageReadError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def sniff_format(data: bytes) -> tuple[str, str] | None:
    """``(格式名, mime)``；只認 png／jpeg／webp／gif。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg", "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


@dataclass(frozen=True)
class ImagePolicy:
    roots: tuple[Path, ...] = ()
    max_bytes: int = 5_000_000
    url_timeout_s: float = 10.0
    max_redirects: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "roots", tuple(Path(r).resolve() for r in self.roots))


def resolve_allowed(source: str, roots: Sequence[Path]) -> Path:
    """本機路徑 -> canonical 真實路徑；不在白名單或不是檔案即 ``ImageReadError``。"""
    if "\x00" in source or source.startswith(("\\\\", "//")) or _OTHER_SCHEME.match(source) or source.lower().startswith("file:"):
        raise ImageReadError("blocked", "只接受白名單目錄內的本機路徑或 http(s) 圖片網址")
    if ":" in os.path.splitdrive(source)[1]:  # 排除 NTFS 資料流（name.png:stream）
        raise ImageReadError("blocked", "路徑格式不允許")
    candidate = Path(source)
    if not candidate.is_absolute():
        raise ImageReadError("blocked", "本機路徑必須是絕對路徑")
    if not roots:
        raise ImageReadError("blocked", "沒有設定可讀取的圖片目錄")
    try:
        resolved = candidate.resolve(strict=True)  # 消掉 ..、跟隨 symlink；不存在即 OSError
    except (OSError, ValueError, RuntimeError) as exc:
        raise ImageReadError("not_found", "找不到圖片檔") from exc
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ImageReadError("blocked", "路徑不在允許讀取的目錄內")
    if not resolved.is_file():
        raise ImageReadError("not_found", "找不到圖片檔")
    return resolved


class ImageToolExecutor(ReadToolExecutor):
    def __init__(
        self, *, vision: VisionPort | None, policy: ImagePolicy | None = None, transport: Transport | None = None,
        resolver: Resolver | None = None, **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vision = vision
        self._policy = policy or ImagePolicy()
        self._transport = transport
        self._resolver = resolver

    def _run(self, tool: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if tool != TOOL_IMAGE_READ:
            return denied(tool)
        source, prompt = params.get("source"), params.get("prompt")
        if not isinstance(source, str) or not source.strip() or len(source) > SOURCE_MAX:
            return failure("failed", f"Invalid arguments for {tool!r}.")
        if prompt is not None and (not isinstance(prompt, str) or len(prompt) > PROMPT_MAX):
            return failure("failed", f"Invalid arguments for {tool!r}.")
        if self._vision is None:
            return failure("denied", "image.read 沒有可用的視覺模型。")
        source = source.strip()
        try:
            origin, data = self._load_url(source) if _URL.match(source) else self._load_local(source)
            detected = sniff_format(data)
            if detected is None:
                raise ImageReadError("bad_type", "不支援的圖片格式（只收 png、jpeg、webp、gif）")
            description = self._vision.describe(data, detected[1], (prompt or "").strip() or None)
        except ImageReadError as exc:
            return failure("failed", f"無法讀取圖片：{exc.message}（{exc.code}）")
        except FetchError as exc:
            return failure("timeout" if exc.code == "timeout" else "failed", f"無法取得圖片：{exc.message}（{exc.code}）")
        text = "".join(ch for ch in str(description) if ch == "\n" or ch >= " ").strip()
        if not text:
            return failure("failed", "視覺模型沒有回傳內容。")
        truncated = len(text) > DESCRIPTION_MAX
        return {"success": True, "result": dump({
            "untrusted": True, "notice": UNTRUSTED_NOTICE, "origin": origin, "format": detected[0], "bytes": len(data),
            "description": text[:DESCRIPTION_MAX], "truncated": truncated,
        })}

    def _load_local(self, source: str) -> tuple[str, bytes]:
        path = resolve_allowed(source, self._policy.roots)
        try:
            with path.open("rb") as handle:
                data = handle.read(self._policy.max_bytes + 1)
        except OSError as exc:
            raise ImageReadError("not_found", "無法讀取圖片檔") from exc
        if len(data) > self._policy.max_bytes:
            raise ImageReadError("too_large", "圖片超過大小上限")
        return "local", data

    def _load_url(self, source: str) -> tuple[str, bytes]:
        policy = FetchPolicy(
            timeout_s=self._policy.url_timeout_s, max_bytes=self._policy.max_bytes,
            max_redirects=self._policy.max_redirects, accept_types=IMAGE_TYPES,
        )
        result = fetch_url(source, policy, transport=self._transport, resolver=self._resolver)
        return "url", result.body
