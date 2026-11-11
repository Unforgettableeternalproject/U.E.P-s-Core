"""web 工具共用的安全 HTTP 抓取層（web.fetch、image.read 的 URL 圖片、web.search 的各 provider 都走這裡）。

只用標準函式庫（http.client／ssl／ipaddress），不依賴 httpx（它只是 openai 的間接依賴）。安全邊界：

- 只允許 http／https；URL 不得帶帳密；非白名單主機只許 80／443 埠。
- SSRF：主機名解析出的**每一個**位址都必須是公網位址（``ipaddress.is_global``；私有、loopback、link-local、保留、
  CGNAT、multicast 都擋，IPv4-mapped／NAT64／6to4 先拆出內嵌的 IPv4 再判）。連線時直接連驗證過的 IP
  （SNI 與 Host 仍用原主機名），不做第二次解析，避免 DNS rebinding。雲端 metadata 位址永遠擋，白名單也豁免不了。
- redirect 不自動跟隨：每一跳都重新解析、重新檢查，並有跳數上限。
- 不信任 ``Content-Length``，以實際讀到的位元組計數；``Accept-Encoding: identity``，不解壓縮（防壓縮炸彈）；
  一條單調時鐘的總時限涵蓋所有跳與讀取。
- 只接受呼叫端宣告的 content-type。
- 私網白名單（``FetchPolicy.private_hosts``）只豁免「該 policy 直接請求的主機名」，由呼叫端明確設定（目前只有 SearXNG provider），
  web.fetch 與圖片 URL 永遠不豁免。
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin, urlsplit

TEXT_TYPES: tuple[str, ...] = ("text/*", "application/xhtml+xml")
JSON_TYPES: tuple[str, ...] = ("application/json", "text/json", "text/plain")
IMAGE_TYPES: tuple[str, ...] = ("image/png", "image/jpeg", "image/webp", "image/gif")
# 通用 User-Agent：不帶專案名稱或聯絡資訊
USER_AGENT = "Mozilla/5.0 (compatible; text-fetcher/1.0)"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DEFAULT_PORTS = {"http": 80, "https": 443}
_CHUNK = 16 * 1024
# 永遠拒絕的位址（雲端 metadata），白名單也不豁免
_ALWAYS_BLOCKED = frozenset(
    ipaddress.ip_address(a) for a in ("169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254")
)


class FetchError(Exception):
    """抓取失敗；``code`` 是分類（bad_url／blocked／timeout／too_large／bad_type／http_error／network／redirects）。
    訊息只放分類原因，不含外部回應內容。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FetchPolicy:
    timeout_s: float = 10.0
    max_bytes: int = 1_000_000
    max_redirects: int = 5
    accept_types: tuple[str, ...] = TEXT_TYPES
    private_hosts: frozenset[str] = frozenset()
    user_agent: str = USER_AGENT
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content_type: str
    charset: str | None
    body: bytes


class Opened(Protocol):
    status: int
    headers: Mapping[str, str]  # 鍵為小寫

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


class Transport(Protocol):
    def open(
        self, scheme: str, host: str, port: int, ip: str, target: str, headers: Mapping[str, str], timeout_s: float,
    ) -> Opened:
        """連到 ``ip``（已驗證）送 GET；``host`` 供 Host 標頭與 TLS SNI／憑證驗證。"""
        ...


Resolver = Callable[[str], list[str]]


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise FetchError("network", "無法解析主機名稱") from exc
    return list(dict.fromkeys(str(info[4][0]).split("%")[0] for info in infos))


def _embedded_v4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr in ipaddress.ip_network("64:ff9b::/96"):  # NAT64
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if addr in ipaddress.ip_network("2002::/16"):  # 6to4
        return ipaddress.IPv4Address((int(addr) >> 80) & 0xFFFFFFFF)
    return None


def is_public_ip(value: str) -> bool:
    """是否為可對外連線的公網位址。無法解析的字串一律視為不安全。"""
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        inner = _embedded_v4(addr)
        if inner is not None:
            addr = inner
    return addr.is_global and not addr.is_multicast and addr not in _ALWAYS_BLOCKED


def _is_always_blocked(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address):
        inner = _embedded_v4(addr)
        if inner is not None:
            addr = inner
    return addr in _ALWAYS_BLOCKED or addr.is_multicast or addr.is_unspecified


def _parse(url: str, policy: FetchPolicy) -> tuple[str, str, int, str, bool]:
    """``(scheme, host, port, target, exempt)``；不合法即 ``bad_url``／``blocked``。"""
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        port = parts.port
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise FetchError("bad_url", "網址格式不正確") from exc
    if scheme not in DEFAULT_PORTS:
        raise FetchError("bad_url", "只允許 http 或 https 網址")
    if not host or parts.username is not None or parts.password is not None:
        raise FetchError("bad_url", "網址缺少主機名稱，或帶有帳密")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise FetchError("bad_url", "主機名稱格式不正確") from exc
    exempt = host in policy.private_hosts
    port = port or DEFAULT_PORTS[scheme]
    if not exempt and port not in (80, 443):
        raise FetchError("blocked", "不允許的連接埠")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return scheme, host, port, target, exempt


