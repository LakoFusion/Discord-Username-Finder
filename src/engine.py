#!/usr/bin/env python3
"""Discord Username Finder v3.1 – Checker engine, circuit breaker, error aggregation, webhook."""

from __future__ import annotations

import asyncio
import json
import math
import random
import sys
import time
from enum import Enum
from urllib.parse import quote

import aiohttp

from src.config import C, DEFAULT_HEADERS, ENDPOINT, OUTPUT_DIR
from src.http_backend import BackendError, Response
from src.proxy import ProxyManager, _clamp_cooldown
from src.token_pool import is_dead_token_response
from src.ui import console

# Endpoint có auth (token): pomelo-attempt trả cùng schema {"taken": bool}.
ENDPOINT_TOKEN = "https://discord.com/api/v9/users/@me/pomelo-attempt"

# ---------------------------------------------------------------------------
# Debug control
# ---------------------------------------------------------------------------

_debug: bool = False


def set_debug(enabled: bool) -> None:
    global _debug
    _debug = enabled


def dbg(*args, **kwargs) -> None:
    """Debug print – only when --debug is active."""
    if _debug:
        print(*args, file=sys.stderr, flush=True, **kwargs)


# ---------------------------------------------------------------------------
# Response classification – thuần, không I/O
# ---------------------------------------------------------------------------

class Verdict(Enum):
    """Kết quả phân loại 1 response từ Discord (hoặc proxy rác)."""
    AVAILABLE = "available"
    TAKEN = "taken"
    INVALID = "invalid"
    RETRY = "retry"
    RATE_LIMITED = "rate_limited"


def classify_response(
    status: int, data, headers: dict[str, str]
) -> tuple[Verdict, str]:
    """Phân loại response -> (Verdict, reason).

    Guard: 200/201/204 chỉ tin body JSON {"taken": bool} – HTML/captive
    portal HTTP 200 từ proxy rác KHÔNG được báo Taken/AVAILABLE oan.
    400 chỉ tính INVALID khi body là JSON dict có code/message/errors –
    400 HTML từ proxy độc không được tính là Taken/Invalid; body JSON
    Cloudflare (code == 0) LUÔN là RETRY "cf_block" để không xoá oan tên.
    429 phân 3 loại qua reason: "cf_block" (body code 0), "shared"
    (header x-ratelimit-scope: shared – rate limit toàn cụm), còn lại
    "rate_limited"/"cf_429" (body không phải JSON).
    """
    if status in (200, 201, 204):
        if (
            isinstance(data, dict)
            and "taken" in data
            and isinstance(data["taken"], bool)
        ):
            if data["taken"] is False:
                return (Verdict.AVAILABLE, "ok")
            return (Verdict.TAKEN, "ok")
        return (Verdict.RETRY, "bad_schema")

    if status == 400:
        if isinstance(data, dict) and data.get("code") == 0:
            # Body JSON Cloudflare (block page) – KHÔNG phải Discord nói
            # username sai; nếu coi là INVALID sẽ xoá oan tên khỏi queue.
            return (Verdict.RETRY, "cf_block")
        if (
            isinstance(data, dict)
            and data
            and any(k in data for k in ("code", "message", "errors"))
        ):
            return (Verdict.INVALID, "invalid_username")
        return (Verdict.RETRY, "bad_400")

    if status == 429:
        if isinstance(data, dict):
            if data.get("code") == 0:
                # Body JSON Cloudflare (block page) – không phải rate limit
                # Discord thường -> checker park proxy dài hạn.
                return (Verdict.RATE_LIMITED, "cf_block")
            scope = str(headers.get("x-ratelimit-scope", "") or "").strip().lower()
            return (
                Verdict.RATE_LIMITED,
                "shared" if scope == "shared" else "rate_limited",
            )
        return (Verdict.RATE_LIMITED, "cf_429")
    if status in (401, 403):
        return (Verdict.RETRY, "waf_block")
    if 500 <= status <= 599:
        return (Verdict.RETRY, "http_5xx")
    if status == 404:
        return (Verdict.RETRY, "http_404")
    return (Verdict.RETRY, f"http_{status}")


