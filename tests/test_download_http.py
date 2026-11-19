"""Real redirects and connection pinning; no external network or credentials."""
import http.server
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="vod-download-")
os.environ.update(VOD_BASE_DIR=TMP.name, VOD_ENV_FILE="", AI_PARSE_ENABLED="0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from quark_vod import download_http


class Downloads(unittest.TestCase):
    def setUp(self):
        self.hits = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.hits.append((self.server.server_port, self.path))
                redirects = {"/d/same": "/d/video", "/d/private": outer.other + "/secret",
                             "/d/admin": "/api/admin", "/d/file": "file:///etc/passwd"}
                if self.path in redirects:
                    self.send_response(302)
                    self.send_header("Location", redirects[self.path])
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", "5")
                self.end_headers()
                self.wfile.write(b"video")

        self.servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
        for server in self.servers:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{self.servers[0].server_port}"
        self.other = f"http://127.0.0.1:{self.servers[1].server_port}"
        patches = [patch.object(download_http.config, key, self.base) for key in ("OPENLIST_API", "OPENLIST_PUBLIC")]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_trusted_openlist_redirect_works_without_environment_proxy(self):
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1"}):
            with download_http.urlopen(self.base + "/d/same") as response:
                self.assertEqual(response.read(), b"video")
        self.assertEqual([path for _, path in self.hits], ["/d/same", "/d/video"])

    def test_redirects_cannot_reach_another_private_service_or_openlist_admin(self):
        for suffix in ("private", "admin", "file"):
            with self.subTest(suffix=suffix), self.assertRaises(urllib.error.URLError):
                download_http.urlopen(self.base + "/d/" + suffix)
        self.assertEqual([path for _, path in self.hits], ["/d/private", "/d/admin", "/d/file"])

    def test_untrusted_first_hop_is_rejected_without_request(self):
        with self.assertRaises(urllib.error.URLError):
            download_http.urlopen(self.other + "/d/video")
        self.assertEqual(self.hits, [])

    def test_dns_mixed_or_mapped_private_addresses_are_rejected(self):
        for addresses in [("1.1.1.1", "127.0.0.1"), ("::ffff:127.0.0.1",), ("169.254.169.254",)]:
            records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80)) for address in addresses]
            with self.subTest(addresses=addresses), patch.object(socket, "getaddrinfo", return_value=records), \
                 patch.object(socket, "create_connection") as connected:
                connection = download_http._HTTPConnection("cdn.example.invalid")
                with self.assertRaises(OSError):
                    connection.connect()
                connected.assert_not_called()

    def test_public_dns_is_pinned_at_connect_and_original_tls_hostname_is_preserved(self):
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        with patch.object(socket, "getaddrinfo", return_value=records), \
             patch.object(socket, "create_connection") as connected:
            connection = download_http._HTTPSConnection("cdn.example.invalid")
            context = connection._context
            with patch.object(context, "wrap_socket") as wrap:
                connection.connect()
                self.assertEqual(connected.call_args.args[0], ("1.1.1.1", 443))
                self.assertEqual(wrap.call_args.kwargs["server_hostname"], "cdn.example.invalid")
                self.assertEqual(connection.host, "cdn.example.invalid")


if __name__ == "__main__":
    unittest.main()