def _validated_ips(host: str, exempt: bool, resolver: Resolver) -> list[str]:
    try:
        ipaddress.ip_address(host)
        ips = [host]  # 位址字面值不經解析
    except ValueError:
        ips = resolver(host)
    if not ips:
        raise FetchError("network", "無法解析主機名稱")
    for ip in ips:
        if _is_always_blocked(ip) or (not exempt and not is_public_ip(ip)):
            raise FetchError("blocked", "目標位址不在允許範圍（私有、本機或保留位址）")
    return ips


def _type_allowed(content_type: str, accept: tuple[str, ...]) -> bool:
    for pattern in accept:
        if pattern.endswith("/*"):
            if content_type.startswith(pattern[:-1]):
                return True
        elif content_type == pattern:
            return True
    return False


def _split_content_type(raw: str) -> tuple[str, str | None]:
    pieces = [p.strip() for p in raw.split(";")]
    charset = None
    for piece in pieces[1:]:
        key, _, value = piece.partition("=")
        if key.strip().lower() == "charset":
            charset = value.strip().strip("\"'").lower() or None
    return pieces[0].lower(), charset


def fetch_url(
    url: str,
    policy: FetchPolicy,
    *,
    transport: Transport | None = None,
    resolver: Resolver | None = None,
    clock: Callable[[], float] = time.monotonic,
    deadline: float | None = None,
) -> FetchResult:
    """安全抓取一個 URL（含手動 redirect）。``deadline`` 是外部給的單調時鐘截止點（搜尋鏈共用），省略則由 policy 起算。"""
    transport = transport or StdlibTransport()
    resolver = resolver or system_resolver
    limit = clock() + policy.timeout_s
    deadline = limit if deadline is None else min(deadline, limit)
    current = url
    for _hop in range(policy.max_redirects + 1):
        scheme, host, port, target, exempt = _parse(current, policy)
        ips = _validated_ips(host, exempt, resolver)
        remaining = deadline - clock()
        if remaining <= 0:
            raise FetchError("timeout", "抓取逾時")
        headers = {
            "User-Agent": policy.user_agent, "Accept": ", ".join(policy.accept_types) + ", */*;q=0.1",
            "Accept-Encoding": "identity", "Connection": "close", **policy.headers,
        }
        try:
            opened = transport.open(scheme, host, port, ips[0], target, headers, remaining)
        except TimeoutError as exc:
            raise FetchError("timeout", "連線逾時") from exc
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise FetchError("network", "連線失敗") from exc
        try:
            if opened.status in REDIRECT_STATUSES:
                location = opened.headers.get("location", "")
                if not location:
                    raise FetchError("http_error", "重新導向缺少目的地")
                current = urljoin(current, location)
                continue
            if not 200 <= opened.status < 300:
                raise FetchError("http_error", f"伺服器回應 {opened.status}")
            return _read_body(opened, current, policy, clock, deadline)
        finally:
            opened.close()
    raise FetchError("redirects", "重新導向次數過多")


def _read_body(opened: Opened, url: str, policy: FetchPolicy, clock: Callable[[], float], deadline: float) -> FetchResult:
    encoding = opened.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        raise FetchError("bad_type", "不支援壓縮編碼的回應")
    content_type, charset = _split_content_type(opened.headers.get("content-type", ""))
    if not content_type or not _type_allowed(content_type, policy.accept_types):
        raise FetchError("bad_type", "不支援的內容類型")
    declared = opened.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > policy.max_bytes:
        raise FetchError("too_large", "回應超過大小上限")
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            if clock() >= deadline:
                raise FetchError("timeout", "讀取逾時")
            chunk = opened.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > policy.max_bytes:  # 以實際位元組計數，不信任 Content-Length
                raise FetchError("too_large", "回應超過大小上限")
            chunks.append(chunk)
    except TimeoutError as exc:
        raise FetchError("timeout", "讀取逾時") from exc
    except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
        raise FetchError("network", "讀取失敗") from exc
    return FetchResult(url=url, status=opened.status, content_type=content_type, charset=charset, body=b"".join(chunks))


# ---- 真實 transport ----


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """連到已驗證的 IP，但 SNI 與憑證驗證用原主機名。"""

    def __init__(self, host: str, ip: str, port: int, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(ip, port, timeout=timeout, context=context)
        self._server_name = host

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._server_name)  # type: ignore[attr-defined]


class _StdlibOpened:
    def __init__(self, conn: http.client.HTTPConnection, response: http.client.HTTPResponse) -> None:
        self._conn = conn
        self._response = response
        self.status = response.status
        self.headers = {k.lower(): v for k, v in response.getheaders()}

    def read(self, size: int) -> bytes:
        return self._response.read(size)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._conn.close()


class StdlibTransport:
    def open(
        self, scheme: str, host: str, port: int, ip: str, target: str, headers: Mapping[str, str], timeout_s: float,
    ) -> Opened:
        conn: http.client.HTTPConnection
        if scheme == "https":
            conn = _PinnedHTTPSConnection(host, ip, port, timeout_s, ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(ip, port, timeout=timeout_s)
        host_header = host if port == DEFAULT_PORTS[scheme] else f"{host}:{port}"
        try:
            conn.request("GET", target, headers={"Host": host_header, **headers})
            return _StdlibOpened(conn, conn.getresponse())
        except BaseException:
            conn.close()
            raise