def _sanitize_retry(value) -> float | None:
    """Chuẩn hoá 1 giá trị retry_after -> float dương, None nếu rác.

    None / không parse được / NaN / <= 0 -> None (bỏ qua nguồn đó);
    +inf giữ nguyên để bước clamp cuối cap về 1800s.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or value <= 0:
        return None
    return value


def _retry_after(resp: Response) -> float:
    """Lấy thời gian chờ 429: max(body.retry_after, header Retry-After) + 0.25s.

    - Body JSON (giây) và header Retry-After đều được xét; body dạng str
      thô được cố parse JSON an toàn; giá trị <= 0 / NaN / không parse
      được bị bỏ qua.
    - Heuristic mili-giây: header > body*100 -> header đang tính bằng ms,
      quy về giây nguyên (bỏ phần lẻ ms) cho đồng bộ đơn vị.
    - Thiếu cả hai nguồn -> fallback 5s; cộng epsilon 0.25s (tránh đua
      đúng mốc hết hạn) rồi clamp [0.5, 1800.0] – đủ dài cho ban
      Cloudflare, chặn NaN/±inf/rác.
    """
    body_val = None
    data = getattr(resp, "data", None)
    if isinstance(data, str):
        # Data thô chưa parse JSON (hiếm) – cố parse, lỗi thì bỏ qua nguồn.
        try:
            data = json.loads(data)
        except Exception:
            data = None
    if isinstance(data, dict):
        body_val = _sanitize_retry(data.get("retry_after"))
    header_val = None
    try:
        headers = getattr(resp, "headers", None) or {}
        header_val = _sanitize_retry(headers.get("retry-after"))
    except Exception:
        header_val = None

    if body_val is None and header_val is None:
        return 5.25
    if body_val is None:
        value = header_val
    elif header_val is None:
        value = body_val
    else:
        if header_val > body_val * 100:
            # Header lớn hơn body >100 lần -> gần như chắc chắn đang là ms.
            if math.isfinite(header_val):
                header_val = float(int(header_val / 1000.0))
        value = max(body_val, header_val)
    return min(max(value + 0.25, 0.5), 1800.0)


def _jittered_cooldown(wait: float) -> float:
    """Cộng đệm ngẫu nhiên 0.5-1.5s rồi clamp [0.5, 60].

    Áp dụng khi request kế tiếp phải đi qua CÙNG proxy/IP sau 429
    (scraped/static/proxyless) — tránh nhiều worker hết cooldown cùng
    lúc rồi bắn dồn trở lại (đồng pha) và tôn trọng thời gian chờ của
    máy chủ đích. Rotating đổi IP mới nên không cần jitter.
    """
    return _clamp_cooldown(wait + random.uniform(0.5, 1.5))


def _mask_proxy(proxy: str) -> str:
    """Che credential trước khi in proxy ra console/debug.

    Dạng login:pass@host:port giữ lại host:port (tiền tố ***@);
    dạng host:port:login:pass giữ host:port (hậu tố :****);
    proxy không chứa credential giữ nguyên.
    """
    if "@" in proxy:
        return "***@" + proxy.rsplit("@", 1)[1]
    if proxy.count(":") >= 3:
        parts = proxy.split(":")
        return ":".join(parts[:2]) + ":****"
    return proxy


# ---------------------------------------------------------------------------
# Circuit breaker – prevents thundering herd on rotating proxy
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Shared across all workers using a single rotating proxy.

    When too many connection failures happen in a short window,
    the circuit opens and all workers pause briefly before retrying.
    This prevents hammering a struggling gateway while still ensuring
    every username eventually gets checked.
    """

    def __init__(
        self,
        threshold: int = 8,
        window: float = 2.0,
        cooldown: float = 2.0,
        on_open = None,
    ) -> None:
        self.threshold = threshold
        self.window = window
        self.cooldown = cooldown
        self._failures: list[float] = []
        self._lock = asyncio.Lock()
        self._open_until: float = 0.0
        self._on_open = on_open  # async callback when circuit opens

    async def record_failure(self) -> None:
        """Record a failure and open the circuit if threshold crossed."""
        opened = False
        async with self._lock:
            now = time.monotonic()
            self._failures.append(now)
            # Purge old failures
            self._failures = [t for t in self._failures if now - t < self.window]
            if len(self._failures) >= self.threshold:
                self._open_until = now + self.cooldown
                # Clear list khi mở mạch: tránh mở lại liên tục chỉ vì list
                # cũ vẫn còn đủ threshold phần tử (audit A14).
                self._failures.clear()
                opened = True
        if opened and self._on_open:
            # Fire-and-forget NGOÀI lock, không bao giờ await callback
            # trong lúc giữ lock.
            try:
                asyncio.get_running_loop().create_task(self._on_open())
            except Exception:
                pass

    async def record_success(self) -> None:
        """Discord trả lời dứt khoát -> reset lỗi, đóng mạch sớm."""
        async with self._lock:
            self._failures.clear()
            if self._open_until:
                self._open_until = 0.0

    async def wait_if_open(self) -> None:
        """Block until the circuit closes."""
        now = time.monotonic()
        if now < self._open_until:
            wait = self._open_until - now
            dbg(f"[cb] circuit open, waiting {wait:.1f}s")
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# ErrorAggregator – batch error counting
# ---------------------------------------------------------------------------

class ErrorAggregator:
    """Aggregates error counts instead of spamming logs."""

    def __init__(
        self,
        flush_interval: float = 30.0,
        flush_count: int = 500,
    ) -> None:
        self.flush_interval = flush_interval
        self.flush_count = flush_count
        self._lock = asyncio.Lock()
        self._counters: dict[str, int] = {}
        self._last_flush = time.time()

    async def inc(self, category: str) -> None:
        async with self._lock:
            self._counters[category] = self._counters.get(category, 0) + 1
            total = sum(self._counters.values())
            if total >= self.flush_count or (time.time() - self._last_flush) >= self.flush_interval:
                await self._flush_locked()

    async def _flush_locked(self) -> None:
        self._counters.clear()
        self._last_flush = time.time()

    def get_counts_sync(self) -> dict[str, int]:
        # Sync copy for UI (không lock chặt, chỉ đọc)
        return dict(self._counters)


