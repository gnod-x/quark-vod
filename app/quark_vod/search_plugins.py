"""Validated, atomic search-provider configuration.

Only explicitly installed, trusted entry points execute Python. JSON files
select providers and their HTTP endpoints; they never contain executable code.
"""
from dataclasses import dataclass
from importlib import metadata
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Callable
from urllib.parse import urlsplit

from . import config

MAX_CONFIG_BYTES = 256 * 1024
MAX_NODES = 32
_ID = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}\Z")
_PROVIDERS = {}
_PROVIDER_LOCK = threading.RLock()
_ENTRYPOINT_ATTEMPTS = set()
_ENTRYPOINT_ERRORS = {}


class PluginConfigError(ValueError):
    """Safe for API responses: messages contain no input values or paths."""
    def __init__(self, code="invalid_configuration"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SearchNode:
    id: str
    name: str
    provider: str
    base: str
    timeout: float = 4.0
    enabled: bool = True
    path: str = "/search"
    query_param: str = "q"
    channels: tuple = ()
    categories: tuple = ()

    def as_dict(self):
        return {"id": self.id, "key": self.id, "name": self.name,
                "provider": self.provider, "base": self.base,
                "timeout": self.timeout, "enabled": self.enabled,
                "path": self.path, "query_param": self.query_param,
                "channels": list(self.channels), "categories": list(self.categories)}

    def public_dict(self):
        return {"id": self.id, "name": self.name, "provider": self.provider,
                "timeout": self.timeout, "enabled": self.enabled}


@dataclass(frozen=True)
class SearchSnapshot:
    nodes: tuple
    fingerprint: str
    generation: int = 0

    @property
    def enabled_nodes(self):
        return tuple(n for n in self.nodes if n.enabled)


def provider_names():
    with _PROVIDER_LOCK:
        return tuple(sorted({"html", "json", "tg_web", "pansou"} | set(_PROVIDERS)))


def register_provider(name: str, fetch: Callable):
    """Register trusted code: fetch(node, keyword, limit, max_pages)->(rows, report).

    Built-ins cannot be replaced. Registration is startup-only; configuration
    hot reload changes nodes, not the code of an executing provider.
    """
    if not isinstance(name, str) or not _ID.fullmatch(name) or name in ("html", "json", "tg_web", "pansou") or not callable(fetch):
        raise PluginConfigError("invalid_provider_registration")
    with _PROVIDER_LOCK:
        if name in _PROVIDERS and _PROVIDERS[name] is not fetch:
            raise PluginConfigError("duplicate_provider_registration")
        _PROVIDERS[name] = fetch


def get_provider(name):
    with _PROVIDER_LOCK:
        return _PROVIDERS.get(name)


def load_entrypoints(names=None):
    """Load only names explicitly selected by VOD_SEARCH_PLUGINS."""
    names = names if names is not None else config._value("VOD_SEARCH_PLUGINS").split(",")
    names = tuple(dict.fromkeys(n.strip() for n in names if n.strip()))
    if any(not _ID.fullmatch(n) or n in ("html", "json", "tg_web", "pansou") for n in names):
        raise PluginConfigError("invalid_entrypoint_selection")
    if not names:
        return {}
    with _PROVIDER_LOCK:
        pending = tuple(n for n in names if n not in _ENTRYPOINT_ATTEMPTS)
        if not pending:
            return {n: _ENTRYPOINT_ERRORS[n] for n in names if n in _ENTRYPOINT_ERRORS}
    entries = metadata.entry_points(group="quark_vod.search")
    for name in pending:
        with _PROVIDER_LOCK:
            if name in _ENTRYPOINT_ATTEMPTS:
                continue
            _ENTRYPOINT_ATTEMPTS.add(name)
        try:
            found = [ep for ep in entries if ep.name == name]
            if len(found) != 1:
                raise PluginConfigError("entrypoint_not_unique")
            register_provider(name, found[0].load())
        except Exception:
            with _PROVIDER_LOCK:
                _ENTRYPOINT_ERRORS[name] = "entrypoint_load_failed"
    with _PROVIDER_LOCK:
        return {n: _ENTRYPOINT_ERRORS[n] for n in names if n in _ENTRYPOINT_ERRORS}


def _valid_base(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise PluginConfigError("invalid_node_url")
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
            raise PluginConfigError("invalid_node_url")
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise PluginConfigError("invalid_node_url")
    except ValueError:
        raise PluginConfigError("invalid_node_url") from None
    return value.rstrip("/")


def _node(raw):
    if not isinstance(raw, dict) or set(raw) - {"id", "key", "name", "provider", "base", "timeout", "enabled", "path", "query_param", "channels", "categories"}:
        raise PluginConfigError("invalid_node_fields")
    identifier = raw.get("id", raw.get("key"))
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        raise PluginConfigError("invalid_node_id")
    if "key" in raw and raw["key"] != identifier:
        raise PluginConfigError("conflicting_node_id")
    name = raw.get("name", identifier)
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise PluginConfigError("invalid_node_name")
    provider = raw.get("provider", "html")
    if not isinstance(provider, str) or not _ID.fullmatch(provider):
        raise PluginConfigError("invalid_provider")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PluginConfigError("invalid_enabled")
    if enabled and provider not in provider_names():
        raise PluginConfigError("unknown_provider")
    timeout = raw.get("timeout", 4.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not .1 <= timeout <= 7:
        raise PluginConfigError("invalid_timeout")
    path = raw.get("path", "/api/search" if provider == "pansou" else "/search")
    if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
        or len(path) > 1024 or any(ord(c) < 33 for c in path) or "?" in path or "#" in path):
        raise PluginConfigError("invalid_search_path")
    query_param = raw.get("query_param", "q")
    if not isinstance(query_param, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", query_param):
        raise PluginConfigError("invalid_query_parameter")
    channels = raw.get("channels", [])
    categories = raw.get("categories", [])
    if (not isinstance(channels, (list, tuple)) or len(channels) > 64
        or any(not isinstance(c, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,63}", c) for c in channels)):
        raise PluginConfigError("invalid_channels")
    if (not isinstance(categories, (list, tuple)) or len(categories) > 32
        or any(not isinstance(c, str) or not c.strip() or len(c) > 80
               or any(ord(ch) < 32 for ch in c) for c in categories)):
        raise PluginConfigError("invalid_categories")
    base = raw.get("base", "https://t.me" if provider == "tg_web" else None)
    return SearchNode(identifier, name.strip(), provider, _valid_base(base),
                      float(timeout), enabled, path, query_param,
                      tuple(dict.fromkeys(channels)), tuple(dict.fromkeys(c.strip() for c in categories)))


def snapshot_from_nodes(nodes, generation=0):
    if not isinstance(nodes, (list, tuple)) or len(nodes) > MAX_NODES:
        raise PluginConfigError("invalid_node_count")
    parsed = tuple(_node(raw) for raw in nodes)
    if len({n.id for n in parsed}) != len(parsed):
        raise PluginConfigError("duplicate_node_id")
    semantic = [{k: v for k, v in n.as_dict().items() if k != "key"} for n in parsed]
    fingerprint = hashlib.sha256(json.dumps(
        {"version": 1, "nodes": semantic}, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return SearchSnapshot(parsed, fingerprint, generation)


class SearchRegistry:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._snapshot = snapshot_from_nodes([])
        self._seen_signature = object()
        self._error = None

    def _signature(self):
        if self.path is None:
            return None
        try:
            s = self.path.stat()
            return (s.st_dev, s.st_ino, s.st_mtime_ns, s.st_size)
        except OSError:
            return ("unavailable",)

    def _read(self):
        if self.path is None:
            raise PluginConfigError("search_config_not_set")
        try:
            with self.path.open("rb") as f:
                raw = f.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise PluginConfigError("search_config_too_large")
            document = json.loads(raw.decode("utf-8"))
        except PluginConfigError:
            raise
        except OSError:
            raise PluginConfigError("search_config_unavailable") from None
        except (ValueError, UnicodeError):
            raise PluginConfigError("search_config_invalid_json") from None
        if (not isinstance(document, dict) or set(document) != {"version", "nodes"}
            or type(document["version"]) is not int or document["version"] != 1):
            raise PluginConfigError("search_config_invalid_schema")
        return snapshot_from_nodes(document["nodes"], self._snapshot.generation + 1)

    def reload(self, *, strict=True):
        with self._lock:
            observed_signature = self._signature()
            try:
                candidate = self._read()
                if candidate.fingerprint != self._snapshot.fingerprint:
                    self._snapshot = candidate
                self._error = None
            except PluginConfigError as exc:
                self._error = exc.code
                if strict:
                    raise
            finally:
                # A replacement during reading must be observed next request.
                self._seen_signature = observed_signature
            return self.status(refresh=False)

    def snapshot(self):
        with self._lock:
            signature = self._signature()
            if self.path is not None and signature != self._seen_signature:
                self.reload(strict=False)
            return self._snapshot

    def status(self, *, refresh=True):
        with self._lock:
            snapshot = self.snapshot() if refresh else self._snapshot
            return {"configured": bool(snapshot.enabled_nodes),
                    "config_file_set": self.path is not None,
                    "generation": snapshot.generation,
                    "fingerprint": snapshot.fingerprint,
                    "node_count": len(snapshot.nodes),
                    "enabled_count": len(snapshot.enabled_nodes),
                    "nodes": [n.public_dict() for n in snapshot.nodes],
                    "providers": list(provider_names()),
                    "error": self._error,
                    "entrypoint_errors": dict(_ENTRYPOINT_ERRORS)}


_DEFAULT_LOCK = threading.RLock()
_DEFAULT_REGISTRY = None


def get_registry():
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        load_entrypoints()
        path = config._value("VOD_SEARCH_CONFIG") or None
        if _DEFAULT_REGISTRY is None or str(_DEFAULT_REGISTRY.path or "") != str(path or ""):
            _DEFAULT_REGISTRY = SearchRegistry(path)
        return _DEFAULT_REGISTRY
