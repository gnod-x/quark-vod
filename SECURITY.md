# Security boundaries

Quark-VOD is a single-process playback service with optional account and media-library integrations. Its management token protects operator APIs; it does not authenticate every playback URL or turn the service into a multi-user gateway.

## Authentication and network access

The Bearer token is supplied through `VOD_ADMIN_TOKEN` or `VOD_ADMIN_TOKEN_FILE`. It protects `/api/admin/*`, `/stats`, `/capability` and `/preheat`. When no token is configured, management APIs are disabled and return `503`; a wrong token returns `401`.

The following routes do not require that Bearer token:

- `/health`, `/`, `/admin`, `/docs` and `/openapi.json`.
- `/play`, `/pending`, `/media/{transfer_id}` and `/clean`.

Playback routes can expose saved media to any client that can reach the service. A `/play` request can also start the configured search and transfer workflow. The `v` query value used by `/media/{transfer_id}` identifies a source version; it is not an access credential. Keep the listener on loopback or restrict it to trusted clients with a firewall, VPN or authenticated gateway. Use TLS when credentials or playback requests cross an untrusted network.

`/clean` is an unauthenticated streaming proxy, not an arbitrary URL fetcher. It accepts URLs only under the configured OpenList origin's download path (`/d/`, including any configured base-path prefix). Redirects to other hosts must resolve exclusively to globally routable addresses; the transport pins a checked address while preserving the TLS hostname and ignores environment proxy settings. A reachable client can still consume proxy bandwidth, so restrict network access to this route as well.

## Search providers and integrations

Search JSON selects installed providers and supplies their HTTP destinations. It does not execute uploaded code, but a configured destination can be a local or private-network service. Only trusted administrators should edit the search configuration, and they should use destinations approved for the service host.

Python providers installed through the `quark_vod.search` entry point execute in the service process when explicitly allowlisted by `VOD_SEARCH_PLUGINS`. They are trusted code, not a sandbox. Install providers only from sources trusted with the service account's filesystem, network and credentials.

Quark cookies, OpenList credentials, Jellyfin keys and management tokens must stay in private environment files or explicitly mounted secret files. Do not put them in source control, issue reports, screenshots, URLs shared with others or test fixtures. Use synthetic values in tests. The management page keeps its entered token in page memory; the playback endpoints remain separately unauthenticated.

Destructive maintenance is disabled by default. Enable it only with the intended protection integration configured. If the Jellyfin protection database, session state or required enumeration is missing or unavailable, maintenance must stop rather than infer that media is safe to delete.

## Vulnerability reports

As of 2026-11-20, GitHub private vulnerability reporting is disabled for this repository, and the maintainer profile publishes no email address. Do not publish vulnerability details in an issue. Open a public issue only to request a private contact channel, without a reproduction, affected URL, credentials or other technical details; send the report after a private channel is provided. If private vulnerability reporting is enabled later, use the repository's Security advisories page instead.

Do not attach credentials, private share links, signed media URLs, database dumps or personal media to public reports.
