#!/usr/bin/env python3
"""Discord Username Finder v3.1 – terminal UI (Pure ANSI, zero dependencies).

Clean Minimalist Typography: no `rich`, no box-drawing characters.
Only raw ANSI escape codes + stdlib (sys, os, re, atexit, time).
"""

from __future__ import annotations

import atexit
import os
import re
import sys
import time

from src.config import C, VERSION  # noqa: F401  (C re-exported for callers)

# Enable Windows VT100 processing so ANSI codes render on CMD/PowerShell.
try:
    if os.name == "nt":
        os.system("")
except Exception:
    pass


class ANSI:
    """Standard ANSI palette + cursor/erase codes."""

    WHITE = "\033[1;97m"    # primary values: BOLD WHITE
    BLUE = "\033[1;94m"     # primary accent: BRIGHT BLUE
    CYAN = "\033[1;96m"     # labels: CYAN
    GREEN = "\033[1;92m"    # positive: GREEN
    YELLOW = "\033[1;93m"   # warning: YELLOW
    RED = "\033[1;91m"      # error: RED
    GRAY = "\033[90m"       # timestamp / secondary: DIM GRAY
    DIM = "\033[2m"
    RESET = "\033[0m"
    HIDE_CURSOR = "\033[?25l"
    SHOW_CURSOR = "\033[?25h"
    HOME = "\033[H"
    CLEAR_SCREEN = "\033[2J\033[H"
    CLEAR_LINE = "\033[K"
    CLEAR_DOWN = "\033[J"


# Bộ glyph tối giản dùng xuyên suốt UI (không emoji, mỗi dòng tối đa 1 glyph).
G_HOT = "●"    # đang chạy / mục hiện tại
G_OK = "✔"     # thành công
G_BAD = "✖"    # lỗi / thất bại
G_NEXT = "›"   # phân cấp / breadcrumb
G_WAIT = "…"   # đang chờ / tiếp diễn


def _restore_cursor() -> None:
    try:
        sys.stdout.write(ANSI.SHOW_CURSOR)
        sys.stdout.flush()
    except Exception:
        pass


atexit.register(_restore_cursor)


def _no_color() -> bool:
    return bool(os.environ.get("NO_COLOR"))


# Rich-markup tag stripper: removes [/], [#RRGGBB], [tag]/[tag=x] but keeps
# [HH:MM:SS] timestamps and never touches ANSI escapes (\x1b[K, \x1b[1;97m, ...).
_TAG_RE = re.compile(r"(?<!\x1b)\[(?:/?(?:#[0-9A-Fa-f]{3,8}|[a-zA-Z!][^\]]*)|/)\]")


def _strip_markup(text: object) -> str:
    """Remove rich-style [tag] markup; preserve [HH:MM:SS] timestamps."""
    try:
        s = str(text)
    except Exception:
        return ""
    try:
        return _TAG_RE.sub("", s)
    except Exception:
        return s


# Bảng tag markup thật trong source -> mã ANSI (C.* là mã hex).
_TAG_ANSI = {
    f"[{C.PRIMARY}]": ANSI.BLUE,
    f"[{C.SUCCESS}]": ANSI.GREEN,
    f"[{C.DANGER}]": ANSI.RED,
    f"[{C.WARNING}]": ANSI.YELLOW,
    f"[{C.MUTED}]": ANSI.GRAY,
    f"[{C.BORDER}]": ANSI.DIM,
    "[dim]": ANSI.DIM,
    "[/]": ANSI.RESET,
}


def _render_markup(text: str) -> str:
    """Render tag markup thành ANSI; NO_COLOR thì chỉ strip tag như cũ."""
    if _no_color():
        return _strip_markup(text)
    try:
        s = str(text)
    except Exception:
        return ""
    for tag, code in _TAG_ANSI.items():
        s = s.replace(tag, code)
    try:
        return _TAG_RE.sub("", s)
    except Exception:
        return s


def _paint(code: str, text: str) -> str:
    """Bọc text bằng một mã màu ANSI; NO_COLOR -> text trần (không ESC)."""
    try:
        if _no_color():
            return f"{text}"
        return f"{code}{text}{ANSI.RESET}"
    except Exception:
        return f"{text}"


