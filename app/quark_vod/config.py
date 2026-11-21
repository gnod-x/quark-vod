"""Portable configuration for Quark-VOD.

Only VOD_ENV_FILE opts into an environment file. Paths and integration switches
are explicit; importing this module never creates directories or probes services.
"""
from contextlib import closing
import json
import math
import os
from pathlib import Path
import re
import shlex
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit

_MAX_SECRET_BYTES = 1024 * 1024


def _load_env_file():
    filename = os.environ.get("VOD_ENV_FILE", "").strip()
    if not filename:
        return {}
    try:
        with open(os.path.expanduser(filename), "r", encoding="utf-8") as stream:
            text = stream.read(_MAX_SECRET_BYTES + 1)
        if len(text.encode("utf-8")) > _MAX_SECRET_BYTES:
            raise ValueError("VOD_ENV_FILE is too large")
    except (OSError, UnicodeError):
        raise ValueError("VOD_ENV_FILE cannot be read") from None
    values = {}
    for number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"VOD_ENV_FILE has invalid syntax at line {number}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"VOD_ENV_FILE has invalid syntax at line {number}")
        if value[:1] in ("'", '"'):
            try:
                tokens = shlex.split(value, comments=True, posix=True)
            except ValueError:
                tokens = []
            if len(tokens) != 1:
                raise ValueError(f"VOD_ENV_FILE has invalid quoting at line {number}")
            value = tokens[0]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


_ENV_FILE_VALUES = _load_env_file()


def _value(name, default=""):
    """An explicit empty process value overrides the file rather than falling back."""
    return str(os.environ.get(name, _ENV_FILE_VALUES.get(name, default))).strip()


def _secret(name, default=""):
    # Process settings precede file settings. A process NAME_FILE can override a
    # NAME in VOD_ENV_FILE without placing a secret value in Compose or argv.
    if name in os.environ:
        return os.environ[name].strip()
    if name + "_FILE" in os.environ:
        path = os.environ[name + "_FILE"].strip()
    elif name in _ENV_FILE_VALUES:
        return _ENV_FILE_VALUES[name].strip()
    else:
        path = _ENV_FILE_VALUES.get(name + "_FILE", "").strip()
    if not path:
        return default
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as stream:
            value = stream.read(_MAX_SECRET_BYTES + 1).strip()
        if len(value.encode("utf-8")) > _MAX_SECRET_BYTES:
            raise ValueError("oversized secret")
        return value
    except (OSError, UnicodeError, ValueError):
        raise ValueError(f"{name}_FILE cannot be read as a bounded UTF-8 secret") from None


def _get_env_or_dot_env(key, default=""):
    """Compatibility helper: no implicit .env discovery."""
    return _secret(key, default)


def _boolean(name, default=False):
    raw = _value(name, "true" if default else "false").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean")


def _number(name, default, minimum, maximum, kind=int):
    raw = _value(name, str(default))
    try:
        value = kind(raw)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a valid {kind.__name__}") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside its supported range")
    return value


def _path(name, default, base=None, optional=False):
    value = _value(name, default)
    if not value and optional:
        return ""
    if not value or "\x00" in value:
        raise ValueError(f"{name} must be a valid filesystem path")
    value = os.path.expanduser(value)
    if base and not os.path.isabs(value):
        value = os.path.join(base, value)
    return os.path.abspath(value)


def _url(name, default="", optional=False):
    value = _value(name, default).rstrip("/")
    if not value and optional:
        return ""
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"{name} must have a valid port") from None
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
        raise ValueError(f"{name} must be an HTTP(S) base URL without embedded credentials") from None
    return value


def _mount(name, default):
    value = _value(name, default).rstrip("/")
    if not value.startswith("/") or value == "/" or any(p in (".", "..") for p in value.split("/")):
        raise ValueError(f"{name} must be an absolute non-root virtual path")
    if any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} contains a control character")
    return value


