# Security boundaries

Quark-VOD is a single-process playback service with optional Quark, OpenList,
Jellyfin and search-provider integrations. The service is intended to run on a
trusted home network, behind a firewall or VPN, or behind an authenticated
gateway. Its management token protects operator APIs; it does not turn playback
URLs into a multi-user authentication system.

## Authentication and network access

The management Bearer token is supplied through `VOD_ADMIN_TOKEN` or
`VOD_ADMIN_TOKEN_FILE`. It protects every `/api/admin/*` route and the following
core routes:

- `/stats`
- `/capability`
- `/preheat`

When no token is configured, those routes return `503` with
`admin_not_configured`. A missing, malformed or incorrect Bearer token returns
`401`. Authentication is checked before the protected handler runs.

The following built-in routes are intentionally reachable without that token:

- `/health`, `/` and `/admin`
- `/docs`, `/redoc`, `/openapi.json` and `/docs/oauth2-redirect`
- `/play`, `/pending`, `/media/{transfer_id}` and `/clean`

The documentation and schema routes expose the API shape, but they do not make
the management handlers unauthenticated. The `/admin` page is a UI shell; its
management API calls still require the Bearer token. The playback routes can
expose saved media to any client that can reach the listener, and `/play` may
start the configured search and transfer workflow. The `v` query value used by
`/media/{transfer_id}` identifies a source version; it is not an access
credential. Keep the listener private or restrict it with a firewall, VPN or
authenticated reverse proxy. Use TLS when credentials or playback requests
cross an untrusted network.

## Signed URLs and access logs

`/clean` receives the upstream OpenList URL in a query parameter. That URL may
contain a short-lived signature. The supported `quark-vod` and compatibility
server entry points disable Uvicorn access logging, and the stream-cleaning log
message omits the source and generated URLs. A reverse proxy, container runtime,
custom Uvicorn invocation or request tracing middleware can still log the full
request target. Treat access logs as sensitive, disable or redact query strings
for `/clean`, restrict log readers and use a short retention period.

Do not put Quark cookies, OpenList credentials, Jellyfin keys, management tokens
or signed media URLs in source control, issue reports, screenshots, shared chat,
or test fixtures. The management page keeps the entered token in page memory;
playback endpoints remain separately unauthenticated.

## The `/clean` proxy

`/clean` is an unauthenticated streaming proxy, not an arbitrary URL fetcher.
The first request must use the configured `OPENLIST_PUBLIC` or `OPENLIST_API`
origin and a `/d/` download path under that origin. Dot-segments, encoded
path traversal, credentials in the URL and fragments are rejected. A configured
OpenList origin is an operator trust decision and may be a local or private
address.

Redirects from a configured origin remain restricted to its download paths.
Redirects to another origin are accepted only when every DNS result is globally
routable. The transport pins a checked address for the connection, preserves the
original TLS hostname for certificate validation and disables environment proxy
settings. A reachable client can still consume upstream and local bandwidth, so
network access to this route must be restricted.

The cleaner forwards only selected `User-Agent` and `Accept` headers. It does
not forward a caller's `Cookie` or `Authorization` header. It translates one
byte range at a time and reads the body lazily in chunks of at most 128 KiB.
There is no total media-size limit in this proxy; use network and upstream
limits appropriate for the deployment. The 2 MiB response limit used by some
search and coordination requests does not apply to media streaming.

## Search providers and integrations

JSON search configuration selects installed providers and supplies their HTTP
destinations. A configured destination can intentionally be a local or private-
network service; this is an administrator-authorized network boundary, not a
general SSRF protection layer. Only trusted administrators should edit the
configuration, and destinations should be reviewed for the service host.

Python providers loaded through the `quark_vod.search` entry point execute in the
service process when explicitly enabled by `VOD_SEARCH_PLUGINS`. They are trusted
code, not a sandbox: they can use the service account's filesystem, network and
credentials. Install providers only from sources trusted with those privileges.
Changing JSON nodes can be reloaded; installing or updating provider code
requires a process restart.

Quark/OpenList saving is the current storage integration. Jellyfin integration,
AI filename assistance, startup write probes, background self-healing and
destructive maintenance are explicit options and are disabled by default in a
standalone installation.

## Maintenance and deletion

Destructive maintenance must be explicitly enabled and uses the configured
protection integration. All maintenance mutations use the maintenance lock and
fail closed when a required protection or remote-enumeration check is missing,
unreadable, structurally invalid or incomplete. Individual jobs then apply their
relevant checks, such as
Jellyfin sessions and user state, pinned/recent access, or metadata references,
before changing anything. The OpenList remove call delegates deletion semantics
to that backend; Quark-VOD does not assume that a removed object is recoverable
or placed in a recycle bin.

Quota eviction treats favorites, unfinished Jellyfin playback, explicit user
pins, and playback during the last six hours as protected. Completed watch
history and automatic preheat/import/playback markers do not create permanent
exemptions. Eligible items are evicted least-recently-used first from
`playback_access`, with legacy `plays` timestamps as a fallback; items with no
access history come first, and larger items break timestamp ties.

## Secret storage

Prefer private environment files or explicitly mounted secret files. Keep those
files outside source control and limit their permissions at deployment time;
when both a normal variable and its `_FILE` form are set, the normal variable
takes precedence, so avoid ambiguous configurations. The application reads
secret-file values but does not change permissions on an arbitrary externally
mounted input file. Its Quark Cookie override is written with mode `0600` and a
`0700` parent directory; missing application data/log directories are created
with restrictive modes, while existing directory modes are left to the
operator.

## Vulnerability reports

Please report suspected vulnerabilities privately through the repository
maintainer's security contact or the repository's private vulnerability-reporting
channel when one is available. Do not publish a reproduction, credentials,
private share links, signed media URLs, database dumps or personal media in a
public issue. If no private channel is visible, open an issue containing only a
request for a private contact method and no technical details; move the report
to that channel once provided.
