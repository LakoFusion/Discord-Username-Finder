#!/usr/bin/env python3
"""Discord Username Finder v3.1 – Proxy manager with rotation, cooldowns, and scoring."""

from __future__ import annotations

import asyncio
import heapq
import math
import random
import time
from collections import deque

import aiohttp

from src.config import DEFAULT_HEADERS, ENDPOINT, normalize_proxy


def _clamp_cooldown(seconds: float, default: float = 5.0) -> float:
    """Cap cooldown về khoảng an toàn 0.5-60s (chống retry_after độc hại).

    NaN/None/rác -> default; +inf -> 60s; -inf/âm -> 0.5s.
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return default
    if math.isnan(value):
        return default
    if not math.isfinite(value):
        return 60.0 if value > 0 else 0.5
    return min(max(value, 0.5), 60.0)


def _clamp_park(seconds: float) -> float:
    """Cap thời gian park proxy vào khoảng an toàn 60-1800s.

    Ban Cloudflare thường kéo dài 15-30 phút nên cooldown ngắn (≤60s)
    không đủ. NaN/None/rác -> 60s; +inf -> 1800s; -inf/âm -> 60s.
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return 60.0
    if math.isnan(value):
        return 60.0
    if not math.isfinite(value):
        return 1800.0 if value > 0 else 60.0
    return min(max(value, 60.0), 1800.0)


