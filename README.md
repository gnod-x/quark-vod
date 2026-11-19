# Quark-VOD

A portable Quark on-demand playback service with reloadable search providers,
precise single-file transfers, STRM output and bounded HTTP Range streaming.

Quark-VOD can run as a Python package or a Docker service. Search nodes are
operator-owned configuration. Jellyfin, AI filename assistance and destructive
maintenance are explicit opt-ins.

## What is included

- HTML and JSON search providers, configurable independently of playback.
- Atomic search configuration reload: new requests use the new snapshot while
  running requests finish with their original snapshot.
- Exact movie/episode identity matching before quality ranking.
- Durable transfer intents to recover ambiguous upstream results.
- Stable `/play` STRM links and bounded streaming caches.
- Token-protected management APIs and a packaged administration page.
- A standalone data directory with no required home-server layout.

The service starts without account credentials. Actual saving/playback requires
your Quark authorization and a reachable OpenList integration. Standalone mode
returns ready playback URLs without generating STRM files; optional Jellyfin
mode uses an existing stable STRM library mapping. No external AI,
cloud-write probe, self-healing loop or maintenance is enabled by default.

## Quick start with Docker

Requires Docker Engine with Compose v2.

```sh
cp .env.example .env
docker compose up -d --build
curl --fail http://127.0.0.1:8099/health
```

The supplied Compose file publishes only on loopback, runs the image as a
non-root user, keeps its root filesystem read-only and limits memory to 256 MiB.
Persistent state is stored in the `quark_vod_data` volume. The initial
`examples/search.json` contains no enabled nodes.

Set a private `VOD_ADMIN_TOKEN` in your local `.env` before using the management
APIs. Configure your OpenList addresses and Quark authorization locally. Never
commit that file. URLs in `OPENLIST_PUBLIC` and `PENDING_BASE_URL` must be
reachable by the actual playback client.

See [installation and configuration](docs/quickstart.md).

## Python installation

Python 3.11+ on Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
quark-vod --help
quark-vod --host 127.0.0.1 --port 8099
```

The default native data base is `./var`. Use `VOD_BASE_DIR` to choose another
location. Native startup reads an environment file only when `VOD_ENV_FILE` is
explicitly set.

## Reloadable search providers

Edit `examples/search.json` using the schema in
[the provider guide](docs/plugins.md). Changes are checked on subsequent searches;
the authenticated reload endpoint can validate and apply them immediately.
An invalid replacement keeps the last valid snapshot.

Built-in `html` and `json` providers require no code installation. Trusted Python
providers can be registered through `quark_vod.search` entry points and explicitly
enabled with `VOD_SEARCH_PLUGINS`. Installing new Python code requires a process
restart; changing the nodes of an installed provider does not.

## Integration boundaries

| Integration | Default | Required for |
|---|---|---|
| Quark authorization | Empty | Real share inspection and saving |
| OpenList | Unconfigured | File resolution and playback |
| Jellyfin | Disabled | Optional library/session protection and refresh |
| AI filename assistance | Disabled | Optional weak-name arbitration |
| Destructive maintenance | Disabled | Explicit protected maintenance |
| Startup write probe / background self-heal | Disabled | Explicit operational checks |

Write and playback mounts may be different paths for the same account. Both are
configurable. No fixed speed guarantee is associated with a particular driver.
Unknown quality is never a proof of HD/4K.

## Development

```sh
python -m pip install -e '.[test]'
make test
python -m build
docker build -t quark-vod:3.0.0 .
```

`make test` runs isolated core and plugin/configuration/API regressions without a
sibling home-server repository or real cloud credentials. Optional legacy
home-server compatibility checks are separate: `make test-homelab`.

Read [architecture](docs/architecture.md), [contributing](CONTRIBUTING.md) and
[security boundaries](SECURITY.md). The package, source distribution and image
exclude legacy bundled FFmpeg/jq binaries.

## License

[MIT](LICENSE). Third-party dependencies retain their original licenses; see
[NOTICE](NOTICE).
