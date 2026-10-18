#!/usr/bin/env python3
"""Discord Username Finder v3.1 – Setup wizard and proxy scraper."""

from __future__ import annotations

import asyncio
import itertools
import random
import sys
from pathlib import Path

import aiohttp

from src.config import (
    DEFAULT_WEBHOOK_MESSAGE,
    INPUT_DIR,
    MAX_CONCURRENCY,
    PROJECT_ROOT,
    USERNAME_CHARS,
    AppSettings,
    Config,
    RunConfig,
    ensure_file,
    filter_usernames,
    is_valid_username,
    load_lines,
    normalize_proxy,
    to_portable_path,
)
from src.ui import (
    C,
    Confirm,
    IntPrompt,
    Prompt,
    banner,
    config_summary,
    console,
    fail,
    info,
    info_card,
    ok,
    progress_steps,
    warn_card,
)

# Default file paths (absolute, under INPUT_DIR)
DEFAULT_PROXY_FILE = str(INPUT_DIR / "proxies.txt")
DEFAULT_NAMES_FILE = str(INPUT_DIR / "names_to_check.txt")

# Clean display versions (shown in prompts, not full absolute paths)
_PROXY_FILE_DISPLAY = "data/input/proxies.txt"
_NAMES_FILE_DISPLAY = "data/input/names_to_check.txt"


def _resolve_input_path(raw: str) -> str:
    """Resolve user input to an absolute path.

    If *raw* is relative it is resolved against PROJECT_ROOT so the
    script works regardless of the current working directory.
    """
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return str(p)


def _collect_pasted_lines() -> list[str]:
    """Thu thập proxy dán từ stdin tới dòng trống hoặc EOF."""
    lines: list[str] = []
    while True:
        line = sys.stdin.readline()
        if line == "":
            break
        line = line.strip()
        if not line:
            break
        lines.append(line)
    return lines

# NOTE: proxy-line validation lives in src.config.normalize_proxy (shared).


# ---------------------------------------------------------------------------
# Setup wizard – main entry
# ---------------------------------------------------------------------------

async def setup_wizard(config: Config, settings: AppSettings) -> RunConfig:
    """Walk the user through a clean 4-step setup."""

    console.clear()
    console.print(banner())
    console.print()
    console.print(progress_steps(0))

    # -- Step 1: Proxies --
    warn_card(
        f"[{C.WARNING}]Proxies strongly recommended[/]",
        f"[{C.MUTED}]Without them Discord will rate-limit you.[/]",
        f"[{C.MUTED}]Format: login:pass@host:port (hoặc host:port:login:pass)[/]",
    )
    proxies, remove_bad, scraped = await _step_proxies(config)

    # -- Step 2: Usernames --
    console.print()
    console.print(progress_steps(1))
    usernames = _step_usernames(config)

    # -- Step 3: Speed --
    console.print()
    console.print(progress_steps(2))
    concurrency, timeout = _step_speed(proxies, scraped)
    config.set("concurrency", concurrency)
    config.set("timeout", timeout)

    # -- Step 4: Webhook --
    console.print()
    console.print(progress_steps(3))
    webhook_url, webhook_msg = _step_webhook(config)

    config.set("remove_proxies", remove_bad)

    # -- Summary --
    config_summary(
        proxy_count=len(proxies),
        scraped=scraped,
        remove_bad=remove_bad,
        username_count=len(usernames),
        concurrency=concurrency,
        timeout=timeout,
        webhook=bool(webhook_url),
    )

    if not Confirm.ask(f"\n[{C.PRIMARY}]Start checking?[/]", default=True):
        console.print(f"[{C.MUTED}]Aborted.[/]")
        sys.exit(0)

    return RunConfig(
        proxies=proxies,
        remove_bad_proxies=remove_bad,
        usernames=usernames,
        concurrency=concurrency,
        timeout=timeout,
        scraped=scraped,
        webhook_url=webhook_url,
        webhook_message=webhook_msg,
    )


# ---------------------------------------------------------------------------
# Step 1: Proxies
# ---------------------------------------------------------------------------

