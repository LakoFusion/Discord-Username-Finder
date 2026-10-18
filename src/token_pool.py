#!/usr/bin/env python3
"""Discord Username Finder v3.1 – Token pool cho chế độ token (xoay vòng, cooldown, validate)."""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

import aiohttp

from src.config import DEFAULT_HEADERS

_ME_ENDPOINT = "https://discord.com/api/v9/users/@me"
_VALIDATE_CONCURRENCY = 10


def _now() -> float:
    """Đồng hồ monotonic của pool (tách module-level để test monkeypatch)."""
    return time.monotonic()


def _clamp_retry_after(seconds: float, default: float = 5.0) -> float:
    """Cap retry_after về khoảng an toàn 0.5-1800s (chống retry_after độc hại).

    NaN/None/rác -> default; +inf -> 1800s; -inf/âm -> 0.5s.
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return default
    if math.isnan(value):
        return default
    if not math.isfinite(value):
        return 1800.0 if value > 0 else 0.5
    return min(max(value, 0.5), 1800.0)


async def _probe_token(token: str, timeout: float) -> tuple[int | None, object]:
    """GET /users/@me để kiểm tra token.

    Trả (status, body JSON hoặc None); (None, None) khi mọi lỗi. Chỉ đọc
    body khi 401/403 để phân loại token chết đúng rule auth Discord.
    Không bao giờ log/đưa token vào thông điệp lỗi; session luôn được đóng.
    """
    session: aiohttp.ClientSession | None = None
    try:
        session = aiohttp.ClientSession(
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=timeout),
        )
        async with session.get(
            _ME_ENDPOINT,
            headers={
                "Authorization": token,
                "User-Agent": DEFAULT_HEADERS["User-Agent"],
            },
        ) as resp:
            if resp.status in (401, 403):
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                return (resp.status, data)
            return (resp.status, None)
    except Exception:
        return (None, None)
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass


def is_dead_token_response(status: int | None, data: object) -> bool:
    """401/403 chỉ là token chết khi body JSON lỗi auth của Discord.

    Discord trả {"message": "...", "code": 0}; HTML/None (Cloudflare, proxy)
    thì không kết luận token chết.
    """
    if status not in (401, 403):
        return False
    return (
        isinstance(data, dict)
        and isinstance(data.get("message"), str)
        and isinstance(data.get("code"), int)
    )


class TokenPool:
    """Bể token xoay vòng: cooldown khi 429, dead khi 401/403 kèm body lỗi auth
    Discord (is_dead_token_response), validate 1 lần.

    `acquire()` là hàm đồng bộ vì pool chỉ chạy trong 1 event loop duy nhất.
    """

    def __init__(self, tokens: list[str] | None, validate_timeout: float = 5.0) -> None:
        """Nạp token: bỏ phi-str/rỗng/chứa khoảng trắng, khử trùng theo thứ tự."""
        clean: list[str] = []
        seen: set[str] = set()
        for raw in tokens or []:
            if not isinstance(raw, str):
                continue
            token = raw.strip()
            if not token or token in seen:
                continue
            if any(ch.isspace() for ch in token):
                continue
            seen.add(token)
            clean.append(token)
        self._tokens: list[str] = clean
        self._validate_timeout = validate_timeout
        self._dead: set[str] = set()
        self._cooldowns: dict[str, float] = {}  # token -> hạn cooldown (monotonic)
        self._cursor: int = 0

    @classmethod
    def from_file(cls, path: str | Path, validate_timeout: float = 5.0) -> "TokenPool":
        """Nạp token từ file (mỗi dòng 1 token). Thiếu/không đọc được -> bể rỗng."""
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, ValueError):
            return cls([], validate_timeout=validate_timeout)
        return cls(text.splitlines(), validate_timeout=validate_timeout)

    # -- Properties --

    @property
    def total(self) -> int:
        """Số token nạp ban đầu (sau lọc rỗng/trùng)."""
        return len(self._tokens)

    @property
    def size(self) -> int:
        """Số token còn sống (không dead; đang cooldown vẫn tính là sống)."""
        return sum(1 for t in self._tokens if t not in self._dead)

    # -- Cấp phát --

    def acquire(self) -> str | None:
        """Token kế tiếp theo round-robin; None nếu hết token dùng được.

        Bỏ qua token dead + đang cooldown; cooldown hết hạn được dọn lazily.
        """
        # SYNC: chỉ 1 event loop sở hữu pool -> không cần await/lock.
        n = len(self._tokens)
        if n == 0:
            return None
        now = _now()
        for step in range(n):
            idx = (self._cursor + step) % n
            token = self._tokens[idx]
            if token in self._dead:
                continue
            expiry = self._cooldowns.get(token, 0.0)
            if expiry > now:
                continue
            self._cooldowns.pop(token, None)
            self._cursor = (idx + 1) % n
            return token
        return None

    def mark_rate_limited(self, token: str, retry_after: float) -> None:
        """Đặt cooldown cho token đến _now() + clamp(retry_after, 0.5-1800s)."""
        if not isinstance(token, str) or not token:
            return
        self._cooldowns[token] = _now() + _clamp_retry_after(retry_after)

    def mark_dead(self, token: str) -> None:
        """Đánh dấu token chết (theo rule auth 401/403) — không bao giờ cấp lại."""
        if token:
            self._dead.add(token)

    def __repr__(self) -> str:
        """Không bao giờ để lộ token trong repr/log."""
        return f"TokenPool(total={self.total}, usable={self.size})"

    # -- Validate --

    async def validate(self) -> tuple[int, int]:
        """Kiểm tra toàn bộ token (semaphore 10). Trả về (alive, dead).

        - 200 -> sống.
        - 401/403 CHỈ mark_dead khi body JSON lỗi auth Discord (message
          str + code int); HTML/None (Cloudflare/proxy) -> giữ nguyên.
        - None/status khác (429, 5xx, timeout...) -> giữ nguyên.
        - Bể rỗng -> (0, 0) và không gọi mạng; không bao giờ raise.
        """
        tokens = list(self._tokens)
        if not tokens:
            return (0, 0)
        timeout = self._validate_timeout
        sem = asyncio.Semaphore(_VALIDATE_CONCURRENCY)

        async def _check(token: str) -> None:
            async with sem:
                try:
                    status, data = await _probe_token(token, timeout)
                except Exception:
                    return  # lỗi bất ngờ -> giữ token (không mark dead)
            if status == 200:
                return
            if is_dead_token_response(status, data):
                self.mark_dead(token)

        try:
            await asyncio.gather(*(_check(t) for t in tokens))
        except Exception:
            pass  # phòng thủ: validate không bao giờ raise
        alive = sum(1 for t in tokens if t not in self._dead)
        return (alive, len(tokens) - alive)
