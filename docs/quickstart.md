# Installation and configuration

## Start an empty standalone instance

Copy `.env.example` to a private `.env`, then run:

```sh
docker compose up -d --build
curl --fail http://127.0.0.1:8099/health
```

An empty configuration is a valid setup state. It does not prove Quark
authorization or playback readiness. Add search providers and credentials before
requesting a real transfer.

The container listens on 8099. `VOD_PORT` in the Compose `.env` selects the host
port; `VOD_PUBLISH_ADDRESS` defaults to 127.0.0.1. Change it only for the intended
network and configure an appropriate authenticated reverse proxy.

## Local configuration

| Variable | Meaning |
|---|---|
| `VOD_BASE_DIR` | Native state base; defaults to current directory + `var`; Compose fixes it to `/data` |
| `VOD_DATA_DIR`, `VOD_LOG_DIR`, `VOD_CINEMA2_DIR` | Optional directory overrides |
| `VOD_ENV_FILE` | Explicit environment file for native startup |
| `VOD_ADMIN_TOKEN` / `VOD_ADMIN_TOKEN_FILE` | Management authentication |
| `VOD_SEARCH_CONFIG` | Versioned search configuration JSON |
| `VOD_SEARCH_PLUGINS` | Comma-separated allowlist of installed provider entry points |
| `OPENLIST_API` | Server-reachable OpenList API |
| `OPENLIST_PUBLIC` | OpenList URL reachable by the playback client |
| `PENDING_BASE_URL` | Quark-VOD URL reachable by the playback client |
| `OPENLIST_ADMIN_USER`, `OPENLIST_ADMIN_PASS` | OpenList login |
| `QUARK_COOKIE` | Quark authorization |
| `QUARK_MOUNT`, `QUARK_PLAY_MOUNT` | Write and read mounts for the same account |
| `OPENLIST_DB_PATH` | Explicit optional read-only credential inheritance source |
| `VOD_JELLYFIN_ENABLED` | Enables Jellyfin integration; default false |
| `JELLYFIN2_API_BASE`, `JELLYFIN2_KEY`, `JELLYFIN2_DB` | Explicit Jellyfin connection and protection data |
| `VOD_MAINTENANCE_ENABLED` | Enables guarded maintenance; default false |
| `VOD_STARTUP_SCAN`, `VOD_STARTUP_WRITE_PROBE` | Startup operational opt-ins; default false |
| `SELFHEAL_ENABLED` | Background self-healing; default false |
| `AI_PARSE_ENABLED` | Optional weak-name arbitration; default false |
| `AI_PARSE_BASE_URL`, `AI_PARSE_MODEL`, `OPENAI_API_KEY` | Explicit optional model configuration |

Secret variables support corresponding `_FILE` variables. Mount those files
read-only using your own Compose override. Process environment values take
precedence over explicit secret files, followed by the explicit environment file.
The persisted administrator cookie override is deliberately the active Quark
cookie until cleared.

Do not mount a live database as a single bare file when it uses WAL. Its
directory and WAL/SHM files must be available read-only. Absence of protection
data blocks destructive maintenance; it is not interpreted as an idle library.

OpenList on the Docker host can be reached through `host.docker.internal` when
its listener is reachable from the bridge. A host-only loopback listener needs
an explicit network arrangement; container localhost is not host localhost.

## Search configuration

The supplied `examples` directory is mounted read-only at `/config`;
`examples/search.json` is therefore available as `/config/search.json`. Start from `examples/search.example.json`, replace the
placeholder addresses and enable only nodes you operate or intend to use.

Compose mounts the whole configuration directory so atomic host file replacement
is visible to the container. Keep a directory mount in custom deployments too;
a single-file bind mount can pin the old inode. Search configuration validation
preserves the last good snapshot.

See [provider schema and reload behavior](plugins.md).

## Native Python deployment

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
export VOD_BASE_DIR="$PWD/var"
export VOD_ENV_FILE="$PWD/.env"
export VOD_SEARCH_CONFIG="$PWD/examples/search.json"
quark-vod --host 127.0.0.1 --port 8099
```

Use one worker. SQLite state, in-process transfer sharing, media cache capacity
and plugin snapshots are designed around one service process.

## Standalone and Jellyfin playback

With Jellyfin disabled, successful resolution returns the ready playback URL
directly without creating STRM files. This mode still needs configured search,
Quark authorization and OpenList to save and read actual media.

Jellyfin mode is an optional library integration. The requested movie/episode
must already have a stable STRM mapping; use the library integration workflow
before requesting playback. A missing mapping is an integration error rather
than evidence that the cloud has no matching resource.

## Validate before exposing real playback

1. Check `/health` and the management authentication boundary.
2. Apply an intentionally empty provider configuration; ensure it is observable
   as unconfigured rather than a global resource-absence claim.
3. Verify an operator-controlled provider and authenticated Quark/OpenList setup.
4. Test a permitted single title/file, start playback and seek.
5. Back up the state database with SQLite's online backup API, plus relevant
   STRM/NFO/configuration files before upgrades.

Health, account capability, a completed transfer, successful byte reads and
device playback are separate results. Keep your local acceptance evidence.

## Upgrade and rollback

Build a versioned wheel/image, preserve the old image digest and back up state
consistently before replacement. Stop new work and confirm there are no active
transfers or playback before deploying. Do not replace current user state with
an older complete database after new activity has occurred.

Legacy home-server jobs and media-server probe wrappers are optional integration
tools, not dependencies of the public package. Their local deployment and
cross-repository synchronization must be verified separately.