async def _step_proxies(config: Config) -> tuple[list[str], bool, bool]:
    """Guide user through proxy setup. Returns (proxies, remove_bad, scraped)."""
    proxies: list[str] = []
    remove_bad = False
    scraped = False
    # Đường dẫn file proxy đã dùng trong phiên này (để persist vào config).
    source_path = ""

    # Check for proxies from last session
    existing = load_lines(DEFAULT_PROXY_FILE)
    if existing:
        console.print()
        info_card(f"Found {len(existing)} proxies from last session.")
        if Confirm.ask("Reuse them?", default=False):
            proxies = existing
            source_path = DEFAULT_PROXY_FILE
            ok(f"Reusing {len(proxies)} proxies")

    if not proxies:
        # Present all proxy sources as first-class options
        console.print()
        mode = Prompt.ask(
            "Proxy source",
            choices=["f", "p", "s", "n"],
            default="f",
            options=[
                ("f", "file", _PROXY_FILE_DISPLAY),
                ("p", "paste", "paste lines directly (one per line)"),
                ("s", "scrape", "scrape free proxies (17 sources)"),
                ("n", "none", "run without proxies (direct)"),
            ],
        )

        if mode == "f":
            proxy_path = Prompt.ask("Path to proxy file", default=_PROXY_FILE_DISPLAY)
            resolved = _resolve_input_path(proxy_path)
            proxies = load_lines(resolved)
            if not proxies:
                fail("No proxies found in that file.")
            else:
                source_path = resolved
        elif mode == "p":
            console.print(f"\n[{C.PRIMARY}]Paste your proxies[/] [dim](one per line, empty line to finish)[/]")
            console.print(f"[{C.MUTED}]Format: login:pass@host:port (hoặc host:port:login:pass)[/]")
            collected = _collect_pasted_lines()
            lines_pasted = []
            for line in collected:
                norm = normalize_proxy(line)
                if norm is None:
                    fail(f"Skipped invalid line: {line[:60]}")
                    continue
                lines_pasted.append(norm)
            if lines_pasted:
                ensure_file(DEFAULT_PROXY_FILE)
                Path(DEFAULT_PROXY_FILE).write_text("\n".join(lines_pasted), encoding="utf-8")
                proxies = lines_pasted
                source_path = DEFAULT_PROXY_FILE
            else:
                fail("No proxies provided.")
        elif mode == "s":
            console.print()
            scraped_proxies = await _scrape_proxies()
            if scraped_proxies:
                # Pre-validate: free proxy chết ~95% – lọc nhanh để giữ
                # lại proxy sống trước khi vào màn hình check chính.
                try:
                    from src.proxy import ProxyManager as _PM
                    console.print(f"[{C.MUTED}]Lọc proxy sống (pre-check nhanh)...[/]")
                    alive = await _PM(scraped_proxies, scored=True).filter_working(
                        timeout=8.0, concurrency=300
                    )
                    if alive:
                        ok(f"Giữ lại {len(alive)}/{len(scraped_proxies)} proxy sống")
                        scraped_proxies = alive
                    else:
                        warn_card("Pre-check: 0 proxy sống – giữ nguyên để thử lại sau.")
                except Exception as e:
                    warn_card(f"Pre-check lỗi: {e} – giữ nguyên danh sách.")
                proxies = scraped_proxies
                scraped = True
                ensure_file(DEFAULT_PROXY_FILE)
                Path(DEFAULT_PROXY_FILE).write_text("\n".join(proxies), encoding="utf-8")
                source_path = DEFAULT_PROXY_FILE
        # mode "n" -> proxies stays empty (proxyless)

    if proxies:
        tag = f" [{C.MUTED}](free)[/]" if scraped else ""
        ok(f"{len(proxies)} proxies loaded{tag}")

        # Always ask about removing dead proxies (scraped or file)
        if len(proxies) > 1:
            remove_bad = Confirm.ask(
                "Auto-remove dead proxies?", default=True
            )
    else:
        warn_card(
            f"[{C.WARNING}]Running proxyless — expect heavy rate-limiting.[/]",
            f"[{C.MUTED}]Tip: close your Discord client to free up requests.[/]",
        )

    if proxies and source_path:
        config.set("last_proxy_file", to_portable_path(source_path))
    # proxy_mode: scraped ưu tiên, không có proxy -> none, còn lại static
    config.set("proxy_mode", "scraped" if scraped else ("none" if not proxies else "static"))

    return proxies, remove_bad, scraped