HOST = _value("VOD_HOST", "127.0.0.1")
if not HOST or any(c.isspace() or ord(c) < 32 for c in HOST):
    raise ValueError("VOD_HOST must be a nonempty bind address")
PORT = _number("VOD_PORT", 8099, 1, 65535)
SERVICE_NAME = _value("VOD_SERVICE_NAME", "quark-vod")
BASE_DIR = _path("VOD_BASE_DIR", os.path.join(os.getcwd(), "var"))
CINEMA2_DIR = _path("VOD_CINEMA2_DIR", "cinema2", BASE_DIR)
DATA_DIR = _path("VOD_DATA_DIR", "data", BASE_DIR)
LOG_DIR = _path("VOD_LOG_DIR", "logs", BASE_DIR)
STATE_DB = _path("VOD_STATE_DB", "quark_plays.db", DATA_DIR)
LOG_FILE = _path("VOD_LOG_FILE", "quark_service.log", LOG_DIR)
COOKIE_OVERRIDE_FILE = _path("QUARK_COOKIE_OVERRIDE_FILE", "quark_cookie.txt", DATA_DIR)

OPENLIST_API = _url("OPENLIST_API", "http://127.0.0.1:5244")
OPENLIST_PUBLIC = _url("OPENLIST_PUBLIC", OPENLIST_API)
OPENLIST_ADMIN_USER = _secret("OPENLIST_ADMIN_USER", "admin")
OPENLIST_ADMIN_PASS = _secret("OPENLIST_ADMIN_PASS")
OPENLIST_DB_PATH = _path("OPENLIST_DB_PATH", "", BASE_DIR, optional=True)

QUARK_MOUNT = _mount("QUARK_MOUNT", "/夸克网盘")
QUARK_VOD_ROOT = _mount("QUARK_VOD_ROOT", QUARK_MOUNT + "/点播")
QUARK_PLAY_MOUNT = _mount("QUARK_PLAY_MOUNT", "/夸克")
QUARK_PLAY_ROOT = _mount("QUARK_PLAY_ROOT", QUARK_PLAY_MOUNT + "/点播")
PENDING_BASE_URL = _url("PENDING_BASE_URL", "http://127.0.0.1:8099")

JELLYFIN2_API = _url("JELLYFIN2_API_BASE", "http://127.0.0.1:8097")
JELLYFIN2_DB = _path("JELLYFIN2_DB", "jellyfin2/config/data/jellyfin.db", BASE_DIR)
JELLYFIN_LIBRARY_ROOT = _mount("JELLYFIN_LIBRARY_ROOT", "/cinema2")
# Metadata deletion requires an explicit host root and database path mapping.
JELLYFIN_METADATA_LIBRARY_DIR = _value("JELLYFIN_METADATA_LIBRARY_DIR", "")
JELLYFIN_METADATA_LIBRARY_PREFIX = _value("JELLYFIN_METADATA_LIBRARY_PREFIX", "")
JELLYFIN_METADATA_EXTERNAL_ROOTS = _value("JELLYFIN_METADATA_EXTERNAL_ROOTS", "[]")
VOD_JELLYFIN_ENABLED = _boolean("VOD_JELLYFIN_ENABLED")
VOD_MAINTENANCE_ENABLED = _boolean("VOD_MAINTENANCE_ENABLED")
VOD_STARTUP_SCAN = _boolean("VOD_STARTUP_SCAN")
VOD_STARTUP_WRITE_PROBE = _boolean("VOD_STARTUP_WRITE_PROBE")
def get_admin_token():
    """Re-read explicit credentials so a mounted secret can rotate live."""
    return _secret("VOD_ADMIN_TOKEN")


VOD_ADMIN_TOKEN = get_admin_token()


def get_jellyfin2_key():
    return _secret("JELLYFIN2_KEY")


JELLYFIN2_KEY = get_jellyfin2_key()
_cached_quark_cookie = ""
_cached_cookie_ts = 0.0
_cached_cookie_source = ""