# ---------------------------------------------------------------------------
# Checker – the core async checking loop
# ---------------------------------------------------------------------------

class Checker:
    """Async username checker with keep-alive sessions and retry loop.

    Key behaviour:
    - Rotating proxy: retries until Discord gives a definitive answer.
      Connection errors get progressive backoff + circuit breaker.
      The only hard stop is Discord returning 200/201/204/400.
    - Static proxy list: cooldowns, 20 retries, 120 s total timeout.
      429 phân 3 loại (rate_limited / shared / cf HTML-JSON): lần đầu
      jitter cooldown ngắn; tái phạm -> park leo thang 60-1800s; AIMD tự
      giãn interval theo lỗi; release() giãn cách đo từ lúc nhận response,
      200 dứt khoát thì record_ok + unpark để phục hồi dần.
    - Scraped free proxies: one shot per proxy (dead proxy removed
      instantly), up to 25 different proxies per username.
    - Double-check: hit AVAILABLE được xác nhận bằng proxy thứ hai, xác
      thực chéo bằng API ngược lại (token ↔ unauthed) khi có thể – chống
      proxy rác báo "available"; không thể chéo thì giữ hành vi cũ.
    - Token mode (tùy chọn qua token_pool): gửi pomelo-attempt kèm token
      dính theo proxy; chỉ rate limit THẬT của Discord mới cooldown token
      (cf_block/cf_429 = lỗi proxy -> park proxy, token giữ nguyên);
      401/403 chỉ mark_dead khi body JSON lỗi auth Discord (message str +
      code int — is_dead_token_response; HTML/Cloudflare = lỗi proxy); hết token sống -> tự chạy unauthed (hybrid).
    """

    MAX_RETRIES = 20                # hard cap for static list
    MAX_RETRIES_ROTATING = 100      # generous cap for rotating (should never hit)
    MAX_RETRIES_SCRAPED = 25        # free proxies: one-shot PER PROXY, not per username
    STATIC_TOTAL_TIMEOUT = 120.0    # total timeout for static list

    def __init__(
        self,
        proxy_manager: ProxyManager,
        backend,  # AiohttpBackend | CurlCffiBackend (src.http_backend)
        timeout: int = 10,
        scraped: bool = False,
        circuit_breaker: CircuitBreaker | None = None,
        stats = None,
        *,
        token_pool = None,
    ) -> None:
        self.pm = proxy_manager
        self.backend = backend
        self.timeout = max(1, int(timeout or 10))
        self._err = ErrorAggregator()
        self._rotating = proxy_manager.is_single
        self._scraped = scraped
        self._cb = circuit_breaker
        self._stats = stats  # Stats object for live counters
        self._token_pool = token_pool
        # Double-check hit: chỉ khi có nhiều proxy thay thế để xác nhận
        # (proxyless / rotating-gateway không double-check).
        self._double_check = scraped or (
            not proxy_manager.is_single and not proxy_manager.is_proxyless
        )

        if scraped:
            self._max_retries = self.MAX_RETRIES_SCRAPED
        elif self._rotating:
            self._max_retries = self.MAX_RETRIES_ROTATING
        else:
            self._max_retries = self.MAX_RETRIES

    async def _apply_proactive_cooldown(
        self, resp: Response, proxy: str | None
    ) -> None:
        """Đọc X-RateLimit headers ở response 200/400 để nghỉ TRƯỚC khi 429.

        Guard: thiếu headers / proxyless -> return. Chỉ set cooldown cho
        riêng proxy đó, không pause toàn cục. Response.headers keys lowercase.
        Ưu tiên x-ratelimit-reset-after; fallback x-ratelimit-reset (epoch giây).
        """
        try:
            headers = resp.headers or {}
            remaining = headers.get("x-ratelimit-remaining")
            if remaining is None or str(remaining).strip() != "0":
                return
            reset_after = headers.get("x-ratelimit-reset-after")
            if reset_after is not None:
                wait_s = float(str(reset_after).strip()) + 0.15
            else:
                # Fallback: X-RateLimit-Reset (epoch giây) khi thiếu -After
                reset_epoch = headers.get("x-ratelimit-reset")
                if reset_epoch is None:
                    return
                wait_s = float(str(reset_epoch).strip()) - time.time() + 0.15
            if wait_s <= 0 or wait_s > 60:
                return
        except Exception:
            return
        if proxy is None:
            return
        try:
            if self._scraped:
                self.pm.set_rate_limit(proxy, wait_s)
            else:
                await self.pm.set_cooldown(proxy, wait_s)
            await self._err.inc("proactive_cooldown")
        except Exception:
            pass

    async def _confirm_available(
        self,
        username: str,
        proxy: str | None,
        tried: set[str],
        *,
        primary_used_token: bool = False,
    ) -> bool:
        """Xác nhận 1 hit AVAILABLE bằng proxy THỨ HAI (double-check).

        Quy tắc chéo API: primary_used_token=True (hit tìm bằng Token API)
        -> confirm chuyển sang API unauthed (ENDPOINT, không Authorization);
        primary unauthed + pool còn token -> confirm dùng Token API (chéo
        ngược); không thể chéo (pool None/rỗng) -> dùng unauthed. Best-effort:
        thiếu proxy thứ hai / lỗi transport -> thà giữ hit còn hơn mất tên.

        True  – giữ hit gốc (không đủ proxy để xác nhận / lỗi transport /
                lỗi phía proxy xác nhận – thà giữ hit còn hơn mất tên).
        False – conflict (proxy thứ hai báo TAKEN/INVALID) -> vòng ngoài
                thử lại từ đầu, KHÔNG trả kết quả sai.
        """
        try:
            confirm_proxy = await self.pm.next(exclude=set(tried))
        except Exception:
            return True
        if confirm_proxy is None or confirm_proxy == proxy:
            # Không có proxy khác -> chấp nhận hit ngay, không tốn request
            return True

        token = None
        if self._token_pool is not None and not primary_used_token:
            # Hit tìm bằng Token API -> confirm cố tình chuyển sang API
            # unauthed (xác thực chéo), không acquire token nữa.
            try:
                token = self._token_pool.acquire()
            except Exception:
                token = None
        if token:
            url = ENDPOINT_TOKEN
            headers = {**DEFAULT_HEADERS, "Authorization": token}
        else:
            url = ENDPOINT
            headers = DEFAULT_HEADERS
        dbg(
            f"  [confirm] {username} dùng API "
            f"{'token' if token else 'unauthed'} qua proxy {_mask_proxy(confirm_proxy)}"
        )

        try:
            # Count every HTTP attempt (including double-check) for RPS
            if self._stats:
                await self._stats.inc_requests()
            tried.add(confirm_proxy)

            try:
                resp = await self.backend.post_json(
                    url,
                    {"username": username},
                    proxy=confirm_proxy,
                    headers=headers,
                )
            except Exception:
                await self.pm.release(confirm_proxy)
                raise
            # Nhịp release tính từ lúc response về (no-op với bể khác static).
            await self.pm.release(confirm_proxy)
            # Nghỉ trước 429 nếu headers cảnh báo, rồi mới phân loại
            await self._apply_proactive_cooldown(resp, confirm_proxy)
            verdict, _reason = classify_response(
                resp.status, resp.data, resp.headers
            )

            if token:
                if verdict is Verdict.RATE_LIMITED and _reason not in ("cf_block", "cf_429"):
                    # Chỉ cooldown token với rate limit thật của Discord;
                    # cf_block/cf_429 là lỗi proxy, token còn nguyên giá trị.
                    self._token_pool.mark_rate_limited(token, _retry_after(resp))
                elif is_dead_token_response(resp.status, resp.data):
                    # 401/403 kèm body lỗi auth Discord (message + code)
                    # mới là token chết thật
                    self._token_pool.mark_dead(token)

            if verdict is Verdict.AVAILABLE:
                if self._scraped and confirm_proxy:
                    self.pm.score_hit(confirm_proxy)
                elif not self._rotating:
                    self.pm.record_success(confirm_proxy)
                    self.pm.record_ok(confirm_proxy)
                    await self.pm.unpark(confirm_proxy)
                if self._cb:
                    await self._cb.record_success()
                return True

            if verdict in (Verdict.TAKEN, Verdict.INVALID):
                return False

            # RETRY / RATE_LIMITED -> lỗi phía proxy xác nhận, giữ hit gốc
            return True
        except BackendError:
            # Lỗi transport khi xác nhận – đừng vì thế mà mất hit
            return True
        except Exception:
            return True

    async def check(
        self,
        username: str,
    ) -> tuple[bool | str, dict | None, int | None]:
        """Check a single username.

        Returns:
            (True, data, code)  – available
            (False, data, code) – taken or invalid
            ("EXHAUSTED", None, None) – all proxies dead
            ("ERROR", None, None) – max retries exceeded
        """
        attempt = 0
        started = time.time()
        tried: set[str] = set()
        # Sticky token theo proxy: (proxy, token) đang giữ qua các lần thử.
        held: tuple[str | None, str | None] | None = None

        while True:
            # Hết 1 vòng xoay qua mọi proxy sống -> làm mới tried để xoay tiếp
            alive = self.pm.alive_count
            if alive > 0 and len(tried) >= alive:
                tried.clear()
            proxy = await self.pm.next(exclude=tried or None)
            if proxy is None and not self.pm.is_proxyless:
                return ("EXHAUSTED", None, None)
            if proxy is not None:
                tried.add(proxy)

            attempt += 1
            if attempt > self._max_retries:
                return ("ERROR", None, None)

            # Total timeout — chỉ áp dụng cho static list, không áp dụng rotating
            if not self._rotating:
                if time.time() - started > self.STATIC_TOTAL_TIMEOUT:
                    await self._err.inc("total_timeout")
                    return ("ERROR", None, None)

            # Count every HTTP attempt (including 429 retries) for RPS
            if self._stats:
                await self._stats.inc_requests()

            token = None
            if self._token_pool is not None:
                if held is not None and held[0] == proxy:
                    token = held[1]
                else:
                    try:
                        token = self._token_pool.acquire()
                    except Exception:
                        token = None
                    held = (proxy, token) if token else None

            if token:
                url = ENDPOINT_TOKEN
                headers = {**DEFAULT_HEADERS, "Authorization": token}
            else:
                url = ENDPOINT
                headers = DEFAULT_HEADERS

            try:
                try:
                    resp = await self.backend.post_json(
                        url,
                        {"username": username},
                        proxy=proxy,
                        headers=headers,
                    )
                except Exception:
                    await self.pm.release(proxy)
                    raise
                # Nhịp AIMD/release tính từ lúc RESPONSE về (không tính
                # thời gian chờ mạng), no-op với scored/rotating/proxyless.
                await self.pm.release(proxy)
                dbg(f"  [{attempt}] {username} › HTTP {resp.status}")

                verdict, reason = classify_response(
                    resp.status, resp.data, resp.headers
                )

                # -- 429 Rate Limited --
                if verdict is Verdict.RATE_LIMITED:
                    cooldown = _retry_after(resp)

                    if token and reason not in ("cf_block", "cf_429"):
                        # Rate limit THẬT của Discord theo TOKEN -> cooldown
                        # token rồi xoay token khác, KHÔNG park proxy.
                        self._token_pool.mark_rate_limited(token, cooldown)
                        held = None
                        if self._stats:
                            await self._stats.inc_ratelimited()
                        continue

                    # cf_block/cf_429 = lỗi phía PROXY (Cloudflare/WAF), KHÔNG
                    # phải rate limit Discord -> giữ token (held giữ nguyên),
                    # rơi xuống nhánh park proxy 1800s/900s như luồng unauthed.
                    if self._scraped:
                        if proxy:
                            self.pm.set_rate_limit(proxy, _jittered_cooldown(cooldown))
                        if self._stats:
                            await self._stats.inc_ratelimited()
                        continue

                    if self._rotating:
                        # Fresh IP on next request — no point waiting
                        if self._stats:
                            await self._stats.inc_ratelimited()
                        continue

                    if self._stats:
                        await self._stats.inc_ratelimited()
                    if proxy:
                        if reason == "cf_block":
                            # Body JSON Cloudflare (code 0) -> ban dài 30 phút
                            parked = await self.pm.park(proxy, 1800.0)
                            console.print(
                                f"[{C.DANGER}]Proxy {_mask_proxy(proxy)} bị Cloudflare block "
                                f"› tạm nghỉ {parked:.0f}s[/]"
                            )
                            await self._err.inc("cf_ban_park")
                        elif reason == "cf_429":
                            # 429 HTML/không JSON (WAF) -> park 15 phút
                            parked = await self.pm.park(proxy, 900.0)
                            console.print(
                                f"[{C.DANGER}]Proxy {_mask_proxy(proxy)} bị WAF 429 chặn "
                                f"› tạm nghỉ {parked:.0f}s[/]"
                            )
                            await self._err.inc("cf_html_park")
                        else:
                            # 429 JSON thường (Discord): AIMD + leo thang tái phạm
                            self.pm.record_429(proxy)  # AIMD: giãn interval proxy này
                            # FakePM cũ trong test có thể thiếu park_level -> coi như lần đầu
                            level = getattr(self.pm, "park_level", lambda _p: 0)(proxy)
                            if level >= 1:
                                # Đã từng bị park -> tái phạm: park leo thang 60*3^level
                                sec = max(cooldown, 60.0 * (3.0 ** level))
                                parked = await self.pm.park(proxy, sec)
                                console.print(
                                    f"[{C.WARNING}]Proxy {_mask_proxy(proxy)} 429 tái phạm "
                                    f"(retry_after {cooldown:.0f}s, level {level}) "
                                    f"› tạm nghỉ {parked:.0f}s[/]"
                                )
                                await self._err.inc("429_park")
                            else:
                                await self.pm.set_cooldown(proxy, _jittered_cooldown(cooldown))
                    else:
                        await self._err.inc("429_proxyless")
                        await asyncio.sleep(_jittered_cooldown(cooldown))
                    continue

                # -- Available --
                if verdict is Verdict.AVAILABLE:
                    await self._apply_proactive_cooldown(resp, proxy)
                    if self._double_check:
                        # Xác nhận hit bằng proxy khác trước khi chốt
                        confirmed = await self._confirm_available(
                            username, proxy, tried, primary_used_token=bool(token)
                        )
                        if not confirmed:
                            await self._err.inc("double_check_conflict")
                            continue
                    if self._scraped and proxy:
                        self.pm.score_hit(proxy)
                    elif not self._rotating:
                        self.pm.record_success(proxy)
                        self.pm.record_ok(proxy)      # AIMD: hồi interval
                        await self.pm.unpark(proxy)   # reset park level
                    if self._cb:
                        await self._cb.record_success()
                    return (True, resp.data, resp.status)

                # -- Taken (Discord trả lời dứt khoát) --
                if verdict is Verdict.TAKEN:
                    await self._apply_proactive_cooldown(resp, proxy)
                    if self._scraped and proxy:
                        self.pm.score_hit(proxy)
                    elif not self._rotating:
                        self.pm.record_success(proxy)
                        self.pm.record_ok(proxy)      # AIMD: hồi interval
                        await self.pm.unpark(proxy)   # reset park level
                    if self._cb:
                        await self._cb.record_success()
                    return (False, resp.data, resp.status)

                # -- Invalid username (Discord nói rõ trong JSON) --
                if verdict is Verdict.INVALID:
                    await self._apply_proactive_cooldown(resp, proxy)
                    if not self._rotating:
                        self.pm.record_success(proxy)
                        self.pm.record_ok(proxy)      # AIMD: hồi interval
                        await self.pm.unpark(proxy)   # reset park level
                    if self._cb:
                        await self._cb.record_success()
                    return (False, resp.data, resp.status)

                # -- Retry: route theo reason --
                if reason == "bad_schema":
                    await self._err.inc("proxy_bad_response")
                    if self._scraped and proxy:
                        self.pm.score_miss(proxy)
                    elif not self._rotating:
                        await self.pm.record_failure(proxy)
                    await asyncio.sleep(0.3)
                    continue

                if reason == "waf_block":
                    if token and is_dead_token_response(resp.status, resp.data):
                        # 401/403 kèm body lỗi auth Discord (message + code)
                        # = token chết THẬT -> loại token, KHÔNG park proxy.
                        self._token_pool.mark_dead(token)
                        held = None
                        await self._err.inc("token_dead")
                        continue
                    # Body HTML/None (Cloudflare challenge/auth proxy) = lỗi
                    # phía proxy -> xử lý như unauthed bên dưới, KHÔNG giết
                    # token; held giữ nguyên cho vòng lặp sau.
                    await self._err.inc(f"http_{resp.status}")
                    if self._scraped and proxy:
                        self.pm.score_miss(proxy)
                    elif not self._rotating and proxy:
                        # Cloudflare block (403) không phải proxy chết vĩnh
                        # viễn: park 15 phút rồi thử lại khi ban hết
                        # (KHÔNG fatal=True như bug cũ).
                        parked = await self.pm.park(proxy, 900.0)
                        console.print(
                            f"[{C.WARNING}]Proxy {_mask_proxy(proxy)} WAF 403 "
                            f"› tạm nghỉ {parked:.0f}s[/]"
                        )
                        await self._err.inc("403_park")
                    await asyncio.sleep(0.5)
                    continue

                if reason == "http_5xx":
                    # Proxy-side 5xx (bad gateway from proxy itself)
                    await self._err.inc(f"http_{resp.status}")
                    if self._scraped and proxy:
                        self.pm.score_miss(proxy)
                    elif not self._rotating:
                        await self.pm.record_failure(proxy)
                    await asyncio.sleep(0.5)
                    continue

                # http_404 / bad_400 / status lạ – brief wait, retry
                await self._err.inc(f"http_{resp.status}")
                await asyncio.sleep(0.5)
                continue

            except BackendError as exc:
                dbg(f"  [{attempt}] {username} › {exc.kind}")
                await self._err.inc(f"conn_{exc.kind}")

                # -- Scraped free proxies: one-shot --
                if self._scraped:
                    if proxy:
                        self.pm.score_miss(proxy)
                    continue

                # -- Rotating proxy: progressive backoff + circuit breaker --
                if self._rotating:
                    if self._cb:
                        await self._cb.record_failure()
                        await self._cb.wait_if_open()
                    # Per-worker progressive backoff
                    backoff = min(attempt * 0.25, 3.0)
                    await asyncio.sleep(backoff)
                    continue

                # -- Static proxy list: cooldown + backoff --
                await self._err.inc("proxy_conn_err")
                await self.pm.set_cooldown(proxy, 3)
                await self.pm.record_failure(proxy)
                backoff = min(attempt * 0.15, 5.0)
                await asyncio.sleep(backoff)
                continue

            except Exception as exc:
                # CancelledError là BaseException (3.11) -> tự propagate,
                # không bao giờ bị nuốt ở đây.
                dbg(f"  [{attempt}] {username} › {type(exc).__name__}: {exc!s:.100}")
                await asyncio.sleep(0.3)
                continue