# ---------------------------------------------------------------------------
# Step 2: Usernames
# ---------------------------------------------------------------------------


def _step_usernames(config: Config | None = None) -> list[str]:
    """Guide user through username list setup."""
    raw = Prompt.ask(
        "Usernames",
        choices=["f", "g"],
        default="f",
        options=[
            ("f", "file", _NAMES_FILE_DISPLAY),
            ("g", "generate", "generate random by length"),
        ],
    )
    mode = "generate" if raw == "g" else "file"
    usernames: list[str] = []

    if mode == "file":
        names_path = Prompt.ask("Path to username file", default=_NAMES_FILE_DISPLAY)
        resolved = _resolve_input_path(names_path)
        usernames = load_lines(resolved)
        raw_count = len(usernames)
        usernames, removed = filter_usernames(usernames)
        if removed:
            console.print(f"[{C.MUTED}]Đã lọc {removed}/{raw_count} tên (trùng lặp / sai định dạng – chỉ a-z 0-9 '_' '.' , dài 2-32)[/]")
        if not usernames:
            fail("File is empty — switching to generate mode.")
            mode = "generate"
        else:
            ok(f"Đã nạp {len(usernames)} usernames hợp lệ")
            if config is not None:
                config.set("last_names_file", to_portable_path(resolved))

    if mode == "generate":
        length = IntPrompt.ask("Username length", default=4, choices=["2", "3", "4", "5"])
        total = len(USERNAME_CHARS) ** length

        if length >= 4:
            # Too many combos for itertools.product (2M cho 4 ký tự, 79M cho 5)
            ok(f"Generating 50000 random {length}-char usernames (of {total:,} possible)...")
            seen: set[str] = set()
            usernames = []
            while len(usernames) < 50000:
                cand = "".join(random.choices(USERNAME_CHARS, k=length))
                if cand not in seen and is_valid_username(cand):
                    seen.add(cand)
                    usernames.append(cand)
        else:
            combos = ["".join(c) for c in itertools.product(USERNAME_CHARS, repeat=length)]
            usernames = [c for c in combos if is_valid_username(c)]
            usernames = random.sample(usernames, min(50000, len(usernames)))

        ensure_file(DEFAULT_NAMES_FILE)
        Path(DEFAULT_NAMES_FILE).write_text("\n".join(usernames), encoding="utf-8")
        if config is not None:
            config.set("last_names_file", to_portable_path(DEFAULT_NAMES_FILE))
        ok(f"Generated {len(usernames)} usernames (of {total:,} possible)")

    return usernames


# ---------------------------------------------------------------------------
# Step 3: Speed
# ---------------------------------------------------------------------------

def _step_speed(proxies: list[str], scraped: bool = False) -> tuple[int, int]:
    """Guide user through concurrency and timeout settings."""
    if not proxies:
        info("Proxyless mode — 1 worker with delay.")
        delay = max(1, IntPrompt.ask("Delay between requests (seconds)", default=5))
        return 1, delay

    if scraped:
        info("Free proxy mode — high concurrency, short timeout.")
        conc = max(1, IntPrompt.ask("Concurrent workers", default=50))
        if conc > MAX_CONCURRENCY:
            warn_card(f"Capped at {MAX_CONCURRENCY} — beyond this the program freezes.")
            conc = MAX_CONCURRENCY
        timeout = max(1, IntPrompt.ask("Request timeout (seconds)", default=5))
        return conc, timeout

    default_conc = min(MAX_CONCURRENCY, max(10, len(proxies) * 5))
    concurrency = max(1, IntPrompt.ask("Concurrent workers", default=default_conc))
    if concurrency > MAX_CONCURRENCY:
        warn_card(f"Capped at {MAX_CONCURRENCY} — beyond this the program freezes.")
        concurrency = MAX_CONCURRENCY
    timeout = max(1, IntPrompt.ask("Request timeout (seconds)", default=10))
    return concurrency, timeout


# ---------------------------------------------------------------------------
# Step 4: Webhook
# ---------------------------------------------------------------------------

