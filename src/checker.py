#!/usr/bin/env python3
"""Discord Username Finder v3.1 – Discord username availability checker.

Async engine, circuit-breaker for rotating proxies.
"""

from __future__ import annotations

# -- Python 3.13 SSL bug fix -----------------------------------------------
import asyncio.sslproto as _sslproto

_orig_eof = _sslproto.SSLProtocol.eof_received


def _safe_eof(self):
    try:
        return _orig_eof(self)
    except RuntimeError:
        return False


_sslproto.SSLProtocol.eof_received = _safe_eof
# --------------------------------------------------------------------------

import argparse
import asyncio
import os
import random
import sys
import time
from pathlib import Path

from src.config import (
    PROJECT_ROOT,
    DATA_DIR,
    INPUT_DIR,
    OUTPUT_DIR,
    CONFIG_FILE,
    DEFAULT_WEBHOOK_MESSAGE,
    MAX_CONCURRENCY,
    VERSION,
    AppSettings,
    Config,
    RunConfig,
    Stats,
    ensure_dir,
    ensure_file,
    filter_usernames,
    from_portable_path,
    refresh_default_headers,
)
from src.http_backend import make_backend
from src.proxy import ProxyManager
from src.engine import (
    Checker,
    CircuitBreaker,
    ThirdPartyResolver,
    WebhookSender,
    set_debug,
    dbg,
)
from src.token_pool import TokenPool
from src.ui import (
    C,
    Live,
    banner,
    console,
    final_summary,
    live_card,
)
from src.wizard import setup_wizard


# ---------------------------------------------------------------------------
# Input file helpers
# ---------------------------------------------------------------------------

def _write_names_remaining(path: Path, leftover: list[str]) -> None:
    """Ghi các tên còn lại vào chính file names (atomic: tmp + os.replace)."""
    lines = list(dict.fromkeys(leftover))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(tmp, path)


def _stagger_delay(worker_id: int, n_workers: int, interval: float) -> float:
    """Delay khởi động cho worker i: i*iv/n + jitter (0-0.3*iv) giây.

    Trả 0.0 khi không cần pace (interval <= 0 hoặc chỉ 1 worker).
    """
    if interval <= 0 or n_workers <= 1:
        return 0.0
    return worker_id * interval / n_workers + random.uniform(0.0, 0.3 * interval)


def _resolve_webhook(config: Config) -> tuple[str | None, str | None]:
    """Lấy (url, message) webhook: tự bật khi URL hợp lệ; thiếu/lỗi -> (None, None).

    Message luôn là DEFAULT_WEBHOOK_MESSAGE (mọi key legacy bị bỏ qua).
    """
    try:
        url = (config.get("webhook") or "").strip()
        if not url:
            return None, None
        return url, DEFAULT_WEBHOOK_MESSAGE
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> AppSettings:
    parser = argparse.ArgumentParser(
        description="Discord Username Finder – Discord username availability checker",
    )
    parser.add_argument(
        "-d", "--debug",
        action="store_true",
        help="Enable debug output (request/response logs)",
    )
    parser.add_argument(
        "-n", "--no-wizard",
        action="store_true",
        help="Skip setup wizard – use saved config and last proxies/usernames",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Discord Username Finder v{VERSION}",
    )
    args = parser.parse_args()
    return AppSettings(debug=args.debug, no_wizard=args.no_wizard)


# ---------------------------------------------------------------------------
# RPS calculator
# ---------------------------------------------------------------------------

async def _rps_calculator(stats: Stats, stop_event: asyncio.Event) -> None:
    """Update stats.rps and stats.checks_rps every second."""
    while not stop_event.is_set():
        prev_req = stats.requests
        prev_done = stats.works + stats.taken
        await asyncio.sleep(1)
        await stats.set_rps(float(stats.requests - prev_req))
        await stats.set_checks_rps(float(stats.works + stats.taken - prev_done))