class ProxyManager:
    """Manages proxy rotation with per-proxy ratelimit cooldowns.

    For a single proxy (rotating gateway) every request gets a fresh
    IP from the pool — the proxy itself is never marked dead.

    Hàng đợi nội bộ dùng deque (`_ready`) + min-heap (`_cooldown_heap`)
    với lazy invalidation: pick thường là O(1), không scan O(N) mỗi lần
    và không giữ lock lâu.

    Bể static nhiều proxy có thêm 3 cơ chế điều tiết cho engine:
    `park()` khóa proxy dài hạn (60-1800s, đủ cho ban Cloudflare),
    AIMD per-proxy (`record_429`/`record_ok`) tự chỉnh giãn cách theo lỗi,
    và `release()` giãn cách sau mỗi lần trả proxy về bể (chỉ kéo dài,
    không bao giờ rút ngắn cooldown đang có).
    """

    def __init__(
        self,
        proxies: list[str],
        remove_on_fail: bool = True,
        scored: bool = False,
        min_interval: float | None = None,
    ) -> None:
        """Khởi tạo bể proxy.

        *min_interval*: giãn cách tối thiểu (giây) giữa 2 lần cấp phát cùng
        1 proxy để chống dồn request lên cùng IP. None -> tự chọn theo loại
        bể: static 1.8s, scored/rotating/proxyless 0.0 (không cần).
        """
        cleaned: list[str | None] = []
        for p in proxies or []:
            if p is None:
                cleaned.append(None)
                continue
            if not isinstance(p, str):
                continue
            if not p.strip():
                continue
            norm = normalize_proxy(p)
            # Loại socks4/5 + dòng rác ngay từ đầu để aiohttp không bao
            # giờ nhận proxy scheme không hỗ trợ (ValueError).
            if norm is not None:
                cleaned.append(norm)
        self._proxies: list[str | None] = cleaned if cleaned else [None]
        self._proxyless = all(p is None for p in self._proxies)
        self._dead: set[str] = set()
        self._cooldowns: dict[str, float] = {}
        self.remove_on_fail = remove_on_fail
        self._lock = asyncio.Lock()
        # Bộ đếm lỗi liên tiếp cho static proxies (3-strike rule)
        self._fail_counts: dict[str, int] = {}

        # -- Hàng đợi lazy (collections.deque + heapq) --
        # _ready: proxy sẵn sàng ngay; _in_ready: membership O(1).
        # _cooldowns: key -> thời điểm "ấm" lại (time.monotonic()).
        # _cooldown_heap: min-heap (ready_at, key), có thể chứa entry cũ
        # (lazy invalidation).
        self._ready: deque[str | None] = deque(self._proxies)
        self._in_ready: set[str] = {p for p in self._proxies if p}
        self._cooldown_heap: list[tuple[float, str]] = []

        # -- scoring (scraped free proxies only) --
        self._scored = scored
        self._scores: dict[str, int] = {}
        self._rate_limited_until: dict[str, float] = {}
        if scored:
            for raw in self._proxies:
                if raw:
                    self._scores[raw] = 1

        # -- Per-proxy pacing: giãn cách tối thiểu giữa 2 lần cấp phát
        # cùng 1 proxy (chống dồn request lên cùng IP -> 429) --
        if min_interval is None:
            if scored or self._proxyless or self.is_single:
                self._min_interval = 0.0        # scraped/rotating/proxyless: không cần
            else:
                self._min_interval = 1.8        # static list
        else:
            try:
                self._min_interval = max(0.0, float(min_interval))
            except (TypeError, ValueError):
                self._min_interval = 0.0

        # -- Park + AIMD per-proxy (engine/checker điều khiển) --
        # _park_levels: số lần bị park liên tiếp (escalation).
        # _intervals: giãn cách riêng từng proxy (AIMD), ghi đè _min_interval.
        # _ok_streaks: chuỗi OK liên tiếp để quyết định giảm interval.
        self._park_levels: dict[str, int] = {}
        self._intervals: dict[str, float] = {}
        self._ok_streaks: dict[str, int] = {}

    # -- Properties --

    @property
    def is_proxyless(self) -> bool:
        return self._proxyless

    @property
    def is_single(self) -> bool:
        """True if exactly one proxy (rotating gateway)."""
        return len(self._proxies) == 1 and not self._proxyless

    @property
    def alive_count(self) -> int:
        """Number of proxies that are not dead (for display)."""
        if self.is_single:
            return 1  # rotating gateway is always "alive"
        return max(0, len(self._proxies) - len(self._dead))

    @property
    def min_interval(self) -> float:
        """Giãn cách tối thiểu mặc định (giây) — checker dùng để stagger."""
        return self._min_interval

    @property
    def parked_count(self) -> int:
        """Số proxy đang bị park (dùng cho hiển thị)."""
        return len(self._park_levels)

    # -- Formatting --

    @staticmethod
    def _format(raw: str | None) -> str | None:
        # Giữ lại để tương thích ngược; logic mới dùng normalize_proxy.
        return normalize_proxy(raw)

    # -- Nội bộ hàng đợi (luôn gọi trong lock) --

    def _refill_locked(self, now: float) -> None:
        """Chuyển proxy hết cooldown từ heap về _ready.

        Chỉ đụng vào heap; entry cũ (dead/cooldown bị kéo dài)
        được xử lý lazily ngay tại đây.
        """
        heap = self._cooldown_heap
        while heap and heap[0][0] <= now:
            _, key = heapq.heappop(heap)
            if not key or key in self._dead:
                continue
            if key in self._in_ready:
                continue
            if self._cooldowns.get(key, 0.0) > now:
                # Entry cũ: cooldown đã bị kéo dài -> đẩy lại deadline mới.
                heapq.heappush(heap, (self._cooldowns[key], key))
                continue
            self._ready.append(key)
            self._in_ready.add(key)

    def _pace_locked(self, key: str, now: float) -> None:
        """Đặt giãn cách tối thiểu sau mỗi lần cấp phát proxy (gọi trong lock).

        Dùng interval riêng của proxy nếu AIMD đã điều chỉnh
        (`_intervals`), ngược lại dùng mặc định `_min_interval`.
        Ghi deadline vào `_cooldowns` + đẩy heap; entry còn trong deque sẽ
        được xử lý lazily như cooldown thường (pop ra gặp cooldown -> heap).
        """
        if not key:
            return
        iv = self._intervals.get(key, self._min_interval)
        if iv <= 0:
            return
        deadline = now + iv
        if self._cooldowns.get(key, 0.0) < deadline:
            self._cooldowns[key] = deadline
        heapq.heappush(self._cooldown_heap, (deadline, key))

    def _weighted_pick(self, pool: list[str]) -> str:
        """Chọn weighted random theo _scores (tổng <= 0 -> phần tử đầu)."""
        weights = [self._scores.get(k, 0) for k in pool]
        total = sum(weights)
        if total <= 0:
            return pool[0]
        pick = random.uniform(0, total)
        acc = 0.0
        for key, weight in zip(pool, weights):
            acc += weight
            if acc >= pick:
                return key
        return pool[-1]

    # -- Core: next proxy --

    async def next(self, exclude: set[str] | None = None) -> str | None:
        """Return the next ready proxy.

        - Proxyless → None (valid).
        - Rotating gateway → always returns the proxy immediately.
        - Static list → skips dead & cooldown proxies, waits if all busy.
        - Scored (free) → weighted random pick from live proxies.
        - *exclude*: tập proxy vừa lỗi của username hiện tại -> bỏ qua.
        - Pacing: sau mỗi lần cấp phát ở bể static, proxy bị giãn cách
          tối thiểu `_min_interval` giây trước khi được cấp lại (chống 429);
          scored/rotating/proxyless không áp dụng.

        Không bao giờ await/hold lock trong lúc mutate; mọi sleep nằm
        ngoài lock.
        """
        if self._proxyless:
            return None

        # Rotating gateway – never block
        if self.is_single:
            return self._format(self._proxies[0])

        # Scored path (scraped free proxies) – weighted random trên _ready
        if self._scored:
            while True:
                wait = 0.0
                chosen: str | None = None
                async with self._lock:
                    now = time.monotonic()
                    self._refill_locked(now)
                    restored: list[str] = []
                    candidates: list[str] = []
                    fallback: str | None = None
                    # Pop tối đa 32 đầu deque/lần (O(1) mỗi lần pop)
                    for _ in range(min(len(self._ready), 32)):
                        key = self._ready.popleft()
                        if not key or key in self._dead:
                            if key:
                                self._in_ready.discard(key)
                            continue
                        self._in_ready.discard(key)
                        if key not in self._scores:
                            continue  # không còn được theo dõi điểm -> bỏ
                        if self._cooldowns.get(key, 0.0) > now:
                            heapq.heappush(
                                self._cooldown_heap,
                                (self._cooldowns[key], key),
                            )
                            continue
                        if self._rate_limited_until.get(key, 0.0) > now:
                            heapq.heappush(
                                self._cooldown_heap,
                                (self._rate_limited_until[key], key),
                            )
                            continue
                        restored.append(key)
                        if exclude and key in exclude:
                            # Né exclude; giữ fallback nếu mọi proxy đã thử
                            if fallback is None:
                                fallback = key
                            continue
                        candidates.append(key)
                    # Trả hết về cuối deque để bảo toàn vòng xoay
                    for key in restored:
                        self._ready.append(key)
                        self._in_ready.add(key)
                    if candidates:
                        chosen = self._weighted_pick(candidates)
                    elif fallback is not None:
                        chosen = fallback
                    elif self._cooldown_heap:
                        wait = min(
                            max(self._cooldown_heap[0][0] - now, 0.01), 2.0
                        )
                    elif not self._ready:
                        return None  # thực sự hết proxy sống
                    # else: còn entry rác phía sau -> quét tiếp (wait 0.0)
                if chosen is not None:
                    return chosen
                await asyncio.sleep(wait)

        # Static multi-proxy path
        while True:
            wait = 0.05
            async with self._lock:
                if len(self._dead) >= len(self._proxies):
                    return None

                now = time.monotonic()
                self._refill_locked(now)
                picked: str | None = None
                fallback: str | None = None
                # Static: xoay vòng, ưu tiên né exclude nhưng fallback
                # dùng lại proxy trong exclude khi đã thử hết 1 vòng (không
                # báo EXHAUSTED oan, không treo khi exclude phủ kín bể).
                # Quét tối đa 8 đầu deque/lần để tránh O(N).
                for _ in range(min(len(self._ready), 8)):
                    key = self._ready.popleft()
                    if not key or key in self._dead:
                        if key:
                            self._in_ready.discard(key)
                        continue
                    self._in_ready.discard(key)
                    if self._cooldowns.get(key, 0.0) > now:
                        heapq.heappush(
                            self._cooldown_heap, (self._cooldowns[key], key)
                        )
                        continue
                    if exclude and key in exclude:
                        if fallback is None:
                            fallback = key
                        self._ready.append(key)
                        self._in_ready.add(key)
                        continue
                    picked = key
                    self._ready.append(key)
                    self._in_ready.add(key)
                    break
                if picked is not None:
                    self._pace_locked(picked, now)
                    return picked
                if fallback is not None:
                    self._pace_locked(fallback, now)
                    return fallback
                if self._ready:
                    # Còn entry chưa quét hết -> quét tiếp ngay, không chờ.
                    wait = 0.0
                elif self._cooldown_heap:
                    wait = min(max(self._cooldown_heap[0][0] - now, 0.01), 2.0)
                else:
                    return None  # hết proxy sống thật sự
            await asyncio.sleep(wait)

    # -- Scoring API (called by Checker for scraped proxies) --

    def score_hit(self, proxy: str | None) -> None:
        """+5 points for a working proxy (scraped) + reset fail streak."""
        if proxy:
            self._fail_counts.pop(proxy, None)
        if not self._scored or not proxy:
            return
        key = proxy
        if key in self._scores:
            self._scores[key] = min(self._scores[key] + 5, 100)

    def record_success(self, proxy: str | None) -> None:
        """Reset consecutive-failure streak for a static proxy."""
        if proxy:
            self._fail_counts.pop(proxy, None)

    def set_rate_limit(self, proxy: str | None, seconds: float) -> None:
        """Mark proxy as rate-limited until *now + seconds* (monotonic)."""
        if not self._scored or not proxy:
            return
        key = proxy
        if key in self._scores:
            self._rate_limited_until[key] = time.monotonic() + _clamp_cooldown(seconds)

    def score_miss(self, proxy: str | None) -> None:
        """-1 point for a failed proxy.  Score ≤ 0 → removed."""
        if proxy:
            # Đồng bộ với 3-strike rule cho static list
            self._fail_counts[proxy] = self._fail_counts.get(proxy, 0) + 1
        if not self._scored or not proxy:
            return
        key = proxy
        if key in self._scores:
            self._scores[key] -= 1
            if self._scores[key] <= 0:
                self._dead.add(key)
                self._in_ready.discard(key)
                del self._scores[key]

    async def record_failure(self, proxy: str | None, *, fatal: bool = False) -> None:
        """Record one failure for a static proxy (3-strike rule).

        Guard clauses: proxyless / rotating-gateway -> never mark dead
        via this path (rotating IP must stay).
        """
        if proxy is None:
            return
        if self.is_single:
            return
        async with self._lock:
            count = self._fail_counts.get(proxy, 0) + 1
            self._fail_counts[proxy] = count
            if not self.remove_on_fail:
                return
            if fatal or count >= 3:
                self._dead.add(proxy)
                self._in_ready.discard(proxy)

    async def set_cooldown(self, proxy: str | None, seconds: float) -> None:
        if proxy is None:
            return
        async with self._lock:
            deadline = time.monotonic() + _clamp_cooldown(seconds)
            self._cooldowns[proxy] = deadline
            if proxy in self._in_ready:
                # Deque entry sẽ tự bị đẩy sang heap khi pick (lazy),
                # KHÔNG pop ra khỏi deque ở đây.
                heapq.heappush(self._cooldown_heap, (deadline, proxy))

    # -- Park / AIMD / Release (engine điều tiết static list) --

    def park_level(self, proxy: str | None) -> int:
        """Số lần proxy bị park liên tiếp (0 nếu chưa từng bị park)."""
        if proxy is None:
            return 0
        return self._park_levels.get(proxy, 0)

    def _static_paced(self) -> bool:
        """True khi bể là static list nhiều proxy (không scored/rotating)."""
        return (
            not self._scored
            and not self._proxyless
            and not self.is_single
        )

    async def park(self, proxy: str | None, seconds: float) -> float:
        """Park 1 proxy trong *seconds* (clamp 60-1800s, đủ cho ban Cloudflare).

        Tăng `_park_levels` để engine biết mức escalation; trả về số giây
        thực sự đặt. Cooldown cũ bị ghi đè bởi deadline mới.
        """
        if proxy is None:
            return 60.0
        sec = _clamp_park(seconds)
        async with self._lock:
            self._park_levels[proxy] = self._park_levels.get(proxy, 0) + 1
            deadline = time.monotonic() + sec
            self._cooldowns[proxy] = deadline
            if proxy in self._in_ready:
                heapq.heappush(self._cooldown_heap, (deadline, proxy))
        return sec

    async def unpark(self, proxy: str | None) -> None:
        """Bỏ đánh dấu park (cooldown hiện tại KHÔNG bị đụng tới)."""
        if proxy is None:
            return
        async with self._lock:
            self._park_levels.pop(proxy, None)

    def record_429(self, proxy: str | None) -> None:
        """AIMD tăng: 429 -> giãn cách proxy x1.5 (trần 6.0s), reset chuỗi OK."""
        if not proxy or not self._static_paced():
            return
        iv = self._intervals.get(proxy, self._min_interval)
        self._intervals[proxy] = min(iv * 1.5, 6.0)
        self._ok_streaks.pop(proxy, None)

    def record_ok(self, proxy: str | None) -> None:
        """AIMD giảm: đủ 20 lần OK liên tiếp -> giãn cách x0.95 (sàn 1.5s)."""
        if not proxy or not self._static_paced():
            return
        s = self._ok_streaks.get(proxy, 0) + 1
        if s >= 20:
            iv = self._intervals.get(proxy, self._min_interval)
            self._intervals[proxy] = max(iv * 0.95, 1.5)
            s = 0
        self._ok_streaks[proxy] = s

    async def release(self, proxy: str | None) -> None:
        """Giãn cách sau khi release proxy (chỉ KÉO DÀI cooldown, không rút ngắn)."""
        if proxy is None or not self._static_paced():
            return
        async with self._lock:
            iv = self._intervals.get(proxy, self._min_interval)
            if iv <= 0:
                return
            deadline = time.monotonic() + iv
            if self._cooldowns.get(proxy, 0.0) < deadline:
                self._cooldowns[proxy] = deadline
                if proxy in self._in_ready:
                    heapq.heappush(self._cooldown_heap, (deadline, proxy))

    def revive_soft_dead(self) -> int:
        """Second-chance: hồi sinh proxy đã bị 3-strike / score<=0 để dùng lại.

        - Guard: proxyless / rotating single -> 0 (không có gì để hồi).
        - Xóa _dead + _fail_counts; rebuild _ready/_in_ready từ _proxies;
          clear _cooldown_heap (cooldown values trong _cooldowns/_rate_limited_until
          ĐƯỢC GIỮ — pick loop tự tôn trọng lại).
        - Park/AIMD cũng được reset (_park_levels/_ok_streaks/_intervals):
          pass 2 bắt đầu lại từ trạng thái sạch.
        - Scored mode: proxy bị xóa khỏi _scores được đưa lại với score 1.
        - Trả về số proxy được hồi sinh (kích thước _dead trước khi xóa).
        """
        # called between passes (workers đã dừng) — không cần lock
        if self._proxyless or self.is_single:
            return 0
        revived = len(self._dead)
        self._dead.clear()
        self._fail_counts.clear()
        self._park_levels.clear()
        self._ok_streaks.clear()
        self._intervals.clear()
        self._cooldown_heap.clear()
        self._ready = deque(p for p in self._proxies if p is not None)
        self._in_ready = set(self._ready)
        if self._scored:
            for raw in self._proxies:
                if raw and raw not in self._scores:
                    self._scores[raw] = 1
        return revived

    async def filter_working(
        self,
        timeout: float = 8.0,
        concurrency: int = 300,
        sample: int | None = None,
    ) -> list[str]:
        """Lọc nhanh proxy sống. Trả về list proxy alive (giữ thứ tự).

        Guard: rỗng/proxyless -> [].
        """
        if self._proxyless or not self._proxies:
            return []
        sem = asyncio.Semaphore(concurrency)
        pool = self._proxies[:]
        if sample is not None:
            pool = random.sample(pool, min(sample, len(pool)))
        alive: list[str] = []

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            trust_env=False,
        ) as sess:
            async def _one(p: str | None) -> str | None:
                async with sem:
                    proxy = normalize_proxy(p) if p else None
                    if p and proxy is None:
                        return None
                    try:
                        async with sess.post(
                            ENDPOINT,
                            json={"username": "discord"},
                            proxy=proxy,
                            headers=DEFAULT_HEADERS,
                        ) as resp:
                            if resp.status in (200, 201, 204, 429):
                                return proxy
                            return None
                    except Exception:
                        return None

            results = await asyncio.gather(*[_one(p) for p in pool])
        for r in results:
            if r:
                alive.append(r)
        return alive