def _step_webhook(config: Config) -> tuple[str | None, str | None]:
    """Guide user through optional webhook setup (message cố định mặc định)."""

    saved_url = (config.get("webhook") or "").strip()

    if saved_url:
        if not Confirm.ask(
            f"Use webhook? [dim](saved from last session)[/]", default=True
        ):
            if Confirm.ask("Forget saved webhook?", default=False):
                config.set("webhook", "")
            return None, None
        return saved_url, DEFAULT_WEBHOOK_MESSAGE

    if not Confirm.ask("Send hits to a Discord webhook?", default=False):
        return None, None

    url = (Prompt.ask("Webhook URL") or "").strip()
    if not url:
        info("Empty URL — webhook disabled.")
        return None, None

    console.print(f"[{C.MUTED}]Hits are sent in batches to avoid rate-limits.[/]")
    config.set("webhook", url)
    return url, DEFAULT_WEBHOOK_MESSAGE


# ---------------------------------------------------------------------------
# Proxy scraper
# ---------------------------------------------------------------------------

async def _scrape_proxies() -> list[str]:
    """Fetch free HTTP proxies from multiple sources, deduplicate."""

    SOURCES = [
        ("TheSpeedX",           "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
        ("monosans",            "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
        ("proxifly",            "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"),
        ("ShiftyTR-http",       "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt"),
        ("ShiftyTR-https",      "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt"),
        ("roosterkid",          "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
        ("sunny9577",           "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt"),
        ("rdavydov",            "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt"),
        ("Anonym0usWork12",     "https://raw.githubusercontent.com/Anonym0usWork12/Proxy-List/master/http.txt"),
        ("officialputuid",      "https://raw.githubusercontent.com/officialputuid/rules/master/proxies.txt"),
        ("mmpx12-http",         "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
        ("mmpx12-https",        "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt"),
        ("iplocate-http",       "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt"),
        ("iplocate-https",      "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/https.txt"),
        ("openproxylist.xyz",   "https://api.openproxylist.xyz/http.txt"),
        ("proxyscrape.com",     "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all"),
        ("geonode.com",         "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps"),
    ]

    console.print(f"[{C.MUTED}]Scraping {len(SOURCES)} sources...[/]")

    async def _fetch_one(
        sess: aiohttp.ClientSession, name: str, url: str
    ) -> list[str]:
        try:
            async with sess.get(url) as resp:
                if resp.status != 200:
                    console.print(f"  [{C.DANGER}]✖[/] {name} HTTP {resp.status}")
                    return []
                # Geonode trả JSON {"data": [{"ip","port",...}]} – parse riêng
                if "geonode.com" in url:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        console.print(f"  [{C.DANGER}]✖[/] {name} bad JSON")
                        return []
                    rows = data.get("data", []) if isinstance(data, dict) else []
                    found = []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        ip = row.get("ip")
                        port = row.get("port")
                        if ip and port:
                            norm = normalize_proxy(f"{ip}:{port}")
                            if norm:
                                found.append(norm)
                    console.print(f"  [{C.SUCCESS}]✔[/] {name} {len(found)} proxies")
                    return found
                text = await resp.text()
                found = []
                for line in text.splitlines():
                    norm = normalize_proxy(line.strip())
                    if norm:
                        found.append(norm)
                console.print(f"  [{C.SUCCESS}]✔[/] {name} {len(found)} proxies")
                return found
        except Exception as e:
            console.print(f"  [{C.DANGER}]✖[/] {name} {e}")
            return []

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10),
        trust_env=False,
    ) as sess:
        tasks = [_fetch_one(sess, name, url) for name, url in SOURCES]
        results = await asyncio.gather(*tasks)

    seen: set[str] = set()
    all_proxies: list[str] = []
    for batch in results:
        for p in batch:
            key = p.split("@")[-1] if "@" in p else p
            if key not in seen:
                seen.add(key)
                all_proxies.append(p)

    if not all_proxies:
        fail("All sources failed — no proxies.")
        return []

    ok(f"{len(all_proxies)} unique proxies [{C.MUTED}](~2-5% usually work)[/]")
    return all_proxies
