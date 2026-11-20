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

The service can start without Quark or OpenList account credentials, but management
APIs require a token from `VOD_ADMIN_TOKEN` or `VOD_ADMIN_TOKEN_FILE`. Search
requires an enabled, reachable search provider. Saving a new item requires Quark
authorization and a reachable OpenList integration; playback of an item already
saved in the service state requires that state and an OpenList endpoint reachable
by the service and player. Standalone mode returns ready playback URLs without
generating STRM files; optional Jellyfin mode uses an existing stable STRM library
mapping. No external AI, cloud-write probe, self-healing loop or maintenance is
enabled by default.

## Quick start with Docker

Requires Docker Engine with Compose v2.

Follow [installation and configuration](docs/quickstart.md) to create a private
`.env`, set the required administrator token before first startup, configure
playback-reachable OpenList and Quark-VOD URLs, and then build and start the
service. The supplied Compose file builds from the local source tree; its image
tag is not a promise that a public registry image is available.

The supplied Compose file publishes only on loopback, runs the image as a
non-root user, keeps its root filesystem read-only and limits memory to 256 MiB.
Persistent state is stored in a Compose-managed named volume. Search nodes are
operator-owned; start from `examples/search.example.json` and enable only
sources you operate.

Keep `.env` private and never commit it. URLs in `OPENLIST_PUBLIC` and
`PENDING_BASE_URL` must be reachable by the actual playback client.

## Python installation

Python 3.11+ on Linux is supported. Follow the [native setup guide](docs/quickstart.md)
for a virtual environment, a private native configuration file and the startup
command with management authentication enabled.

The default native data base is `./var`. Use `VOD_BASE_DIR` to choose another
location. Native startup reads an environment file only when `VOD_ENV_FILE` is
explicitly set. The Docker `.env.example` contains a Docker-specific OpenList
address and should not be reused unchanged for native Linux installation; see
the [native setup guide](docs/quickstart.md).

## Reloadable search providers

In a source checkout, copy `examples/search.example.json` to a private
`examples/search.json` and set `VOD_SEARCH_CONFIG` to that file, using the schema
in [the provider guide](docs/plugins.md). The supplied Docker Compose mounts the
whole `examples/` directory as `/config` and reads `/config/search.json`; an
installed wheel has no operator search file and must be given one explicitly.
Changes are checked on subsequent searches; the authenticated reload endpoint can
validate and apply them immediately. An invalid replacement keeps the last valid
snapshot.

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
