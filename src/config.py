#!/usr/bin/env python3
"""Discord Username Finder v3.1 – Configuration, constants, helpers, and data models."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import string
import urllib.request
from urllib.parse import quote
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths – resolved relative to project root
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
CONFIG_FILE = PROJECT_ROOT / "config.json"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "3.1.0"
ENDPOINT = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"
USERNAME_CHARS = string.ascii_lowercase + string.digits + "_" + "."
MAX_CONCURRENCY = 2000  # hard cap — beyond this asyncio/aiohttp stalls
# Nội dung tin nhắn webhook mặc định (cố định)
DEFAULT_WEBHOOK_MESSAGE = "**<name>** available | <t:time:R>"

# Browser-mimicking headers shared by checker + validator.
# aiohttp defaults to "Python/3.x aiohttp/3.x" which Cloudflare/Discord
# WAF blocks aggressively on datacenter IPs.
# X-Super-Properties: base64 JSON fingerprint of Discord web client
# (Chrome 126 / Win10) – required to look like the real web app.
_SUPER_PROPERTIES = (
    "eyJvcyI6IldpbmRvd3MgMTAiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2aWNlIjoiIiwn"
    "c3lzdGVtX2xvY2FsZSI6ImVuLVVTIiwiYnJvd3Nlcl91c2VyX2FnZW50IjoiTW96aWxs"
    "YS81LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3"
    "LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEyNi4wLjAuMCBTYWZhcmkvNTM3"
    "LjM2IiwiYnJvd3Nlcl92ZXJzaW9uIjoiMTI2LjAuMC4wIiwib3NfdmVyc2lvbiI6IjEw"
    "IiwiY2xpZW50X2J1aWxkX251bWJlciI6MzM3MDEwLCJjbGllbnRfZXZlbnRfbmFtZSI6"
    "ImRpc2NvcmQgU3RhYmxlIiwiY2xpZW50X2V2ZW50X3NvdXJjZSI6bnVsbH0="
)
DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://discord.com",
    "Referer": "https://discord.com/",
    "X-Super-Properties": _SUPER_PROPERTIES,
    "X-Discord-Locale": "en-US",
    "X-Discord-Timezone": "America/New_York",
    "Sec-Ch-Ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


# ---------------------------------------------------------------------------
# Dynamic client build number – scraped from the Discord web client
# ---------------------------------------------------------------------------

BUILD_NUMBER_FALLBACK = 337010  # build mặc định khi không cào được


def _fetch_text(url: str, timeout: float = 3.0, max_bytes: int = 5_000_000) -> str:
    """GET url -> text, UA giống DEFAULT_HEADERS; raise khi lỗi; cắt ở max_bytes."""
    req = urllib.request.Request(
        url, headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(max_bytes)
    return raw.decode("utf-8", errors="replace")


def scrape_client_build(timeout: float = 3.0) -> int | None:
    """Cào client_build_number từ web client Discord; None nếu thất bại (không bao giờ raise)."""
    try:
        html = _fetch_text("https://discord.com/app", timeout)
        match = re.search(r"/assets/[A-Za-z0-9._-]+\.js", html)
        if match is None:
            return None
        bundle = _fetch_text("https://discord.com" + match.group(0), timeout)
        match = re.search(r"Build Number:\s*(\d+)", bundle)
        if match is None:
            return None
        return int(match.group(1))
    except Exception:
        return None


def build_super_properties(build_number: int) -> str:
    """base64 JSON X-Super-Properties với client_build_number động (schema giống bản hardcode)."""
    payload = {
        "os": "Windows 10",
        "browser": "Chrome",
        "device": "",
        "system_locale": "en-US",
        "browser_user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "browser_version": "126.0.0.0",
        "os_version": "10",
        "client_build_number": int(build_number),
        "client_event_name": "discord Stable",
        "client_event_source": None,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def refresh_default_headers(config=None) -> bool:
    """Cập nhật DEFAULT_HEADERS['X-Super-Properties'] in-place; True nếu cập nhật."""
    if config is not None and not config.get("auto_scrape_build", True):
        return False
    build_number = scrape_client_build()
    if build_number is None or build_number <= 0:
        return False
    DEFAULT_HEADERS["X-Super-Properties"] = build_super_properties(build_number)
    return True


def create_browser_ssl_context() -> "ssl.SSLContext":
    """Build an SSLContext with Chrome-like cipher order for aiohttp.

    Guard: falls back to default context if cipher tuning unsupported.
    """
    import ssl as _ssl

    try:
        ctx = _ssl.create_default_context()
        if hasattr(_ssl, "TLSVersion"):
            ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
        try:
            ctx.set_ciphers(
                "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
                "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
                "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305"
            )
        except Exception:
            pass
        return ctx
    except Exception:
        import ssl as _ssl2
        return _ssl2.create_default_context()

# Shared proxy-line regex (moved from wizard): host:port with optional
# scheme and optional user:pass@. Rejects JSON cruft like '"ip":"1.2.3.4"'.
PROXY_RE = re.compile(
    r"^(?:https?://)?(?:[^@\s]+@)?[a-zA-Z0-9](?:[a-zA-Z0-9\-.]*[a-zA-Z0-9])?:\d{1,5}$"
)

# Proxy dạng 4 phần kiểu nhà cung cấp: host:port:user:pass (không scheme/@).
PROXY_4_RE = re.compile(
    r"^([a-zA-Z0-9](?:[a-zA-Z0-9\-.]*[a-zA-Z0-9])?):(\d{1,5}):([^:@\s]+):([^:@\s]+)$"
)


def normalize_proxy(raw: str | None) -> str | None:
    """Normalize one proxy line. Returns None if unusable.

    Guard clauses: None/empty -> None; socks4/5 (aiohttp can't handle
    via `proxy=`) -> None; regex mismatch -> None. Otherwise prepends
    http:// when no scheme is present.

    Hỗ trợ thêm dạng 4 phần host:port:user:pass (kiểu nhà cung cấp,
    không scheme/@) -> chuẩn hoá thành http://user:pass@host:port.
    """
    if not raw:
        return None
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    low = line.lower()
    if low.startswith(("socks4://", "socks5://")):
        return None
    # Dạng 4 phần host:port:user:pass -> chuẩn hoá thành URL http://
    if "@" not in line and "://" not in line:
        m = PROXY_4_RE.match(line)
        if m is not None:
            host, port, user, password = m.groups()
            return (
                f"http://{quote(user, safe='')}:"
                f"{quote(password, safe='')}@{host}:{port}"
            )
    if not PROXY_RE.match(line):
        return None
    return line if line.startswith("http") else f"http://{line}"


# ---------------------------------------------------------------------------
# Colour palette (Rich hex codes)
# ---------------------------------------------------------------------------

class C:
    """Semantic colour constants – vibrant terminal-optimized palette."""
    PRIMARY   = "#0A84FF"   # bright blue – headers, accents, borders
    SUCCESS   = "#30D158"   # bright green – available, hits
    DANGER    = "#FF453A"   # bright red – taken, errors
    WARNING   = "#FF9F0A"   # bright orange – warnings, rate-limits
    MUTED     = "#98989D"   # light gray – secondary text
    BORDER    = "#48484A"   # subtle border – cards


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ensure_dir(*paths: str | Path) -> None:
    """Create directories if they don't exist."""
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)


