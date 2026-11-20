# Security boundaries

Management endpoints require an operator-configured token. Keep the token in a
private environment or explicit secret file. Use authenticated TLS access when
management crosses a trusted local network. The example Compose publishes on
loopback.

Playback endpoints are intended for trusted media clients. Do not treat the
management token as a multi-user authorization system for every media URL.
Signed upstream URLs, drive cookies and account data must not be included in
public reports.

The byte-cleaning endpoint accepts initial download URLs only under the
configured OpenList origin's `/d/` path. Subsequent CDN redirects must resolve
to global addresses; the transport pins a checked address while preserving the
original TLS hostname. It ignores environment proxy settings for this route.
This prevents arbitrary internal-network redirects through the media cleaner.

Python search providers are trusted code installed by the operator. JSON node
configuration does not accept uploaded code. Enable only providers and network
destinations appropriate for the deployment. The process is not a sandbox for
untrusted third-party plugins.

Maintenance is disabled by default and requires explicit protection integration.
A missing or unavailable protection database/session check must stop deletion.

For a suspected vulnerability, preserve a minimal synthetic reproduction and
contact the repository maintainer privately. Do not open an issue containing
credentials, private shares, database dumps or personal media.
