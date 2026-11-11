"""HTML 轉純文字（標準函式庫 ``html.parser``；專案沒有 bs4／lxml）。

抓網頁只要可讀的文字：略過 script／style／noscript／template／svg／iframe 與 head 內容（保留 title），
區塊元素斷行，空白折疊，控制字元移除，總長度有上限。輸出仍是不可信外部內容，由呼叫端加標記。
"""
from __future__ import annotations

import contextlib
import re
from html.parser import HTMLParser

_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "object", "embed", "canvas"})
_BLOCK = frozenset({
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer", "nav", "aside",
    "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "hr", "form", "main", "dl", "dt", "dd", "figure",
})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?\s*([A-Za-z0-9_\-]+)", re.IGNORECASE)


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._in_head = False
        self.title_parts: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag == "head":
            self._in_head = True
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "head":
            self._in_head = False
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        elif not self._in_head:
            self.parts.append(data)


def collapse(text: str) -> str:
    """移除控制字元、折疊空白；保留段落換行（連續空行縮成一行）。"""
    text = _CONTROL.sub("", text)
    lines = [re.sub(r"[ \t 　]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def clip(text: str, limit: int) -> tuple[str, bool]:
    """截到 ``limit`` 字元；回傳 ``(文字, 是否被截斷)``。"""
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def decode_body(body: bytes, charset: str | None) -> str:
    """依 header charset、其次 meta charset、最後 utf-8 解碼；永不拋例外（無法解碼的位元組以替代字元代替）。"""
    candidates = [charset] if charset else []
    meta = _META_CHARSET.search(body[:4096])
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for name in candidates:
        try:
            return body.decode(name or "utf-8", errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def html_to_text(html: str) -> tuple[str, str]:
    """``(title, text)``，皆已折疊空白，未截長度。"""
    parser = _TextParser()
    with contextlib.suppress(Exception):  # 畸形 HTML 不致命：用已解析的部分
        parser.feed(html)
        parser.close()
    return collapse(" ".join(parser.title_parts)), collapse("".join(parser.parts))