# ---------------------------------------------------------------------------
# Non-blocking hits writer
# ---------------------------------------------------------------------------

class _HitsWriter:
    """Ghi hits.txt không block event loop: worker enqueue, 1 task drain + flush."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._warned = False

    def _warn_write_error(self, exc: Exception) -> None:
        """In cảnh báo lỗi ghi đúng 1 lần cho mỗi instance (best-effort, không raise)."""
        if self._warned:
            return
        self._warned = True
        console.print(
            f"[{C.WARNING}]! Không ghi được data/output/hits.txt — hit có thể chưa lưu: {exc}[/]"
        )

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def enqueue(self, name: str) -> None:
        # Không IO, không block – an toàn gọi từ worker
        self._queue.put_nowait(name)

    async def _run(self) -> None:
        while True:
            name = await self._queue.get()
            batch = [name]
            # Drain queue thành batch để giảm số lần write/flush
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                self._fh.write("".join(f"{n}\n" for n in batch))
                self._fh.flush()
            except Exception as exc:
                self._warn_write_error(exc)

    def flush_sync(self) -> None:
        # Drain + flush + close; chạy được cả khi event loop bị cancel (idempotent)
        if self._closed:
            return
        self._closed = True
        pending: list[str] = []
        while True:
            try:
                pending.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        try:
            if pending:
                self._fh.write("".join(f"{n}\n" for n in pending))
            self._fh.flush()
        except Exception as exc:
            self._warn_write_error(exc)
        try:
            self._fh.close()
        except Exception as exc:
            self._warn_write_error(exc)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.flush_sync()


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def _run_checker(
    cfg: RunConfig,
    settings: AppSettings,
    names_file: str | None = None,
    *,
    config: Config | None = None,
) -> None:
    """Core async checker with live display."""
    _cfg = config if config is not None else Config()
    # File names nguồn: ghi lại tên chưa check xong vào chính file này khi kết thúc
    names_path = Path(names_file) if names_file else INPUT_DIR / "names_to_check.txt"

    hits_writer = _HitsWriter(OUTPUT_DIR / "hits.txt")
    start_time = time.time()

    # Bọc toàn bộ để đảm bảo backend/file luôn được cleanup kể cả khi lỗi
    backend = None
    stats = None
    try:
        pm = ProxyManager(cfg.proxies, remove_on_fail=cfg.remove_bad_proxies, scored=cfg.scraped)
        stats = Stats()
        proxyless = not cfg.proxies

        # Safety-cap concurrency (saved configs or --no-wizard could hold old values)
        if cfg.concurrency > MAX_CONCURRENCY:
            cfg.concurrency = MAX_CONCURRENCY

        # Bump file-descriptor limit (macOS defaults to 256)
        try:
            import resource as _resource
            _soft, _hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
            _resource.setrlimit(_resource.RLIMIT_NOFILE, (_hard if _hard > 1024 else 4096, _hard))
        except Exception:
            pass

        # Circuit breaker cho rotating proxies
        cb: CircuitBreaker | None = None
        paused = False
        use_cb = pm.is_single and not pm.is_proxyless
        if use_cb:
            async def _on_circuit_open():
                nonlocal paused
                paused = True
                await stats.inc_circuit_open()
                await asyncio.sleep(2.0)
                paused = False
            cb = CircuitBreaker(threshold=10, window=2.0, cooldown=2.0, on_open=_on_circuit_open)

        http_timeout = 5 if proxyless else cfg.timeout
        mode = ("scraped" if cfg.scraped else
                "proxyless" if proxyless else
                "rotating" if pm.is_single else "static")
        backend = make_backend(
            user_timeout=http_timeout,
            concurrency=cfg.concurrency,
            mode=mode,
            use_fingerprint=_cfg.get("use_fingerprint", True),
        )

        # Token mode (opt-in): nạp + validate pool; rỗng/chết hết -> unauthed.
        token_pool = None
        if _cfg.get("use_tokens", False):
            try:
                tp = TokenPool.from_file(INPUT_DIR / "tokens.txt")
                if tp.total:
                    alive, dead = await tp.validate()
                    console.print(f"[{C.MUTED}]Đã kiểm tra token: {alive} sống / {dead} chết[/]")
                    if alive:
                        token_pool = tp
                else:
                    console.print(
                        f"[{C.MUTED}]Đã bật token nhưng tokens.txt trống — chạy không token.[/]"
                    )
            except Exception:
                token_pool = None

        # Third-party fallback cho pass 2 (skip hoàn toàn khi URL không hợp lệ).
        resolver = ThirdPartyResolver.from_config(_cfg.get("third_party_api_url", ""))

        checker = Checker(pm, backend, timeout=http_timeout, scraped=cfg.scraped, circuit_breaker=cb, stats=stats, token_pool=token_pool)

        # Webhook sender
        webhook: WebhookSender | None = None
        if cfg.webhook_url and cfg.webhook_message:
            webhook = WebhookSender(cfg.webhook_url, cfg.webhook_message, start_time)

        # Atomic index into the username list
        names = list(cfg.usernames)
        total = len(names)
        _idx_lock = asyncio.Lock()
        _next_idx = 0

        async def _next_task() -> tuple[int, str] | None:
            nonlocal _next_idx
            async with _idx_lock:
                if _next_idx >= total:
                    return None
                i = _next_idx
                _next_idx += 1
                in_flight.add(names[i])  # ngay khi bốc: Ctrl+C vẫn cứu được
            return i, names[i]

        recent_hits: list[str] = []
        request_log: list[str] = []
        # Micro-opt: bound feed để quét 100k+ usernames không phình RAM
        # (live UI chỉ render ≤8 mục cuối).
        _MAX_FEED = 200
        # Second-chance: username bị ERROR (?)/EXHAUSTED được thử lại đúng 1 vòng cuối
        retry_names: list[str] = []
        unchecked_names: list[str] = []
        second_pass = False
        # Tên đang được worker check dở – Ctrl+C vẫn phải cứu được
        in_flight: set[str] = set()

        # Telemetry park: cứ ~50 kết quả xử lý xong thì báo nếu số proxy park đổi
        _park_tick = 0
        _park_last = -1

        def _maybe_report_parked() -> None:
            """Cứ ~50 kết quả thì báo 1 lần nếu số proxy đang bị park (429) thay đổi."""
            nonlocal _park_tick, _park_last
            _park_tick += 1
            if _park_tick % 50 != 0:
                return
            try:
                n = pm.parked_count
            except Exception:
                return
            if n > 0 and n != _park_last:
                _park_last = n
                console.print(f"[{C.MUTED}]Tạm nghỉ: {n} proxy (429)[/]")

        def _finalize_flush() -> None:
            """SYNC – ghi tên chưa check xong trở lại chính file names + hits; chạy được cả khi bị cancel."""
            try:
                leftover = list(dict.fromkeys(
                    unchecked_names + retry_names + list(in_flight) + names[_next_idx:total]
                ))
                if leftover:
                    _write_names_remaining(names_path, leftover)
                    console.print(
                        f"[{C.WARNING}]Còn {len(leftover)} tên chưa check xong (đã giữ lại trong {names_path} để chạy tiếp)[/]"
                    )
                else:
                    _write_names_remaining(names_path, [])
                    console.print(f"[{C.SUCCESS}]Đã check xong toàn bộ danh sách ✔[/]")
            except Exception:
                pass
            try:
                hits_writer.flush_sync()
            except Exception:
                pass

        def _ts() -> str:
            try:
                return time.strftime("%H:%M:%S")
            except Exception:
                return "00:00:00"

        # Worker
        async def _worker(worker_id: int, n_workers: int, stagger: bool = True) -> None:
            # Stagger khởi động: tránh dội request đồng loạt lúc start
            delay = _stagger_delay(worker_id, n_workers, pm.min_interval) if stagger else 0.0
            if delay > 0:
                await asyncio.sleep(delay)
            while True:
                t = await _next_task()
                if t is None:
                    return
                idx, name = t

                try:
                    result, data, code = await checker.check(name)

                    if result is True:
                        await stats.inc_works()
                        hits_writer.enqueue(name)  # non-blocking, không IO
                        recent_hits.append(name)
                        if len(recent_hits) > _MAX_FEED:
                            del recent_hits[: len(recent_hits) - _MAX_FEED]
                        request_log.append(f"[{_ts()}] + {name} available")
                        if len(request_log) > _MAX_FEED:
                            del request_log[: len(request_log) - _MAX_FEED]
                        if webhook:
                            webhook.enqueue(name)

                    elif result is False:
                        await stats.inc_taken()
                        request_log.append(f"[{_ts()}] - {name} taken")
                        if len(request_log) > _MAX_FEED:
                            del request_log[: len(request_log) - _MAX_FEED]

                    elif result == "EXHAUSTED":
                        # Pass 1: exhausted cũng được second-chance sau khi hồi sinh proxy
                        if second_pass:
                            unchecked_names.append(name)
                            request_log.append(f"[{_ts()}] ? {name} vẫn hết proxy")
                        else:
                            retry_names.append(name)
                            request_log.append(f"[{_ts()}] ! {name} exhausted – second-chance sau")
                        if len(request_log) > _MAX_FEED:
                            del request_log[: len(request_log) - _MAX_FEED]

                    else:
                        # "ERROR" (?) -> đưa vào hàng đợi second-chance (1 vòng cuối)
                        if second_pass:
                            unchecked_names.append(name)
                            request_log.append(f"[{_ts()}] ? {name} unchecked")
                        else:
                            retry_names.append(name)
                            request_log.append(f"[{_ts()}] ? {name} retrying")
                        if len(request_log) > _MAX_FEED:
                            del request_log[: len(request_log) - _MAX_FEED]

                    in_flight.discard(name)
                    _maybe_report_parked()

                    if proxyless:
                        await asyncio.sleep(cfg.timeout)

                except asyncio.CancelledError:
                    # Cancel giữa chừng: giữ name trong in_flight để _finalize_flush cứu
                    raise
                except Exception:
                    # Lỗi ngoài luồng -> vẫn cứu ở vòng second-chance
                    if second_pass:
                        unchecked_names.append(name)
                    else:
                        retry_names.append(name)
                    request_log.append(f"[{_ts()}] !! {name} error")
                    if len(request_log) > _MAX_FEED:
                        del request_log[: len(request_log) - _MAX_FEED]
                    in_flight.discard(name)
                    continue

        # Start background tasks
        stop_rps = asyncio.Event()
        hits_writer.start()
        rps_task = asyncio.create_task(_rps_calculator(stats, stop_rps))
        webhook_task = asyncio.create_task(webhook.run()) if webhook else None

        # Create worker tasks
        pending: set[asyncio.Task] = set()

        # Build live display function
        _done_base = 0  # offset để pass 2 hiển thị tiến độ riêng

        def _live_render():
            return live_card(
                done=stats.works + stats.taken - _done_base,
                total=total,
                works=stats.works,
                taken=stats.taken,
                requests=stats.requests,
                ratelimited=stats.ratelimited,
                circuit_opens=stats.circuit_opens,
                rps=stats.rps,
                checks_rps=stats.checks_rps,
                elapsed=time.time() - start_time,
                proxy_alive=pm.alive_count,
                paused=paused,
                recent=recent_hits,
                feed=request_log,
                concurrency=cfg.concurrency,
                timeout=cfg.timeout,
                scraped=cfg.scraped,
                # Đếm lỗi theo loại hiển thị trên live card
                error_counts=checker._err.get_counts_sync(),
            )

        async def _run_pass(conc: int, stagger: bool = True) -> None:
            """Chạy 1 vòng worker + live display cho (names, total) hiện tại."""
            nonlocal _next_idx, pending
            pending = {
                asyncio.create_task(_worker(i, conc, stagger=stagger)) for i in range(conc)
            }
            with Live(_live_render(), refresh_per_second=4, console=console) as live:
                while pending:
                    await asyncio.sleep(0.3)

                    # Remove completed tasks
                    pending = {w for w in pending if not w.done()}

                    # Refill workers if any crashed
                    while _next_idx < total and len(pending) < conc:
                        pending.add(asyncio.create_task(_worker(0, conc, stagger=False)))

                    if not pending and _next_idx >= total:
                        break

                    live.update(_live_render())

        console.print()
        try:
            await _run_pass(cfg.concurrency)
            # Second-chance pass: thử lại đúng 1 vòng cho các username bị (?)/exhausted
            # Khử trùng lặp để mỗi name chỉ quét lại 1 lần duy nhất.
            if retry_names:
                second = list(dict.fromkeys(retry_names))
                retry_names = []
                revived = 0
                try:
                    revived = pm.revive_soft_dead()
                except Exception:
                    pass
                names = second
                total = len(names)
                _next_idx = 0
                _done_base = stats.works + stats.taken
                second_pass = True
                console.print(
                    f"[{C.WARNING}]Thử lại lần hai: {total} tên (hồi sinh {revived} proxy)...[/]"
                )
                await _run_pass(max(2, cfg.concurrency // 2), stagger=False)

                # Third-party fallback: chốt nốt tên pass 2 vẫn dở (nếu bật).
                if resolver is not None:
                    leftover = list(dict.fromkeys(
                        unchecked_names + retry_names + list(in_flight) + names[_next_idx:total]
                    ))
                    if leftover:
                        resolved_n = 0
                        remaining: list[str] = []
                        sem = asyncio.Semaphore(5)

                        async def _resolve_one(name: str) -> tuple[str, bool | None]:
                            async with sem:
                                await stats.inc_requests()
                                try:
                                    got = await resolver.resolve(name)
                                except asyncio.CancelledError:
                                    raise
                                except Exception:
                                    got = None
                            return name, got

                        results = await asyncio.gather(
                            *(_resolve_one(n) for n in leftover)
                        )
                        for name, got in results:
                            if got is True:
                                # Mirror đúng đường ghi hit AVAILABLE của _worker
                                await stats.inc_works()
                                hits_writer.enqueue(name)
                                recent_hits.append(name)
                                if len(recent_hits) > _MAX_FEED:
                                    del recent_hits[: len(recent_hits) - _MAX_FEED]
                                request_log.append(f"[{_ts()}] + {name} available")
                                if len(request_log) > _MAX_FEED:
                                    del request_log[: len(request_log) - _MAX_FEED]
                                if webhook:
                                    webhook.enqueue(name)
                                resolved_n += 1
                            elif got is False:
                                await stats.inc_taken()
                                request_log.append(f"[{_ts()}] - {name} taken")
                                if len(request_log) > _MAX_FEED:
                                    del request_log[: len(request_log) - _MAX_FEED]
                                resolved_n += 1
                            else:
                                remaining.append(name)
                        unchecked_names = remaining
                        console.print(
                            f"[{C.MUTED}]Đã chốt thêm {resolved_n}/{len(leftover)} tên qua API bên thứ ba[/]"
                        )
        except asyncio.CancelledError:
            pass
        finally:
            # SYNC trước mọi await: hits + tên đang dở phải được ghi kể cả khi Ctrl+C
            _finalize_flush()
            stop_rps.set()
            rps_task.cancel()
            for w in pending:
                w.cancel()
            if webhook_task:
                webhook_task.cancel()
                try:
                    await webhook_task
                except asyncio.CancelledError:
                    pass
                # run() đã tự flush buffer cuối trong except CancelledError;
                # gọi close() thêm lần nữa để chắc chắn không sót hit.
                if webhook is not None:
                    try:
                        await webhook.close()
                    except Exception:
                        pass
            if backend is not None:
                try:
                    await backend.close()
                except Exception:
                    pass
                backend = None
            if resolver is not None:
                try:
                    await resolver.close()
                except Exception:
                    pass
            await asyncio.sleep(0)

        # Final summary
        elapsed = time.time() - start_time
        snap = await stats.snapshot()

        final_summary(
            requests=snap["requests"],
            works=snap["works"],
            taken=snap["taken"],
            ratelimited=snap["ratelimited"],
            circuit_opens=snap["circuit_opens"],
            elapsed=elapsed,
            peak_rps=snap["peak_rps"],
            best_streak=snap["best_streak"],
        )

    except Exception:
        # Đảm bảo file/backend đóng ngay cả khi lỗi
        try:
            _finalize_flush()
        except Exception:
            # _finalize_flush có thể chưa tồn tại nếu lỗi trước khi setup xong
            try:
                hits_writer.flush_sync()
            except Exception:
                pass
        if backend is not None:
            try:
                await backend.close()
            except Exception:
                pass
            backend = None
        raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Application entry point."""
    settings = parse_args()

    set_debug(settings.debug)

    ensure_dir(DATA_DIR, INPUT_DIR, OUTPUT_DIR)
    ensure_file(CONFIG_FILE)
    ensure_file(INPUT_DIR / "proxies.txt")
    ensure_file(INPUT_DIR / "names_to_check.txt")

    config = Config()
    refresh_default_headers(config)

    try:
        if settings.no_wizard:
            # Quick start: load from config + saved files
            from src.config import load_lines
            console.print(banner())
            proxy_mode = config.get("proxy_mode", "")

            proxy_file = from_portable_path(
                config.get("last_proxy_file", str(INPUT_DIR / "proxies.txt"))
            )
            names_file = from_portable_path(
                config.get("last_names_file", str(INPUT_DIR / "names_to_check.txt"))
            )
            proxies = load_lines(proxy_file)
            if proxy_mode == "none":
                proxies = []  # lần wizard gần nhất chọn proxyless, tôn trọng lựa chọn đó
            usernames = load_lines(names_file)
            _raw = len(usernames)
            usernames, _removed = filter_usernames(usernames)
            if _removed:
                console.print(f"[{C.MUTED}]Đã lọc {_removed}/{_raw} tên (trùng/không hợp lệ) – còn {len(usernames)}[/]")
            if not proxies:
                console.print(f"[{C.WARNING}]Không tìm thấy proxy — chạy proxyless.[/]")
            if not usernames:
                console.print(f"[{C.DANGER}]Không có username nào — chạy wizard (bỏ --no-wizard) trước.[/]")
                sys.exit(1)
            wh_url, wh_msg = _resolve_webhook(config)
            run_config = RunConfig(
                proxies=proxies,
                remove_bad_proxies=config.get("remove_proxies", False),
                usernames=usernames,
                concurrency=config.get("concurrency", 50),
                timeout=config.get("timeout", 10),
                scraped=(proxy_mode == "scraped"),
                webhook_url=wh_url,
                webhook_message=wh_msg,
            )
            console.print(f"[{C.MUTED}]Bỏ qua wizard – {len(proxies)} proxy, {len(usernames)} tên[/]")
        else:
            run_config = asyncio.run(setup_wizard(config, settings))

        names_file = from_portable_path(
            config.get("last_names_file", str(INPUT_DIR / "names_to_check.txt"))
        )
        asyncio.run(_run_checker(run_config, settings, names_file=names_file, config=config))
    except (EOFError, KeyboardInterrupt):
        console.print(f"\n[{C.WARNING}]Aborted.[/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
