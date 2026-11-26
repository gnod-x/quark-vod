"""Explicit host paths. Defaults stay inside the configured writable data base."""
import os
from pathlib import Path
from quark_vod import config

def path(name, default):
    value = config._value(name, str(default))
    if not value or not os.path.isabs(os.path.expanduser(value)):
        raise ValueError(name + " must be an absolute path")
    return os.path.abspath(os.path.expanduser(value))

BASE_DIR = config.BASE_DIR
DATA_DIR = config.DATA_DIR
LOG_DIR = config.LOG_DIR
CINEMA2_DIR = config.CINEMA2_DIR
CINEMA_DIR = path("VOD_HOST_CINEMA_DIR", Path(BASE_DIR) / "cinema")
SOURCE_DIR = path("VOD_HOST_SOURCE_DIR", Path(BASE_DIR) / "media")
VIDEO_ROOT = path("VOD_HOST_VIDEO_ROOT", Path(SOURCE_DIR) / "视频")
JELLYFIN_CONFIG = path("VOD_HOST_JELLYFIN_CONFIG", Path(BASE_DIR) / "jellyfin/config")
JELLYFIN2_CONFIG = path("VOD_HOST_JELLYFIN2_CONFIG", Path(BASE_DIR) / "jellyfin2/config")
JELLYFIN_DB = path("VOD_HOST_JELLYFIN_DB", Path(JELLYFIN_CONFIG) / "data/jellyfin.db")
JELLYFIN2_DB = config.JELLYFIN2_DB
JELLYFIN2_LOG_DIR = path("VOD_HOST_JELLYFIN2_LOG_DIR", Path(JELLYFIN2_CONFIG) / "log")
JELLYFIN2_METADATA_DIR = path("VOD_HOST_JELLYFIN2_METADATA_DIR", Path(JELLYFIN2_CONFIG) / "metadata")
TMDB_XML = config._value("VOD_HOST_TMDB_XML", "")
AI_CACHE_DB = path("AI_CACHE_DB_PATH", Path(DATA_DIR) / "ai_cache.db")
RSS_OUTPUT_DIR = path("VOD_HOST_RSS_OUTPUT_DIR", Path(DATA_DIR) / "recommendations")
SKIP_PRESETS_FILE = path("VOD_HOST_SKIP_PRESETS_FILE", Path(DATA_DIR) / "skip_presets.json")
TRADITIONAL_API = config._value("VOD_HOST_JELLYFIN_API", "http://127.0.0.1:8096")
