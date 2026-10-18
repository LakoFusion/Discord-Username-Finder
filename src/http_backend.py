#!/usr/bin/env python3
"""Discord Username Finder v3.1 – Tầng HTTP client: backend thay thế được (curl_cffi + aiohttp).

Engine/checker chỉ làm việc với 2 kiểu mỏng: `BackendError` (đã chuẩn hoá
kind) và `Response`. curl_cffi là optional (lazy import): thiếu thì rơi êm
xuống aiohttp đã tinh chỉnh sâu (TCPConnector + SSL giả Chrome).

X-Fingerprint: trước POST đầu tiên của mỗi proxy, harvester GET /experiments
qua ĐÚNG proxy đó để lấy header bắt tay; mọi thất bại bỏ qua âm thầm. Tắt
hoàn toàn bằng use_fingerprint=False (không phát sinh request thêm).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import ssl
from dataclasses import dataclass
from typing import Any

import aiohttp

from src.config import DEFAULT_HEADERS, create_browser_ssl_context


# ---------------------------------------------------------------------------
# Lỗi chuẩn hoá – engine map thẳng kind sang cooldown / mark_dead
# ---------------------------------------------------------------------------

class BackendError(Exception):
    """Lỗi HTTP đã phân loại: kind ∈ {proxy_dead, timeout, tls, disconnect, unknown}."""

    def __init__(self, kind: str, message: str = "", proxy: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.proxy = proxy


class Response:
    """Kết quả gọn: status, JSON đã parse (None nếu không phải JSON), headers lowercase."""

    def __init__(self, status: int, data: Any, headers: dict[str, str]) -> None:
        self.status = status
        self.data = data
        self.headers = headers

    def header(self, name: str, default: str | None = None) -> str | None:
        """Tra header không phân biệt hoa/thường."""
        return self.headers.get(name.lower(), default)


# ---------------------------------------------------------------------------
# Timeout theo mode – giá trị vàng, chốt cứng với engine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TimeoutSpec:
    """Bộ timeout (giây): connect / read / total."""

    connect: float
    read: float
    total: float


def mode_timeout(mode: str, user_timeout: float) -> TimeoutSpec:
    """Quy đổi mode + timeout người dùng thành TimeoutSpec.

    u = max(1.0, user_timeout): chặn timeout rác < 1s bóp chết request oan.
    Mode lạ -> cấu hình "default".
    """
    u = max(1.0, float(user_timeout))
    m = str(mode or "").strip().lower()
    if m == "scraped":
        # Proxy free: chết nhanh để đổi proxy khác (one-shot per proxy).
        return TimeoutSpec(2.5, 4.0, min(u, 6.0))
    if m == "static":
        return TimeoutSpec(4.0, u, u + 2.0)
    if m == "rotating":
        return TimeoutSpec(3.0, u, u + 2.0)
    if m == "proxyless":
        # IP nhà: connect chậm hơn nhưng read cố định 8s, total sàn 13s.
        return TimeoutSpec(5.0, 8.0, max(u, 13.0))
    return TimeoutSpec(5.0, u, u + 2.0)


# ---------------------------------------------------------------------------
# Phân loại exception -> kind (chuẩn hoá cho engine)
# ---------------------------------------------------------------------------

def _aiohttp_kind(exc: BaseException, proxy: str | None) -> str:
    """Map exception aiohttp -> kind.

    Thứ tự subclass trước (ClientProxy* > *SSL* > ClientConnectorError >
    ClientOSError/... > OSError) + getattr guard cho class thiếu ở bản cũ.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    for name in ("ServerTimeoutError", "ConnectionTimeoutError"):
        cls = getattr(aiohttp, name, None)
        if cls is not None and isinstance(exc, cls):
            return "timeout"
    for name in ("ClientProxyConnectionError", "ClientProxyError"):
        cls = getattr(aiohttp, name, None)
        if cls is not None and isinstance(exc, cls):
            return "proxy_dead"
    for name in (
        "ClientConnectorCertificateError",
        "ClientConnectorSSLError",
        "ClientSSLError",
    ):
        cls = getattr(aiohttp, name, None)
        if cls is not None and isinstance(exc, cls):
            return "tls"
    if isinstance(exc, ssl.SSLError):
        return "tls"
    cls = getattr(aiohttp, "ClientConnectorError", None)
    if cls is not None and isinstance(exc, cls):
        return "proxy_dead" if proxy else "disconnect"
    for name in ("ClientOSError", "ServerDisconnectedError", "ClientPayloadError"):
        cls = getattr(aiohttp, name, None)
        if cls is not None and isinstance(exc, cls):
            return "disconnect"
    if isinstance(exc, ConnectionResetError):
        return "disconnect"
    cls = getattr(aiohttp, "ClientConnectionError", None)
    if cls is not None and isinstance(exc, cls):
        return "disconnect"
    if isinstance(exc, OSError):
        return "disconnect"
    return "unknown"


