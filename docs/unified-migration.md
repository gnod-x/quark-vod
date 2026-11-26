# Migrating existing hometheater installations

This change supplies a unified alpha candidate. It does not automatically alter
an existing host, cron table, Docker Compose project, media library or account.
Validate the candidate in an isolated environment before a production switch.

## Prepare

1. Inventory the actual API package, image, host scripts, runtime environment,
   scheduled jobs, wrapper mount and writable directories.
2. Stop every old writer before any state-schema upgrade. Make and verify an
   SQLite online backup, keeping the full WAL-aware database directory available.
   The 3.2 integration does not itself add a new core state-schema version.
3. Install the same candidate version for the API and worker. Provide an explicit
   environment file or environment variables. Do not copy credentials into source,
   public examples or build contexts.
4. Map the existing data, library and Jellyfin paths explicitly. Preserve owners,
   ACLs, resource limits, playback URLs and player-visible paths.

## Configuration

The API and worker both accept `--env /absolute/private.env` and
`--data-dir /absolute/runtime`. Paths and existing core configuration still come
from the deployment environment; page settings own task schedules and AI routing.
Run recovery commands in the worker's original PID namespace. A host CLI cannot
infer that a Docker task has stopped merely because it cannot see its PID.

Useful integration settings include:

| Variable | Purpose |
| --- | --- |
| `VOD_DATA_DIR`, `VOD_LOG_DIR`, `VOD_CINEMA2_DIR` | Existing state, log and STRM locations |
| `VOD_JELLYFIN_ENABLED`, `JELLYFIN2_API_BASE`, `JELLYFIN2_KEY_FILE`, `JELLYFIN2_DB` | Optional point-of-play library and protection integration |
| `VOD_JELLYFIN_PUBLIC_URL` | Browser-reachable Jellyfin base URL for deep links |
| `TMDB_API_KEY` or `TMDB_API_KEY_FILE` | Metadata discovery credential |
| `OPENAI_API_KEY_FILE` | AI endpoint credential; the page never accepts this secret |
| `VOD_HOST_TRADITIONAL_ENABLED` | Explicit opt-in for the traditional-library sync |
| `VOD_HOST_SOURCE_DIR`, `VOD_HOST_VIDEO_ROOT`, `VOD_HOST_CINEMA_DIR` | Traditional source and output roots |
| `VOD_HOST_JELLYFIN_CONFIG`, `VOD_HOST_JELLYFIN_DB`, `VOD_HOST_JELLYFIN_API` | Traditional Jellyfin paths and endpoint |
| `VOD_HOST_JELLYFIN2_CONFIG`, `VOD_HOST_JELLYFIN2_LOG_DIR`, `VOD_HOST_JELLYFIN2_METADATA_DIR` | Explicit point-of-play host paths |
| `VOD_API_BASE` | API endpoint reachable from the worker; Compose uses `http://quark-vod:8099` |
| `VOD_HOST_RSS_URL` | Explicit recommendation RSS feed; empty means unavailable |
| `VOD_HOST_FFMPEG_BINARY`, `VOD_HOST_FFPROBE_BINARY` | Host task tools, resolved in the task runtime; default `ffmpeg`/`ffprobe` |
| `VOD_HOST_RSS_OUTPUT_DIR` | Writable recommendation output, separate from installed UI resources |

Consult `integrations/hometheater/paths.py` for the authoritative optional path
settings. A container cannot use a host path merely because the path exists on
the host: mount it into the correct container namespace. Files referenced by credential
`*_FILE` variables must be mounted read-only and readable in both services.
The optional Compose profile supplies a worker, not the library mounts and host
tools required by every task. Configure reachable OpenList/Jellyfin endpoints,
share writable STRM/metadata roots, and expose the full external SQLite directory
including its WAL files read-only. Never mount the Docker
socket merely to make an optional host tool available; run that integration on
the host with explicit tooling when necessary.

## Cut over

Keep all new tasks disabled while validating the page and configuration.
Translate the old schedules in Tasks & Models, then disable the corresponding old
cron lines before enabling the new worker. Use exactly one scheduler owner per
data directory. Use the persistent `quark-vod jobs worker --execute` for scheduled
and web-queued work. `worker --once` is for isolated checks: it holds the worker
lock while consuming one task, so a long task would block later cron invocations
from checking their schedules. Do not use a minute cron loop around `--once`.

Preserve useful checkpoints, the existing core state, AI cache and explicit skip
rules through their configured data paths. Install wrapper source with
`python -m quark_vod.integrations.jellyfin --output /path/to/wrappers`, then mount
that directory into Jellyfin and set the encoder path. Third-party FFmpeg, jq,
curl and timeout must be installed separately in the Jellyfin runtime.
The wrapper's `FFMPEG_BINARY` and `FFPROBE_BINARY` select those upstream binaries;
`FFPROBE_JQ` and `VOD_CURL_BINARY` select jq and curl. Set these in Jellyfin's
runtime, separately from the worker's `VOD_HOST_*` probe/tool settings.

Validate configuration reload, queue execution, lock conflicts, process shutdown,
existing STRM playback and one controlled new save. Natural timer execution,
complete media decoding, notifications and actual client playback are separate
acceptance steps. Do not infer them from fixture tests.

## Roll back

Stop the new worker before returning to old scripts or schedules. Restore only
the affected code, image, wrapper and scheduler configuration. Retain the current
state and later user activity. An old complete database must not overwrite saves,
watch history or notification settings made after the backup. Review version and
schema compatibility before restoring a database.