def _checked_cookie(value):
    if len(value.encode("utf-8")) > _MAX_SECRET_BYTES or any(c in value for c in ("\r", "\n", "\x00")):
        raise ValueError("QUARK_COOKIE must be a bounded single-line header value")
    return value


def get_quark_cookie():
    """Read explicit cookie sources; OpenList SQLite inheritance is opt-in."""
    global _cached_quark_cookie, _cached_cookie_ts, _cached_cookie_source
    if os.path.isfile(COOKIE_OVERRIDE_FILE):
        try:
            with open(COOKIE_OVERRIDE_FILE, "r", encoding="utf-8") as stream:
                value = stream.read(_MAX_SECRET_BYTES + 1).strip()
            if value:
                return _checked_cookie(value)
        except (OSError, UnicodeError):
            raise ValueError("QUARK_COOKIE_OVERRIDE_FILE cannot be read") from None
    if any(key in os.environ or key in _ENV_FILE_VALUES for key in ("QUARK_COOKIE", "QUARK_COOKIE_FILE")):
        direct = _checked_cookie(_secret("QUARK_COOKIE"))
        if direct:
            return direct
    if not OPENLIST_DB_PATH:
        return ""
    source = OPENLIST_DB_PATH + "|" + QUARK_MOUNT
    now = time.monotonic()
    if _cached_quark_cookie and _cached_cookie_source == source and now - _cached_cookie_ts < 60:
        return _cached_quark_cookie
    try:
        # Read WAL normally; immutable=1 would silently miss committed WAL data.
        uri = Path(OPENLIST_DB_PATH).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as con:
            row = con.execute("SELECT addition FROM x_storages WHERE mount_path=?", (QUARK_MOUNT,)).fetchone()
        addition = json.loads(row[0]) if row and row[0] else {}
        candidate = addition.get("cookie", "") if isinstance(addition, dict) else ""
        value = _checked_cookie(candidate) if isinstance(candidate, str) else ""
    except (OSError, sqlite3.Error, ValueError, TypeError):
        value = ""
    _cached_quark_cookie = value
    _cached_cookie_ts = now
    _cached_cookie_source = source
    return value


def set_quark_cookie(new_cookie):
    """Persist an admin override atomically with mode 0600; empty clears it."""
    global _cached_quark_cookie, _cached_cookie_ts, _cached_cookie_source, QUARK_COOKIE, COOKIE_READY
    value = _checked_cookie((new_cookie or "").strip())
    temporary = None
    if value:
        parent = os.path.dirname(COOKIE_OVERRIDE_FILE)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".quark-cookie-", dir=parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, COOKIE_OVERRIDE_FILE)
            temporary = None
        finally:
            if temporary:
                os.unlink(temporary)
    else:
        try:
            os.unlink(COOKIE_OVERRIDE_FILE)
        except FileNotFoundError:
            pass
    _cached_quark_cookie, _cached_cookie_ts, _cached_cookie_source = "", 0.0, ""
    QUARK_COOKIE = get_quark_cookie()
    COOKIE_READY = bool(QUARK_COOKIE)
    return QUARK_COOKIE