def _bar(pct: float, width: int = 20) -> str:
    """Thanh tiến độ text thuần: '━' phần đã xong + '─' phần còn lại."""
    try:
        w = _safe_int(width, 20)
        if w <= 0:
            return ""
        filled = round(_safe_float(pct, 0.0) / 100.0 * w)
        filled = max(0, min(w, filled))
        return "━" * filled + "─" * (w - filled)
    except Exception:
        return "─" * 20


def _collapse(line: str) -> None:
    """Thay dòng vừa nhập bằng dòng chuẩn hóa (chỉ khi stdin/stdout là TTY)."""
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            sys.stdout.write("\x1b[1A\x1b[2K\r" + str(line) + "\n")
            sys.stdout.flush()
            return
    except Exception:
        pass
    try:
        console.print(line)
    except Exception:
        pass


def _safe_int(val: object, default: int = 0) -> int:
    try:
        if val is None:
            return default
        return int(val)  # type: ignore[arg-type]
    except Exception:
        try:
            return int(float(val))  # type: ignore[arg-type]
        except Exception:
            return default


def _safe_float(val: object, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        return float(val)  # type: ignore[arg-type]
    except Exception:
        return default


def _safe_str(val: object, default: str = "N/A") -> str:
    try:
        if val is None:
            return default
        s = str(val)
        return s if s else default
    except Exception:
        return default


def _fmt_int(val: object) -> str:
    return f"{_safe_int(val):,}"


def _fmt_uptime(seconds: object) -> str:
    try:
        s = max(0, int(float(seconds)))  # type: ignore[arg-type]
    except Exception:
        s = 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _row(label: str, value: str, value_color: str = ANSI.WHITE) -> str:
    """One metric line: CYAN label ljust(18), GRAY colon, colored value (NO_COLOR -> plain)."""
    try:
        padded = f"  {label.ljust(18)}"
        if _no_color():
            return f"{padded}: {value}{ANSI.CLEAR_LINE}"
        return (
            f"{ANSI.CYAN}{padded}{ANSI.GRAY}: "
            f"{value_color}{value}{ANSI.RESET}{ANSI.CLEAR_LINE}"
        )
    except Exception:
        return f"  {label}: {value}"


# ---------------------------------------------------------------------------
# Minimal console object (drop-in for rich Console usage in wizard/checker)
# ---------------------------------------------------------------------------

class _StatusContext:
    """Single-line ANSI status (spinner-free quiet line)."""

    def __init__(self, message: str = "", spinner: str | None = None) -> None:
        self.message = _render_markup(message)

    def __enter__(self) -> "_StatusContext":
        try:
            sys.stdout.write(self.message + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        return False


class AnsiConsole:
    """Minimal console with print/clear/status (plain stdout)."""

    def print(self, *args: object, **kwargs: object) -> None:
        try:
            text = " ".join(_render_markup(a) for a in args) if args else ""
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        except Exception:
            pass

    def clear(self) -> None:
        try:
            sys.stdout.write(ANSI.CLEAR_SCREEN)
            sys.stdout.flush()
        except Exception:
            pass

    def status(self, message: str = "", spinner: str | None = None) -> _StatusContext:
        return _StatusContext(message, spinner)


console = AnsiConsole()


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def banner() -> str:
    """Banner tối giản: tên trắng + version xám, đúng 1 space trước v."""
    try:
        return (
            f"  {_paint(ANSI.WHITE, 'Discord Username Finder')}"
            f" {_paint(ANSI.GRAY, f'v{VERSION}')}"
        )
    except Exception:
        return "  Discord Username Finder"


# ---------------------------------------------------------------------------
# Progress indicator
# ---------------------------------------------------------------------------

def progress_steps(current: int = 0, total: int = 4) -> str:
    """Tiến độ 4 bước: xong = ✔ xanh, hiện tại = ● cyan, chờ = … xám."""
    labels = ["Proxies", "Usernames", "Speed", "Webhook"]
    try:
        cur = _safe_int(current, 0)
        parts: list[str] = []
        for i, label in enumerate(labels):
            if i < cur:
                parts.append(_paint(ANSI.GREEN, f"{G_OK} {label}"))
            elif i == cur:
                parts.append(_paint(ANSI.CYAN, f"{G_HOT} {label}"))
            else:
                parts.append(_paint(ANSI.GRAY, f"{G_WAIT} {label}"))
        return "  " + "  ".join(parts)
    except Exception:
        return "  " + "  ".join(labels)


# ---------------------------------------------------------------------------
# Cards (plain, no boxes)
# ---------------------------------------------------------------------------

def info_card(*lines: str) -> None:
    """Print informational lines (gray)."""
    try:
        for ln in lines:
            console.print(f"  {_paint(ANSI.GRAY, _render_markup(ln))}")
    except Exception:
        pass


def warn_card(*lines: str) -> None:
    """Print warning lines (yellow ! prefix)."""
    try:
        for ln in lines:
            console.print(f"  {_paint(ANSI.YELLOW, '! ' + _render_markup(ln))}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Summary (config)
# ---------------------------------------------------------------------------

def config_summary(
    proxy_count: int = 0,
    scraped: bool = False,
    remove_bad: bool = False,
    username_count: int = 0,
    concurrency: int = 0,
    timeout: int = 0,
    webhook: bool = False,
) -> None:
    """In tóm tắt cấu hình trước khi chạy (plain ljust rows)."""
    try:
        console.print()
        console.print(
            f"  {_paint(ANSI.WHITE, f'Ready to check {_fmt_int(username_count)} usernames')}"
        )
        console.print()
        n = _safe_int(proxy_count)
        if scraped:
            proxy_txt = f"{_fmt_int(n)} free proxies (scraped)"
        elif n > 0:
            proxy_txt = f"{_fmt_int(n)} static proxies"
        else:
            proxy_txt = "none (proxyless)"
        console.print(_row("Proxies", proxy_txt))
        if n > 1 and not scraped:
            console.print(_row("Auto-remove", "Yes" if remove_bad else "No"))
        console.print(_row("Workers", f"{_fmt_int(concurrency)} concurrent"))
        console.print(_row("Timeout", f"{_safe_int(timeout)}s per request"))
        console.print(_row("Webhook", "active (Discord)" if webhook else "off",
                           ANSI.GREEN if webhook else ANSI.GRAY))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Live checker renderer -> single string (4 vertical blocks)
# ---------------------------------------------------------------------------

def _format_feed_line(raw: object) -> str:
    """Colorize one feed line by its leading glyph; timestamp gray."""
    try:
        line = _strip_markup(raw).strip()
    except Exception:
        return ""
    if not line:
        return ""
    m = re.match(r"^\[(\d{2}:\d{2}:\d{2})\]\s*(.*)$", line)
    if m:
        ts, rest = m.group(1), m.group(2).strip()
        prefix = f"{_paint(ANSI.GRAY, '[' + ts + ']')}  "
    else:
        rest = line
        prefix = "  "
    try:
        if rest.startswith("+"):
            body = rest[1:].strip()
            return f"{prefix}{_paint(ANSI.GREEN, f'{G_OK}  {body}')}{ANSI.CLEAR_LINE}"
        if rest.startswith("-"):
            body = rest[1:].strip()
            return f"{prefix}{_paint(ANSI.RED, f'{G_BAD}  {body}')}{ANSI.CLEAR_LINE}"
        if rest.startswith("!!"):
            body = rest[2:].strip()
            return f"{prefix}{_paint(ANSI.RED, f'{G_BAD}  {body}')}{ANSI.CLEAR_LINE}"
        if rest.startswith("!"):
            body = rest[1:].strip()
            return f"{prefix}{_paint(ANSI.YELLOW, f'!  {body}')}{ANSI.CLEAR_LINE}"
        if rest.startswith("?"):
            body = rest[1:].strip() or rest
            return f"{prefix}{_paint(ANSI.YELLOW, f'{G_WAIT}  {body}')}{ANSI.CLEAR_LINE}"
        if "available" in rest.lower():
            return f"{prefix}{_paint(ANSI.GREEN, rest)}{ANSI.CLEAR_LINE}"
        if "taken" in rest.lower():
            return f"{prefix}{_paint(ANSI.RED, rest)}{ANSI.CLEAR_LINE}"
        return f"{prefix}{_paint(ANSI.GRAY, rest)}{ANSI.CLEAR_LINE}"
    except Exception:
        return f"{prefix}{rest}"


def live_card(
    done: int = 0,
    total: int = 0,
    works: int = 0,
    taken: int = 0,
    requests: int = 0,
    ratelimited: int = 0,
    circuit_opens: int = 0,
    rps: float = 0.0,
    elapsed: float = 0.0,
    proxy_alive: int = 0,
    checks_rps: float = 0.0,
    paused: bool = False,
    recent: list[str] | None = None,
    feed: list[str] | None = None,
    error_counts: dict | None = None,
    concurrency: int = 0,
    timeout: int = 0,
    scraped: bool = False,
) -> str:
    """Build the live display as ONE plain-ANSI string (4 vertical blocks).

    Crash-proof: any bad input falls back to a minimal header line.
    """
    try:
        done_i = _safe_int(done)
        total_i = _safe_int(total)
        works_i = _safe_int(works)
        taken_i = _safe_int(taken)
        req_i = _safe_int(requests)
        rl_i = _safe_int(ratelimited)
        cb_i = _safe_int(circuit_opens)
        rps_f = _safe_float(rps)
        crps_f = _safe_float(checks_rps)
        elapsed_f = _safe_float(elapsed)
        alive_i = _safe_int(proxy_alive)

        try:
            pct = (done_i / max(total_i, 1) * 100.0) if total_i else 0.0
        except Exception:
            pct = 0.0

        if paused:
            status_txt = _paint(ANSI.YELLOW, f"{G_HOT} PAUSED")
        elif rl_i > 0 and done_i == 0:
            status_txt = _paint(ANSI.YELLOW, f"{G_HOT} RATE LIMITED")
        else:
            status_txt = _paint(ANSI.GREEN, f"{G_HOT} RUNNING")

        lines: list[str] = []

        # -- Block 1: Header & Status --
        title = f"Discord Username Finder v{VERSION}"
        lines.append(
            f"  {_paint(ANSI.WHITE, title)}"
            f"  {status_txt}"
            f"  {_paint(ANSI.GRAY, _fmt_uptime(elapsed_f))}"
            f"{ANSI.CLEAR_LINE}"
        )
        lines.append(f"{ANSI.CLEAR_LINE}")

        # -- Block 2: Core Identity (1 dòng dim, phân cấp bằng ›) --
        try:
            if scraped:
                mode_txt = f"Free Scraped ({alive_i} alive)"
            elif alive_i > 0:
                mode_txt = f"Static Pool ({alive_i} alive)"
            else:
                mode_txt = "Proxyless (direct)"
        except Exception:
            mode_txt = "N/A"
        try:
            conc_i = _safe_int(concurrency)
            to_i = _safe_int(timeout)
        except Exception:
            conc_i, to_i = 0, 0
        segments = ["discord.com/api/v9/unique-username", mode_txt]
        if conc_i:
            segments.append(f"{conc_i} workers (timeout {to_i}s)")
        sep = f"  {_paint(ANSI.GRAY, G_NEXT)}  "
        dimmed = sep.join(_paint(ANSI.DIM, s) for s in segments)
        lines.append(f"  {dimmed}{ANSI.CLEAR_LINE}")
        lines.append(f"{ANSI.CLEAR_LINE}")

        # -- Block 3: Live Metrics --
        lines.append(_row("Progress", f"{_bar(pct)} {pct:.1f}%  ({done_i:,} / {total_i:,})"))
        try:
            avail_color = ANSI.GREEN if works_i > 0 else ANSI.WHITE
        except Exception:
            avail_color = ANSI.WHITE
        lines.append(_row("Available Hits", f"{works_i:,}", avail_color))
        lines.append(_row("Taken / Invalid", f"{taken_i:,}"))
        lines.append(_row("Total Requests", f"{req_i:,}"))
        lines.append(_row("Speed", f"{rps_f:.1f} req/s  {G_NEXT}  {crps_f:.1f} checks/s"))
        lines.append(_row("Rate Limited", f"{rl_i:,}",
                          ANSI.YELLOW if rl_i > 0 else ANSI.WHITE))
        lines.append(_row("Circuit Breaks", f"{cb_i:,}"))
        # -- Đếm lỗi theo loại (cf_ban_park, 429_park, ...) nếu có --
        top_errors: list[tuple[str, int]] = []
        try:
            for key, value in dict(error_counts or {}).items():
                try:
                    count = int(value or 0)
                except (TypeError, ValueError):
                    continue
                if count > 0:
                    top_errors.append((str(key), count))
        except Exception:
            top_errors = []
        if top_errors:
            top_errors.sort(key=lambda kv: (-kv[1], kv[0]))
            err_txt = f"  {G_NEXT}  ".join(f"{k} {v:,}" for k, v in top_errors[:3])
            lines.append(_row("Errors", err_txt, ANSI.YELLOW))
        recent_list: list[str] = []
        try:
            recent_list = [str(n) for n in (recent or []) if n]
            recent_txt = ", ".join(recent_list[-6:]) if recent_list else "none"
        except Exception:
            recent_txt = "N/A"
        lines.append(_row("Recent Hits", recent_txt,
                          ANSI.GREEN if recent_list else ANSI.GRAY))
        lines.append(f"{ANSI.CLEAR_LINE}")

        # -- Block 4: Recent Activity (last 5 feed lines) --
        try:
            feed_list = list(feed or [])[-5:]
        except Exception:
            feed_list = []
        if feed_list:
            for raw in feed_list:
                try:
                    lines.append(_format_feed_line(raw))
                except Exception:
                    continue
        else:
            lines.append(f"  {_paint(ANSI.GRAY, 'waiting for results…')}{ANSI.CLEAR_LINE}")
        lines.append(f"{ANSI.CLEAR_LINE}")
        lines.append(f"  {_paint(ANSI.GRAY, '[Ctrl+C to stop and save progress]')}{ANSI.CLEAR_LINE}")

        return "\n".join(lines)
    except Exception:
        try:
            return (
                f"  {_paint(ANSI.WHITE, 'Discord Username Finder v' + VERSION)}"
                f"  |  Status: {_paint(ANSI.GREEN, 'RUNNING')}{ANSI.CLEAR_LINE}"
            )
        except Exception:
            return "  Discord Username Finder"


# ---------------------------------------------------------------------------
# Zero-flicker live renderer (replaces rich.live.Live)
# ---------------------------------------------------------------------------

class Live:
    """Single-string ANSI buffer renderer with cursor hide/restore."""

    def __init__(self, initial: str = "", refresh_per_second: int = 4,
                 console: object | None = None) -> None:
        self._buf = initial if isinstance(initial, str) else ""

    def __enter__(self) -> "Live":
        try:
            sys.stdout.write(ANSI.HIDE_CURSOR + ANSI.CLEAR_SCREEN)
            if self._buf:
                sys.stdout.write(ANSI.HOME + str(self._buf) + ANSI.CLEAR_DOWN)
            sys.stdout.flush()
        except Exception:
            pass
        return self

    def update(self, renderable: object) -> None:
        try:
            output = renderable if isinstance(renderable, str) else str(renderable)
            sys.stdout.write("\033[H" + output + "\033[J")
            sys.stdout.flush()
        except Exception:
            pass

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        try:
            sys.stdout.write(ANSI.SHOW_CURSOR + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------

def final_summary(
    requests: int = 0,
    works: int = 0,
    taken: int = 0,
    ratelimited: int = 0,
    circuit_opens: int = 0,
    elapsed: float = 0.0,
    peak_rps: float = 0.0,
    best_streak: int = 0,
) -> None:
    """Print the final results (plain ljust rows)."""
    try:
        req_i = _safe_int(requests)
        works_i = _safe_int(works)
        taken_i = _safe_int(taken)
        rl_i = _safe_int(ratelimited)
        cb_i = _safe_int(circuit_opens)
        elapsed_f = _safe_float(elapsed)
        try:
            avg_rps = req_i / max(elapsed_f, 0.1)
        except Exception:
            avg_rps = 0.0
        console.print()
        console.print(
            f"  {_paint(ANSI.GREEN, f'{G_OK} Done')}"
            f"  {_paint(ANSI.GRAY, f'{elapsed_f:.0f}s')}"
        )
        console.print()
        console.print(_row("Available", f"{works_i:,}",
                           ANSI.GREEN if works_i > 0 else ANSI.WHITE))
        console.print(_row("Taken", f"{taken_i:,}", ANSI.RED))
        console.print(_row("Requests", f"{req_i:,}"))
        if rl_i > 0:
            console.print(_row("Rate limited", f"{rl_i:,}", ANSI.YELLOW))
        if cb_i > 0:
            console.print(_row("Circuit breaks", f"{cb_i:,}"))
        console.print(_row("Elapsed", f"{elapsed_f:.0f}s"))
        console.print(_row("Avg req/s", f"{avg_rps:.1f}"))
        if _safe_float(peak_rps) > 0:
            console.print(_row("Peak req/s", f"{_safe_float(peak_rps):.0f}"))
        if _safe_int(best_streak) > 1:
            console.print(_row("Best streak", f"{_safe_int(best_streak)} hits", ANSI.GREEN))
        if works_i > 0:
            console.print(_row("Saved to", "data/output/hits.txt"))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Prompts (pure stdlib replacements for rich.prompt)
# ---------------------------------------------------------------------------

class Prompt:
    """Plain input() prompt với choices/default/options (menu chọn)."""

    @staticmethod
    def ask(prompt: str = "", choices: list[str] | None = None,
            default: str = "", options: list[tuple] | None = None) -> str:
        try:
            label = _strip_markup(prompt).strip()
        except Exception:
            label = ""
        try:
            opts = list(options or [])
            if opts:
                keys: list[str] = []
                labels_by_key: dict[str, str] = {}
                console.print(f"  {G_HOT} {label}")
                for opt in opts:
                    try:
                        key_s = str(opt[0])
                        opt_label = str(opt[1])
                        desc = _paint(ANSI.GRAY, str(opt[2]))
                    except Exception:
                        continue
                    keys.append(key_s)
                    labels_by_key[key_s] = opt_label
                    console.print(f"    {key_s}  {opt_label.ljust(9)}{desc}")
                if keys:
                    # Nổi default trong prompt chỉ khi default là key hợp lệ của menu.
                    if default in labels_by_key:
                        menu_prompt = f"  › [default: {labels_by_key[default]}] "
                    else:
                        menu_prompt = "  › "
                    while True:
                        try:
                            raw = input(menu_prompt).strip()
                        except (EOFError, KeyboardInterrupt):
                            raise
                        if raw == "" and default in labels_by_key:
                            canon = f"  {G_OK} {label} › {labels_by_key[default]}"
                            _collapse(canon)
                            return default
                        if raw in labels_by_key:
                            canon = f"  {G_OK} {label} › {labels_by_key[raw]}"
                            _collapse(canon)
                            return raw
                        console.print(
                            f"  {_paint(ANSI.YELLOW, 'Please choose one of: ' + ', '.join(keys))}"
                        )
            if choices:
                shown = "/".join(str(c) for c in choices)
                # Nối "(default: x)" sau danh sách choices khi default hợp lệ.
                if default in choices:
                    prompt_c = f"  {G_HOT} {label} [{shown}] (default: {default}) › "
                else:
                    prompt_c = f"  {G_HOT} {label} [{shown}] › "
                while True:
                    try:
                        raw = input(prompt_c).strip()
                    except (EOFError, KeyboardInterrupt):
                        raise
                    if raw == "" and default in choices:
                        canon = f"  {G_OK} {label} › {default}"
                        _collapse(canon)
                        return default
                    if raw in choices:
                        canon = f"  {G_OK} {label} › {raw}"
                        _collapse(canon)
                        return raw
                    console.print(
                        f"  {_paint(ANSI.YELLOW, 'Please choose one of: ' + ', '.join(str(c) for c in choices))}"
                    )
            # Nhánh text tự do: default khác rỗng thì hiện ngay trong prompt.
            if isinstance(default, str) and default:
                prompt_t = f"  {G_HOT} {label} [default: {default}] › "
            else:
                prompt_t = f"  {G_HOT} {label} › "
            while True:
                try:
                    raw = input(prompt_t).strip()
                except (EOFError, KeyboardInterrupt):
                    raise
                value = raw if raw else default
                canon = f"  {G_HOT} {label} › {value}"
                _collapse(canon)
                return value
        except (EOFError, KeyboardInterrupt):
            raise
        except Exception:
            return default


class IntPrompt:
    """Plain integer prompt với default + choices, chuẩn hóa 1 dòng."""

    @staticmethod
    def ask(prompt: str = "", default: int = 0,
            choices: list[str] | None = None) -> int:
        try:
            label = _strip_markup(prompt).strip()
        except Exception:
            label = ""
        dflt = _safe_int(default)
        try:
            # Hiển thị default rõ ràng: có choices thì kèm danh sách, không thì chỉ default.
            if choices:
                shown_c = "/".join(str(c) for c in choices)
                prompt_line = f"  {G_HOT} {label} [{shown_c}] (default: {dflt}) › "
            else:
                prompt_line = f"  {G_HOT} {label} [default: {dflt}] › "
            while True:
                try:
                    raw = input(prompt_line).strip()
                except (EOFError, KeyboardInterrupt):
                    raise
                if not raw:
                    val = dflt
                    break
                try:
                    val = int(raw)
                except ValueError:
                    console.print(f"  {_paint(ANSI.YELLOW, 'Please enter a number.')}")
                    continue
                if choices and str(val) not in [str(c) for c in choices]:
                    console.print(
                        f"  {_paint(ANSI.YELLOW, 'Please choose one of: ' + ', '.join(str(c) for c in choices))}"
                    )
                    continue
                break
            canon = f"  {G_HOT} {label} › {val}"
            _collapse(canon)
            return val
        except (EOFError, KeyboardInterrupt):
            raise
        except Exception:
            return dflt


class Confirm:
    """Plain y/n confirm prompt, chuẩn hóa canon dạng yes/no."""

    @staticmethod
    def ask(prompt: str = "", default: bool = False) -> bool:
        try:
            label = _strip_markup(prompt).strip()
        except Exception:
            label = ""
        try:
            hint = "Y/n" if default else "y/N"
            while True:
                try:
                    raw = input(f"  {G_HOT} {label} [{hint}] › ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    raise
                if not raw:
                    value = bool(default)
                    break
                if raw in ("y", "yes"):
                    value = True
                    break
                if raw in ("n", "no"):
                    value = False
                    break
                console.print(f"  {_paint(ANSI.YELLOW, 'Please answer y or n.')}")
            canon = f"  {G_HOT} {label} › {'yes' if value else 'no'}"
            _collapse(canon)
            return value
        except (EOFError, KeyboardInterrupt):
            raise
        except Exception:
            return bool(default)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def ok(msg: str) -> None:
    try:
        console.print(_paint(ANSI.GREEN, f"  {G_OK} {_render_markup(msg)}"))
    except Exception:
        pass


def fail(msg: str) -> None:
    """In lỗi rồi trả về ngay (non-fatal): caller tự quyết định dừng hay chạy tiếp."""
    try:
        console.print(_paint(ANSI.RED, f"  {G_BAD} {_render_markup(msg)}"))
    except Exception:
        pass


def info(msg: str) -> None:
    try:
        console.print(_paint(ANSI.GRAY, f"  {G_NEXT} {_render_markup(msg)}"))
    except Exception:
        pass