def ensure_file(filepath: str | Path, *, clean: bool = False) -> None:
    """Create a file (and its parents). If *clean*, truncate it."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    if clean or not path.exists():
        path.write_text("", encoding="utf-8")


def load_lines(filepath: str | Path) -> list[str]:
    """Read non-empty lines from a file. Returns [] if missing or undecodable (NUL bytes)."""
    path = Path(filepath)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        # Chỉ fallback UTF-16 khi file có BOM (Notepad "Unicode");
        # mã hoá lạ (ANSI...) mà decode mù sẽ ra rác -> coi như rỗng
        try:
            raw = path.read_bytes()
            if raw[:2] not in (b"\xff\xfe", b"\xfe\xff"):
                return []
            text = raw.decode("utf-16")
        except (OSError, ValueError):
            return []
    except (OSError, ValueError):
        # Thư mục/khoá quyền/lỗi đọc -> coi như rỗng thay vì crash
        return []
    if "\x00" in text:
        # UTF-16/UTF-32 thiếu BOM hoặc file nhị phân: decode utf-8-sig
        # "thành công" nhưng còn NUL -> coi như không đọc được, trả rỗng.
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def is_valid_username(name: str) -> bool:
    """Check whether a username passes Discord's basic client-side rules.

    - 2–32 characters
    - only a–z, 0–9, '_', '.'
    - no consecutive dots ('..')
    - cannot start or end with a dot
    - không được chứa chuỗi con "discord" hoặc "clyde" (không phân biệt hoa/thường)
    - không được trùng "everyone" hoặc "here" (reserved, không phân biệt hoa/thường)
    """
    if not isinstance(name, str):
        return False
    if not (2 <= len(name) <= 32):
        return False
    if ".." in name:
        return False
    if name.startswith(".") or name.endswith("."):
        return False
    lowered = name.lower()
    if "discord" in lowered or "clyde" in lowered:
        return False
    if lowered in {"everyone", "here"}:
        return False
    return all(c in USERNAME_CHARS for c in name)


def filter_usernames(names: list[str]) -> tuple[list[str], int]:
    """Lọc tên trùng lặp + không hợp lệ. Trả về (danh sách sạch, số lượng bị loại)."""
    clean: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = (raw or "").strip()
        if not name or name in seen or not is_valid_username(name):
            continue
        seen.add(name)
        clean.append(name)
    return clean, len(names or []) - len(clean)


# ---------------------------------------------------------------------------
# Persistent JSON config (nested sections, flat in-memory keys)
# ---------------------------------------------------------------------------

# key -> section để config.json phân nhóm rõ ràng (chỉ ghi khi set; khi đọc
# luôn tra theo bare-key nên mọi call site config.get("concurrency") giữ nguyên).
_CONFIG_SECTION: dict[str, str] = {
    "concurrency": "general",
    "timeout": "general",
    "last_names_file": "general",
    "proxy_mode": "proxies",
    "remove_proxies": "proxies",
    "last_proxy_file": "proxies",
    "use_tokens": "tokens",
    "webhook": "notifications",
    "third_party_api_url": "advanced",
}

# Key cruft legacy: tự loại khi nạp để config.json gọn lại. Đọc code vẫn
# fallback default tương ứng nên hành vi không đổi.
_CONFIG_CRUFT: frozenset[str] = frozenset(
    {"auto_scrape_build", "use_fingerprint", "reuse_proxies"}
)


def to_portable_path(raw: str | Path) -> str:
    """Đường dẫn ABSOLUTE -> tương đối so với PROJECT_ROOT (nằm trong project).

    Giữ nguyên absolute nếu nằm ngoài project (vd: ổ khác). Tránh lưu lộ
    thông tin cá nhân (E:\\Users\\...) vào config.json.
    """
    try:
        p = Path(raw).expanduser().resolve()
        return str(p.relative_to(PROJECT_ROOT))
    except Exception:
        return str(raw)


def from_portable_path(raw: str | Path) -> str:
    """Đường dẫn tương đối (hoặc absolute) -> absolute tuyệt đối.

    Path tương đối được resolve so với PROJECT_ROOT để chạy được mọi cwd.
    """
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return str(p)


class Config:
    """JSON-backed persistent config: file phân nhóm, trong-memory bare-key.

    - File lưu dạng nested theo _CONFIG_SECTION ("phân chia rõ ràng").
    - get/set LUÔN dùng bare-key ("concurrency") -> không đổi call site.
    - Tự loại key cruft legacy khi nạp; key lạ không mapping vào "other".
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else CONFIG_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        raw: dict = {}
        try:
            if self._path.exists() and self._path.stat().st_size > 0:
                loaded = json.loads(self._path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    raw = loaded
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            raw = {}
        # Phẳng (format cũ) hoặc nested (mới) -> flatten về bare-key dict.
        # Giá trị dict = section; giá trị scalar = key cấp gốc (legacy).
        self._data = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    if k2 not in _CONFIG_CRUFT:
                        self._data[k2] = v2
            elif k not in _CONFIG_CRUFT:
                self._data[k] = v
        # Ghi lại file đã gọn (nested, không cruft) ngay lần nạp đầu.
        try:
            self._save()
        except OSError:
            pass

    def _save(self) -> None:
        """Ghi _data ra file nested theo _CONFIG_SECTION (key lạ -> 'other')."""
        nested: dict[str, dict] = {}
        for k, v in self._data.items():
            sec = _CONFIG_SECTION.get(k, "other")
            nested.setdefault(sec, {})[k] = v
        self._path.write_text(json.dumps(nested, indent=2), encoding="utf-8")

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self._save()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AppSettings:
    """Application-level settings (from CLI args)."""
    debug: bool = False
    no_wizard: bool = False


@dataclass
class RunConfig:
    """All user choices gathered during setup."""
    proxies: list[str]
    remove_bad_proxies: bool
    usernames: list[str]
    concurrency: int
    timeout: int
    scraped: bool = False
    webhook_url: str | None = None
    webhook_message: str | None = None


@dataclass
class Stats:
    """Thread-safe stats counter."""
    requests: int = 0
    works: int = 0
    taken: int = 0
    ratelimited: int = 0
    circuit_opens: int = 0
    rps: float = 0.0
    checks_rps: float = 0.0
    peak_rps: float = 0.0
    best_streak: int = 0
    _streak: int = 0
    _lock: asyncio.Lock = field(default=None, init=False, repr=False)  # type: ignore

    def __post_init__(self) -> None:
        import asyncio
        self._lock = asyncio.Lock()

    async def inc_requests(self) -> None:
        async with self._lock:
            self.requests += 1

    async def inc_works(self) -> None:
        async with self._lock:
            self.works += 1
            self._streak += 1
            if self._streak > self.best_streak:
                self.best_streak = self._streak

    async def inc_taken(self) -> None:
        async with self._lock:
            self.taken += 1
            self._streak = 0

    async def inc_ratelimited(self) -> None:
        async with self._lock:
            self.ratelimited += 1

    async def inc_circuit_open(self) -> None:
        async with self._lock:
            self.circuit_opens += 1

    async def set_rps(self, value: float) -> None:
        async with self._lock:
            self.rps = value
            if value > self.peak_rps:
                self.peak_rps = value

    async def set_checks_rps(self, value: float) -> None:
        async with self._lock:
            self.checks_rps = value

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "requests": self.requests,
                "works": self.works,
                "taken": self.taken,
                "ratelimited": self.ratelimited,
                "circuit_opens": self.circuit_opens,
                "rps": self.rps,
                "checks_rps": self.checks_rps,
                "peak_rps": self.peak_rps,
                "best_streak": self.best_streak,
            }
