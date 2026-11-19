"""HTTP transport for the public cleaner: trusted OpenList or public CDNs.

CDN DNS answers are checked and pinned for each connection. TLS still verifies
the original hostname; environment proxies cannot bypass address validation.
"""
import http.client
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import config


def _origin(value):
    return value.scheme, value.hostname.lower().rstrip("."), value.port or (443 if value.scheme == "https" else 80)


def _parse(url):
    value = urllib.parse.urlsplit(url)
    if (value.scheme not in {"http", "https"} or not value.hostname
            or value.username is not None or value.password is not None or value.fragment
            or any(ord(c) < 33 for c in url)):
        raise ValueError("invalid download URL")
    _origin(value)  # Validate port before opening a connection.
    return value


def _configured_origin(value):
    return any(base and _origin(value) == _origin(_parse(base))
               for base in {config.OPENLIST_PUBLIC, config.OPENLIST_API})


def allowed_openlist_url(url):
    try:
        value = _parse(url)
        decoded = urllib.parse.unquote(value.path)
        if "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
            return False
        for base in {config.OPENLIST_PUBLIC, config.OPENLIST_API}:
            configured = _parse(base)
            if _origin(value) == _origin(configured):
                if decoded.startswith(urllib.parse.unquote(configured.path).rstrip("/") + "/d/"):
                    return True
    except (ValueError, AttributeError):
        pass
    return False


def _public_addresses(host, port):
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    # Reject mixed public/private answers rather than falling back to a private
    # address if a CDN connection fails. IPv4-mapped IPv6 is checked as IPv4.
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        checked = getattr(parsed, "ipv4_mapped", None) or parsed
        if not checked.is_global:
            raise OSError("download redirect address is not public")
    if not addresses:
        raise OSError("download host has no addresses")
    return addresses


class _PinnedConnection:
    def _download_connect(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **kwargs):
        host, port = address
        value = urllib.parse.urlsplit(f"{self.download_scheme}://{host if ':' not in host else '[' + host + ']'}:{port}")
        if _configured_origin(value):
            return socket.create_connection(address, timeout, source_address, **kwargs)
        addresses = _public_addresses(host, port)
        for pinned in addresses:
            try:
                return socket.create_connection((pinned, port), timeout, source_address, **kwargs)
            except OSError as exc:
                last_error = exc
        raise last_error

    def connect(self):
        self._create_connection = self._download_connect
        super().connect()


class _HTTPConnection(_PinnedConnection, http.client.HTTPConnection):
    download_scheme = "http"


class _HTTPSConnection(_PinnedConnection, http.client.HTTPSConnection):
    download_scheme = "https"


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(_HTTPConnection, request)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_HTTPSConnection, request, context=self._context)


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        try:
            value = _parse(newurl)
            if _configured_origin(value) and not allowed_openlist_url(newurl):
                raise ValueError("OpenList redirect is outside download paths")
        except (ValueError, AttributeError):
            raise urllib.error.URLError("download redirect is not allowed") from None
        return super().redirect_request(request, fp, code, message, headers, newurl)


def urlopen(request, timeout=20):
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    if not allowed_openlist_url(url):
        raise urllib.error.URLError("download origin is not allowed")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler(), _HTTPSHandler(), _RedirectHandler())
    return opener.open(request, timeout=timeout)