QUARK_COOKIE = get_quark_cookie()
COOKIE_READY = bool(QUARK_COOKIE)
TRANSFER_TIMEOUT = _number("TRANSFER_TIMEOUT", 180, 1, 3600)
TRANSFER_CONCURRENCY = _number("TRANSFER_CONCURRENCY", 2, 1, 16)
KEEP_DAYS = _number("KEEP_DAYS", 7, 0, 36500)
MAX_FILE_SIZE = _number("MAX_FILE_SIZE", 30 * 1024**3, 1, 1024**5)
PROBE_VERIFY_TIMEOUT = _number("PROBE_VERIFY_TIMEOUT", 180, 1, 3600)
QUARK_FIRST = True
ALLOW_CMS_FALLBACK = False
CLEANUP_HOUR = _number("CLEANUP_HOUR", 4, 0, 23)
RESTORE_STRM_AFTER_CLEAN = _boolean("RESTORE_STRM_AFTER_CLEAN", True)
SELFHEAL_INTERVAL = _number("SELFHEAL_INTERVAL", 86400, 1, 365 * 86400)
SELFHEAL_FILE_CHECK_BATCH = _number("SELFHEAL_FILE_CHECK_BATCH", 1, 0, 100)
SELFHEAL_INITIAL_DELAY = _number("SELFHEAL_INITIAL_DELAY", 120, 0, 86400)
SELFHEAL_ENABLED = _boolean("SELFHEAL_ENABLED")
AI_PARSE_ENABLED = _boolean("AI_PARSE_ENABLED")
AI_PARSE_SHADOW = _boolean("AI_PARSE_SHADOW")
AI_PARSE_BASE_URL = _url("AI_PARSE_BASE_URL", "", optional=True)
AI_PARSE_MODEL = _value("AI_PARSE_MODEL")
if AI_PARSE_ENABLED and not (AI_PARSE_BASE_URL and AI_PARSE_MODEL):
    raise ValueError("AI_PARSE_ENABLED requires AI_PARSE_BASE_URL and AI_PARSE_MODEL")
AI_PARSE_TIMEOUT = _number("AI_PARSE_TIMEOUT", 20.0, 0.1, 300.0, float)
AI_PARSE_MAX_TOKENS = _number("AI_PARSE_MAX_TOKENS", 4096, 1, 131072)
# This extension is not supported by every OpenAI-compatible endpoint.
# Leave it out by default; deployments may explicitly opt in.
AI_PARSE_REASONING_EFFORT = _value("AI_PARSE_REASONING_EFFORT", "")
AI_PARSE_BATCH_SIZE = _number("AI_PARSE_BATCH_SIZE", 24, 1, 256)
AI_PARSE_MIN_CONFIDENCE = _number("AI_PARSE_MIN_CONFIDENCE", 0.7, 0.0, 1.0, float)
AI_PARSE_MAX_FAILS = _number("AI_PARSE_MAX_FAILS", 3, 1, 100)
AI_PARSE_COOLDOWN = _number("AI_PARSE_COOLDOWN", 300, 1, 86400)
# Compatibility only; search registry owns active providers.
PAN_SEARCH_SITES = []
ACCEPT_PAN_TYPES = ("quark", "夸克")


def load_cliproxy_token():
    """Read an explicitly configured rotating token file or API-key setting."""
    path = _value("AI_PARSE_TOKEN_FILE")
    if path:
        try:
            with open(os.path.expanduser(path), "r", encoding="utf-8") as stream:
                value = stream.read(_MAX_SECRET_BYTES + 1).strip()
            if len(value.encode("utf-8")) > _MAX_SECRET_BYTES:
                raise ValueError("oversized token")
            return value
        except (OSError, UnicodeError, ValueError):
            raise ValueError("AI_PARSE_TOKEN_FILE cannot be read as a bounded UTF-8 secret") from None
    return _secret("OPENAI_API_KEY")


def play_path(remote_path):
    """Translate configured write-root paths to the read-root namespace."""
    if not remote_path:
        return remote_path
    for source, target in ((QUARK_VOD_ROOT, QUARK_PLAY_ROOT), (QUARK_MOUNT, QUARK_PLAY_MOUNT)):
        if remote_path == source:
            return target
        if remote_path.startswith(source + "/"):
            return target + remote_path[len(source):]
    return remote_path


def write_path(remote_path):
    if not remote_path:
        return remote_path
    for source, target in ((QUARK_PLAY_ROOT, QUARK_VOD_ROOT), (QUARK_PLAY_MOUNT, QUARK_MOUNT)):
        if remote_path == source:
            return target
        if remote_path.startswith(source + "/"):
            return target + remote_path[len(source):]
    return remote_path


def ensure_dirs():
    """Create only local mutable directories, never virtual drive roots."""
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)


def redact_cookie(cookie):
    return "(已配置)" if cookie else "(未配置)"
