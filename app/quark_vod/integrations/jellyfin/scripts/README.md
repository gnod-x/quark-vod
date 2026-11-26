# Jellyfin media wrappers

These are Quark-VOD owned shell wrappers, not builds of FFmpeg, ffprobe or jq.
Mount this directory read-only into Jellyfin and configure its encoder to use
the mounted ffmpeg script. Both scripts must be adjacent. Install curl and jq
in the Jellyfin runtime; the upstream jellyfin-ffmpeg package supplies the actual
encoder and probe.

Optional environment overrides: FFMPEG_BINARY, FFPROBE_BINARY, FFPROBE_JQ,
VOD_CURL_BINARY, FFPROBE_CACHE_DIR, FFPROBE_TIMEOUT, FFPROBE_RETRIES.
The cache directory must be writable by Jellyfin. No binary or shared-library
bundle is distributed here. Playback, subtitle and hardware support still
depend on the upstream Jellyfin/FFmpeg installation and the client.