def _curl_kind(exc: BaseException) -> str:
    """Map exception curl_cffi -> kind theo tên class/nội dung (không phụ thuộc version).

    curl_cffi đổi tên class giữa các bản (RequestsError, CurlError, Timeout,
    ProxyError, ...) nên chỉ soi tên class + message cho ổn định.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timeout" in text or "timed out" in text:
        return "timeout"
    if "proxy" in name or "proxy" in text:
        return "proxy_dead"
    if any(tok in name or tok in text for tok in ("ssl", "tls", "certificate")):
        return "tls"
    # ConnectionError, CurlError, ... -> coi như đứt kết nối.
    return "disconnect"


# ---------------------------------------------------------------------------
# X-Fingerprint – bắt tay client Discord (GET /experiments theo proxy)
# ---------------------------------------------------------------------------

EXPERIMENTS_URL = "https://discord.com/api/v9/experiments"


class _FingerprintMixin:
    """Harvester X-Fingerprint dùng chung cho 2 backend (best-effort, không raise).

    Trước POST đầu tiên của mỗi proxy, GET EXPERIMENTS_URL qua đúng proxy đó
    để lấy field `fingerprint` (mô phỏng bắt tay client web thật). Cache key
    là `proxy or ""` (proxyless = key rỗng); negative cache bảo đảm mỗi proxy
    chỉ thử MỘT lần kể cả thất bại. Mọi lỗi bị nuốt âm thầm – POST chạy như
    code cũ, không bao giờ phụ thuộc kết quả bootstrap.
    """

    def _fingerprint_setup(self, use_fingerprint: bool) -> None:
        """Khởi tạo state; gọi từ __init__ của từng backend."""
        self._fp_enabled = bool(use_fingerprint)
        # key -> fingerprint | None (None = đã thử, thất bại – negative cache).
        self._fp_cache: dict[str, str | None] = {}
        # key đang bootstrap – dedupe nhẹ cho các POST đồng thời cùng proxy.
        self._fp_inflight: set[str] = set()

    async def _fingerprint_for(self, proxy: str | None) -> str | None:
        """Trả fingerprint đã cache cho proxy; lần đầu thì bootstrap (tối đa 1 lần)."""
        if not self._fp_enabled:
            return None
        key = proxy or ""
        if key in self._fp_cache:
            return self._fp_cache[key]
        if key in self._fp_inflight:
            # Task khác đang bootstrap cùng proxy -> lần này chạy trần, không chờ.
            return None
        self._fp_inflight.add(key)
        value: str | None = None
        try:
            value = await self._fetch_fingerprint(proxy)
        except asyncio.CancelledError:
            raise
        except Exception:
            value = None
        finally:
            self._fp_inflight.discard(key)
            # Negative cache ghi trong finally: kể cả bị cancel cũng không thử lại.
            self._fp_cache[key] = value
        return value

    async def _fetch_fingerprint(self, proxy: str | None) -> str | None:
        """GET experiments -> field fingerprint; None nếu bất kỳ bước nào thất bại."""
        spec = self.timeout_spec
        # Bootstrap là phụ trợ -> chặn total ≤ 6s để không phí thời gian của check.
        cheap = TimeoutSpec(spec.connect, spec.read, min(spec.total, 6.0))
        try:
            resp = await self._get(
                EXPERIMENTS_URL,
                proxy=proxy,
                headers=dict(DEFAULT_HEADERS),
                timeout=cheap,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        if resp.status != 200 or not isinstance(resp.data, dict):
            return None
        value = resp.data.get("fingerprint")
        if isinstance(value, str) and value:
            return value
        return None

    async def _headers_with_fingerprint(
        self, proxy: str | None, headers: dict | None
    ) -> dict | None:
        """Headers đã gắn X-Fingerprint nếu lấy được; không mutate dict caller."""
        if not self._fp_enabled:
            return headers
        fp = await self._fingerprint_for(proxy)
        if not fp:
            return headers
        # Caller đã truyền X-Fingerprint -> tôn trọng, tuyệt đối không override.
        if headers is not None and any(
            str(k).lower() == "x-fingerprint" for k in headers
        ):
            return headers
        merged = dict(headers or {})
        merged["X-Fingerprint"] = fp
        return merged


# ---------------------------------------------------------------------------
# AiohttpBackend – mặc định, connector tinh chỉnh cho pool proxy lớn
# ---------------------------------------------------------------------------

class AiohttpBackend(_FingerprintMixin):
    """Backend aiohttp: MỘT ClientSession dùng chung cho mọi request.

    - limit = concurrency*2 (chặn trên 4000), limit_per_host=0 (proxy pool
      coi như vô số host), keepalive 30s, DNS cache 600s, SSL giả Chrome.
    - Lưu ý aiohttp >= 3.12 đòi running loop ngay khi tạo Connector/Session;
      nếu tạo ngoài loop (ví dụ make_backend gọi đồng bộ), việc tạo được
      hoãn sang lần post_json đầu tiên thay vì nổ RuntimeError.
    - use_fingerprint=True: bootstrap X-Fingerprint theo proxy (best-effort).
    """

    name = "aiohttp"

    def __init__(
        self,
        user_timeout: float = 15.0,
        concurrency: int = 50,
        mode: str = "default",
        *,
        use_fingerprint: bool = True,
    ) -> None:
        self.timeout_spec = mode_timeout(mode, user_timeout)
        try:
            self._concurrency = max(int(concurrency), 1)
        except (TypeError, ValueError):
            self._concurrency = 50
        self._connector: aiohttp.TCPConnector | None = None
        self._session: aiohttp.ClientSession | None = None
        self._fingerprint_setup(use_fingerprint)
        try:
            self._ensure_session()
        except RuntimeError:
            # Chưa có event loop – tạo lazy ở lần dùng đầu tiên.
            pass

    def _build_connector(self) -> aiohttp.TCPConnector:
        # SSL giả Chrome (cipher order); lỗi thì trả None để aiohttp tự tạo.
        try:
            ssl_ctx = create_browser_ssl_context()
        except Exception:
            ssl_ctx = None
        return aiohttp.TCPConnector(
            limit=min(self._concurrency * 2, 4000),
            limit_per_host=0,
            force_close=False,
            enable_cleanup_closed=True,
            ttl_dns_cache=600,
            keepalive_timeout=30,
            ssl=ssl_ctx,
        )

    def _ensure_session(self) -> aiohttp.ClientSession:
        """Trả session dùng chung; tạo mới nếu chưa có/đã đóng."""
        sess = self._session
        if sess is not None and not sess.closed:
            return sess
        if self._connector is None or self._connector.closed:
            self._connector = self._build_connector()
        self._session = aiohttp.ClientSession(
            connector=self._connector, trust_env=False
        )
        return self._session

    async def _get(
        self,
        url: str,
        *,
        proxy: str | None = None,
        headers: dict | None = None,
        timeout: TimeoutSpec | None = None,
    ) -> Response:
        """GET -> Response (nội bộ, phục vụ bootstrap X-Fingerprint).

        Dùng cùng session/connector với post_json; mọi lỗi mạng cũng thành
        BackendError đã phân loại để tầng gọi tự quyết định nuốt hay không.
        """
        spec = timeout or self.timeout_spec
        session = self._ensure_session()
        try:
            async with session.get(
                url,
                proxy=proxy or None,
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=spec.total,
                    sock_connect=spec.connect,
                    sock_read=spec.read,
                ),
            ) as resp:
                status = resp.status
                headers_lower = {
                    str(k).lower(): str(v) for k, v in resp.headers.items()
                }
                try:
                    # Body HTML/rỗng KHÔNG được raise – data=None là hợp lệ.
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                return Response(status, data, headers_lower)
        except asyncio.CancelledError:
            # Không bao giờ nuốt cancellation.
            raise
        except Exception as exc:
            kind = _aiohttp_kind(exc, proxy)
            raise BackendError(
                kind, f"{type(exc).__name__}: {exc}", proxy=proxy
            ) from exc

    async def post_json(
        self,
        url: str,
        payload: dict,
        *,
        proxy: str | None = None,
        headers: dict | None = None,
        timeout: TimeoutSpec | None = None,
    ) -> Response:
        """POST JSON -> Response. Mọi lỗi mạng thành BackendError đã phân loại."""
        spec = timeout or self.timeout_spec
        headers = await self._headers_with_fingerprint(proxy, headers)
        session = self._ensure_session()
        try:
            async with session.post(
                url,
                json=payload,
                proxy=proxy or None,
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=spec.total,
                    sock_connect=spec.connect,
                    sock_read=spec.read,
                ),
            ) as resp:
                status = resp.status
                headers_lower = {
                    str(k).lower(): str(v) for k, v in resp.headers.items()
                }
                try:
                    # Body HTML/rỗng KHÔNG được raise – data=None là hợp lệ.
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                return Response(status, data, headers_lower)
        except asyncio.CancelledError:
            # Không bao giờ nuốt cancellation.
            raise
        except Exception as exc:
            kind = _aiohttp_kind(exc, proxy)
            raise BackendError(
                kind, f"{type(exc).__name__}: {exc}", proxy=proxy
            ) from exc

    async def close(self) -> None:
        """Đóng session + connector; gọi nhiều lần vô hại, không bao giờ raise."""
        sess, self._session = self._session, None
        conn, self._connector = self._connector, None
        try:
            if sess is not None and not sess.closed:
                await sess.close()
        except Exception:
            pass
        try:
            if conn is not None and not conn.closed:
                await conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CurlCffiBackend – TLS/JA3 giả Chrome, qua WAF tốt hơn
# ---------------------------------------------------------------------------

class CurlCffiBackend(_FingerprintMixin):
    """Backend curl_cffi (optional): impersonate Chrome 124.

    __init__ raise RuntimeError khi curl_cffi thiếu/lỗi để make_backend
    fallback êm xuống aiohttp. Timeout ưu tiên tuple (connect, read);
    bản cũ không hỗ trợ thì hạ cấp xuống total (float).

    Session riêng theo proxy tĩnh: cookie __cf_bm do Cloudflare gắn với IP
    client, nên mỗi proxy phải có phiên riêng để cookie không bị dùng sai
    IP (vừa sai vừa là dấu hiệu bot). Proxyless dùng session chung;
    số session proxy tối đa MAX_PROXY_SESSIONS.

    use_fingerprint=True: bootstrap X-Fingerprint theo proxy (best-effort,
    dùng đúng session theo proxy qua _session_for).
    """

    name = "curl_cffi"

    MAX_PROXY_SESSIONS = 64

    def __init__(
        self,
        user_timeout: float = 15.0,
        concurrency: int = 50,
        mode: str = "default",
        *,
        use_fingerprint: bool = True,
    ) -> None:
        self.timeout_spec = mode_timeout(mode, user_timeout)
        self._fingerprint_setup(use_fingerprint)
        try:
            from curl_cffi.requests import AsyncSession

            self._cls = AsyncSession
            self._session = AsyncSession(impersonate="chrome124")
        except Exception as exc:  # ImportError / lỗi cấu hình impersonate
            raise RuntimeError(f"curl_cffi unavailable: {exc}") from exc
        self._sessions: dict[str, Any] = {}
        self._tuple_timeout = self._detect_tuple_timeout()

    @staticmethod
    def _detect_tuple_timeout() -> bool:
        """Dò MỘT lần: bản curl_cffi cài đặt có nhận timeout tuple không."""
        try:
            from curl_cffi.requests import AsyncSession

            param = inspect.signature(AsyncSession.request).parameters.get("timeout")
            if param is None:
                return False
            return "tuple" in str(param.annotation)
        except Exception:
            # Không dò được -> cứ thử tuple, TypeError sẽ tự hạ cấp.
            return True

    def _ensure_session(self):
        """Trả session dùng chung; tạo lại nếu đã bị close()."""
        sess = self._session
        if sess is None:
            sess = self._cls(impersonate="chrome124")
            self._session = sess
        return sess

    def _session_for(self, proxy: str | None):
        """Chọn session theo proxy: static -> session riêng, còn lại -> chung.

        - proxy None (proxyless): session chung.
        - proxy tĩnh: cache session riêng để cookie __cf_bm gắn đúng IP;
          chạm cap MAX_PROXY_SESSIONS thì rơi về session chung (không phình).
        """
        if proxy is None:
            return self._ensure_session()
        sess = self._sessions.get(proxy)
        if sess is not None:
            return sess
        if len(self._sessions) >= self.MAX_PROXY_SESSIONS:
            return self._ensure_session()
        sess = self._cls(impersonate="chrome124")
        self._sessions[proxy] = sess
        return sess

    async def _post(self, session, url, payload, proxy, headers, spec: TimeoutSpec):
        """Gọi post với timeout tuple, tự hạ cấp total-float nếu bản cũ chặn."""
        if self._tuple_timeout:
            tmo: float | tuple[float, float] = (spec.connect, spec.read)
        else:
            tmo = float(spec.total)
        try:
            return await session.post(
                url,
                json=payload,
                proxy=proxy or None,
                headers=headers,
                timeout=tmo,
            )
        except TypeError:
            if not self._tuple_timeout:
                raise
            # Bản cũ chỉ nhận float -> tắt tuple vĩnh viễn rồi thử lại 1 lần.
            self._tuple_timeout = False
            return await session.post(
                url,
                json=payload,
                proxy=proxy or None,
                headers=headers,
                timeout=float(spec.total),
            )

    async def _get_raw(self, session, url, proxy, headers, spec: TimeoutSpec):
        """Gọi get với timeout tuple, tự hạ cấp total-float nếu bản cũ chặn."""
        if self._tuple_timeout:
            tmo: float | tuple[float, float] = (spec.connect, spec.read)
        else:
            tmo = float(spec.total)
        try:
            return await session.get(
                url,
                proxy=proxy or None,
                headers=headers,
                timeout=tmo,
            )
        except TypeError:
            if not self._tuple_timeout:
                raise
            # Bản cũ chỉ nhận float -> tắt tuple vĩnh viễn rồi thử lại 1 lần.
            self._tuple_timeout = False
            return await session.get(
                url,
                proxy=proxy or None,
                headers=headers,
                timeout=float(spec.total),
            )

    async def _get(
        self,
        url: str,
        *,
        proxy: str | None = None,
        headers: dict | None = None,
        timeout: TimeoutSpec | None = None,
    ) -> Response:
        """GET -> Response (nội bộ, phục vụ bootstrap X-Fingerprint).

        Dùng _session_for(proxy) như post_json để giữ cookie __cf_bm gắn
        đúng IP; lỗi mạng phân loại BackendError giống post_json.
        """
        spec = timeout or self.timeout_spec
        session = self._session_for(proxy)
        try:
            resp = await self._get_raw(session, url, proxy, headers, spec)
            try:
                data = json.loads(resp.content)
            except Exception:
                # Body HTML/rỗng KHÔNG được raise – data=None là hợp lệ.
                data = None
            try:
                raw = resp.headers or {}
                headers_lower = {str(k).lower(): str(v) for k, v in raw.items()}
            except Exception:
                headers_lower = {}
            return Response(resp.status_code, data, headers_lower)
        except asyncio.CancelledError:
            # Không bao giờ nuốt cancellation.
            raise
        except Exception as exc:
            kind = _curl_kind(exc)
            raise BackendError(
                kind, f"{type(exc).__name__}: {exc}", proxy=proxy
            ) from exc

    async def post_json(
        self,
        url: str,
        payload: dict,
        *,
        proxy: str | None = None,
        headers: dict | None = None,
        timeout: TimeoutSpec | None = None,
    ) -> Response:
        """POST JSON -> Response. Mọi lỗi mạng thành BackendError đã phân loại."""
        spec = timeout or self.timeout_spec
        headers = await self._headers_with_fingerprint(proxy, headers)
        session = self._session_for(proxy)
        try:
            resp = await self._post(session, url, payload, proxy, headers, spec)
            try:
                data = json.loads(resp.content)
            except Exception:
                # Body HTML/rỗng KHÔNG được raise – data=None là hợp lệ.
                data = None
            try:
                raw = resp.headers or {}
                headers_lower = {str(k).lower(): str(v) for k, v in raw.items()}
            except Exception:
                headers_lower = {}
            return Response(resp.status_code, data, headers_lower)
        except asyncio.CancelledError:
            # Không bao giờ nuốt cancellation.
            raise
        except Exception as exc:
            kind = _curl_kind(exc)
            raise BackendError(
                kind, f"{type(exc).__name__}: {exc}", proxy=proxy
            ) from exc

    async def close(self) -> None:
        """Đóng toàn bộ session (chung + riêng theo proxy); gọi nhiều lần vô hại."""
        sessions, self._sessions = dict(self._sessions), {}
        for sess in sessions.values():
            try:
                await sess.close()
            except Exception:
                pass
        sess, self._session = self._session, None
        if sess is None:
            return
        try:
            await sess.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Factory – không bao giờ raise, luôn trả về backend dùng được
# ---------------------------------------------------------------------------

def make_backend(
    user_timeout: float = 15.0,
    concurrency: int = 50,
    mode: str = "default",
    prefer_curl: bool = True,
    use_fingerprint: bool = True,
) -> AiohttpBackend | CurlCffiBackend:
    """Chọn backend: curl_cffi nếu khả dụng, fallback êm xuống aiohttp.

    Env DISCORDFINDER_BACKEND (fallback tên cũ CLOUDCHECKER_BACKEND):
    "aiohttp" ép aiohttp; "curl" ép thử curl; mặc định "auto" theo
    *prefer_curl*. *use_fingerprint* bật/tắt bootstrap X-Fingerprint
    (False -> zero request thêm, hành vi y hệt bản cũ).
    """
    try:
        ut = max(1.0, float(user_timeout))
    except (TypeError, ValueError):
        ut = 15.0
    env = os.environ.get(
        "DISCORDFINDER_BACKEND",
        os.environ.get("CLOUDCHECKER_BACKEND", "auto"),
    ).strip().lower()
    if env == "aiohttp":
        return AiohttpBackend(
            user_timeout=ut,
            concurrency=concurrency,
            mode=mode,
            use_fingerprint=use_fingerprint,
        )
    try_curl = env == "curl" or prefer_curl
    if try_curl:
        try:
            return CurlCffiBackend(
                user_timeout=ut,
                concurrency=concurrency,
                mode=mode,
                use_fingerprint=use_fingerprint,
            )
        except Exception:
            # curl_cffi thiếu/lỗi -> log 1 dòng rồi rơi xuống aiohttp.
            try:
                from src.ui import info

                info("curl_cffi không khả dụng – dùng aiohttp")
            except Exception:
                pass
    return AiohttpBackend(
        user_timeout=ut,
        concurrency=concurrency,
        mode=mode,
        use_fingerprint=use_fingerprint,
    )