# ---------------------------------------------------------------------------
# WebhookSender – async webhook dispatcher
# ---------------------------------------------------------------------------


class WebhookSender:
    """Sends found usernames to Discord webhook in batches to avoid rate-limits.

    Discord webhook limit: 5 requests / 2 seconds, 2000 chars / message.
    Strategy: accumulate hits, flush batch every 3s or when 15 names / 1800 chars.
    Session riêng (lazy) – không mượn session của checker; tự tạo khi cần gửi.
    """

    DISCORD_MAX_CHARS = 1950  # leave 50 char margin
    BATCH_SIZE = 15
    FLUSH_INTERVAL = 3.0  # seconds
    MAX_SEND_ATTEMPTS = 5

    def __init__(
        self,
        webhook_url: str,
        message_template: str,
        start_time: float = 0.0,
    ) -> None:
        self.url = webhook_url
        self.template = message_template
        self.start_time = start_time
        self._sent: set[str] = set()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._buffer: list[str] = []
        self._buffer_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._failed_warned = False  # chỉ cảnh báo 1 lần khi ghi webhook_failed.txt lỗi

    def enqueue(self, username: str) -> None:
        """Called by workers when a hit is found (non-blocking)."""
        if username not in self._sent:
            self._sent.add(username)
            self._queue.put_nowait(username)

    def _get_session(self) -> aiohttp.ClientSession:
        """Tạo session lazy – chỉ khi thực sự cần gửi."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def run(self) -> None:
        """Background task: accumulate hits, flush in batches.

        On cancellation (checker finished), drains the queue and flushes
        the remaining buffer so last-second hits are never lost.
        """
        last_flush = time.time()

        try:
            while True:
                try:
                    username = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Timeout – flush if buffer has items and enough time passed
                    async with self._buffer_lock:
                        if self._buffer and time.time() - last_flush >= self.FLUSH_INTERVAL:
                            await self._flush_locked()
                            last_flush = time.time()
                    continue

                async with self._buffer_lock:
                    self._buffer.append(username)
                    count = len(self._buffer)

                    # Build preview to check message length
                    hit_lines = [self.template.replace("<name>", n) for n in self._buffer]
                    preview = "\n".join(hit_lines)

                    should_flush = (
                        count >= self.BATCH_SIZE
                        or len(preview) >= self.DISCORD_MAX_CHARS
                        or (count > 0 and time.time() - last_flush >= self.FLUSH_INTERVAL)
                    )

                    if should_flush:
                        await self._flush_locked()
                        last_flush = time.time()
        except asyncio.CancelledError:
            # Vét cạn queue + gửi hết mọi batch còn lại trước khi thoát.
            # shield: cancel lần hai không cắt ngang request đang gửi dở.
            try:
                while not self._queue.empty():
                    try:
                        self._buffer.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                async with self._buffer_lock:
                    while self._buffer:
                        await asyncio.shield(self._flush_locked())
            except Exception:
                pass
            raise

    async def close(self) -> None:
        """Drain queue, flush hết mọi batch còn lại rồi đóng session (idempotent).

        Endgame best-effort ghi phần còn lại vào webhook_failed.txt nên batch
        không bao giờ chỉ nằm trong RAM.
        """
        try:
            while not self._queue.empty():
                try:
                    self._buffer.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            async with self._buffer_lock:
                while self._buffer:
                    # shield: cancel không cắt ngang request đang gửi dở;
                    # batch bị cancel sẽ được trả về buffer (xem _flush_locked).
                    try:
                        await asyncio.shield(self._flush_locked())
                    except asyncio.CancelledError:
                        break
        except Exception:
            pass
        # Endgame best-effort: batch còn lại không bao giờ chỉ nằm trong RAM.
        try:
            if self._buffer:
                self._persist_failed(list(self._buffer))
                self._buffer.clear()
        except Exception:
            pass
        try:
            session = self._session
            self._session = None
            if session is not None and not session.closed:
                await session.close()
        except Exception:
            pass

    async def _flush_locked(self) -> None:
        """Gửi batch hits trong buffer. Phải gọi khi đang giữ _buffer_lock.

        Chỉ pop đúng batch vừa ngưỡng (BATCH_SIZE / DISCORD_MAX_CHARS), phần dư
        giữ trong buffer cho lần gửi kế — không bao giờ mất tên do vượt giới
        hạn ký tự.

        Bảo đảm at-least-once: nếu bị cancel giữa vòng gửi hoặc trong lúc
        sleep backoff, batch đã pop được trả về đầu buffer và sẽ được gửi
        lại ở lần flush sau. Thà chấp nhận gửi trùng hiếm gặp (Discord có
        thể đã nhận trước khi cancel) còn hơn mất tên.
        """
        if not self._buffer:
            return

        # Build message – resolve placeholders
        now = time.time()
        elapsed = int(now - self.start_time)
        ts_discord = f"<t:{int(now)}:R>"

        def _fill(n: str) -> str:
            return (
                self.template
                .replace("<name>", n)
                .replace("<t:time:R>", ts_discord)
                .replace("<time>", ts_discord)
                .replace("<elapsed>", str(elapsed))
            )

        # Chọn vừa đủ batch: dòng nào làm vượt giới hạn thì DỪNG (không pop),
        # giữ nguyên tên trong buffer cho lần flush sau.
        names: list[str] = []
        lines: list[str] = []
        total_len = 0
        while self._buffer and len(lines) < self.BATCH_SIZE:
            name = self._buffer[0]
            line = _fill(name)
            extra = len(line) + (1 if lines else 0)
            if lines and total_len + extra > self.DISCORD_MAX_CHARS:
                break
            if not lines and len(line) > self.DISCORD_MAX_CHARS:
                # Một tên đơn lẻ không thể lùi batch: cắt ngắn nhưng vẫn gửi
                line = line[: self.DISCORD_MAX_CHARS - 3] + "..."
            self._buffer.pop(0)
            names.append(name)
            lines.append(line)
            total_len += extra

        msg = "\n".join(lines)

        payload = {
            "content": msg,
            "username": "Discord Username Finder",
            "avatar_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/95/Burning_Yellow_Sunset.jpg/1280px-Burning_Yellow_Sunset.jpg",
        }

        # Gửi tối đa 5 lần: 429 -> chờ retry_after clamp [1,30] rồi thử lại;
        # status khác / exception -> backoff 1-8s rồi thử lại.
        # Batch đã bị pop khỏi buffer: nếu bị cancel giữa vòng gửi thì trả
        # batch về đầu buffer trước khi raise để không mất tên.
        try:
            for attempt in range(self.MAX_SEND_ATTEMPTS):
                try:
                    session = self._get_session()
                    async with session.post(self.url, json=payload) as resp:
                        if resp.status == 429:
                            try:
                                data = await resp.json()
                                wait = float(data.get("retry_after", 5))
                            except Exception:
                                wait = 5.0
                            if not math.isfinite(wait):
                                wait = 5.0
                            await asyncio.sleep(min(max(wait, 1.0), 30.0))
                            continue
                        if resp.status in (200, 204):
                            return
                        # Status khác -> coi như fail, retry
                except Exception:
                    pass
                # Chỉ backoff khi còn lượt thử (1, 2, 4, 8)
                if attempt < self.MAX_SEND_ATTEMPTS - 1:
                    await asyncio.sleep(min(2 ** attempt, 16))
        except asyncio.CancelledError:
            self._buffer[0:0] = names
            raise

        # Gửi thất bại hẳn -> lưu tên vào file để không mất hit
        self._persist_failed(names)

    def _persist_failed(self, names: list[str]) -> None:
        """Lưu tên vào data/output/webhook_failed.txt khi gửi thất bại hẳn.

        Không raise: ghi file lỗi thì cảnh báo đúng một lần qua console
        (best-effort, không chặn luồng shutdown).
        """
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            path = OUTPUT_DIR / "webhook_failed.txt"
            with path.open("a", encoding="utf-8") as fh:
                for name in names:
                    fh.write(f"{name}\n")
        except Exception as exc:
            if not self._failed_warned:
                self._failed_warned = True
                console.print(
                    f"[{C.DANGER}]! Không ghi được webhook_failed.txt "
                    f"— {len(names)} tên có thể chưa lưu: {exc}[/]"
                )
        dbg(
            f"[webhook] gửi thất bại sau {self.MAX_SEND_ATTEMPTS} lần, "
            f"lưu {len(names)} tên vào data/output/webhook_failed.txt"
        )


# ---------------------------------------------------------------------------
# ThirdPartyResolver – fallback API bên thứ ba cho pass 2 (best-effort)
# ---------------------------------------------------------------------------

THIRD_PARTY_TIMEOUT = 2.0


class ThirdPartyResolver:
    """Adapter API bên thứ ba: tên -> True (available) / False (taken) / None.

    - URL chứa '{name}' -> thế bằng quote(name); không có -> append
      '/quote(name)' sau rstrip('/').
    - GET + parse JSON dict: 'available' bool -> trả; 'taken' bool ->
      not taken; còn lại -> None. Non-200/lỗi mạng/timeout/JSON rác -> None.
    - Không bao giờ raise (trừ CancelledError); session lazy, close()
      idempotent – mẫu WebhookSender.
    """

    def __init__(self, api_url: str, timeout: float = THIRD_PARTY_TIMEOUT) -> None:
        self.api_url = api_url
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def from_config(cls, url) -> "ThirdPartyResolver | None":
        """Tạo resolver từ config; falsy / không phải http(s) -> None (skip)."""
        text = str(url or "").strip()
        if not text or not text.startswith(("http://", "https://")):
            return None
        return cls(text)

    def _get_session(self) -> aiohttp.ClientSession:
        """Tạo session lazy – chỉ khi thực sự cần gọi API."""
        session = self._session
        if session is None or session.closed:
            session = aiohttp.ClientSession(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
            self._session = session
        return session

    def _build_url(self, name: str) -> str:
        encoded = quote(str(name), safe="")
        if "{name}" in self.api_url:
            return self.api_url.replace("{name}", encoded)
        return self.api_url.rstrip("/") + "/" + encoded

    async def resolve(self, name: str) -> bool | None:
        """True=available, False=taken, None=unknown. Mọi lỗi -> None."""
        try:
            session = self._get_session()
            async with session.get(self._build_url(name)) as resp:
                if resp.status != 200:
                    return None
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    return None
                if not isinstance(data, dict):
                    return None
                available = data.get("available")
                if isinstance(available, bool):
                    return available
                taken = data.get("taken")
                if isinstance(taken, bool):
                    return not taken
                return None
        except asyncio.CancelledError:
            # Không bao giờ nuốt cancellation.
            raise
        except Exception:
            return None

    async def close(self) -> None:
        """Đóng session (idempotent, best-effort, không bao giờ raise)."""
        session, self._session = self._session, None
        if session is None:
            return
        try:
            if not session.closed:
                await session.close()
        except Exception:
            pass
