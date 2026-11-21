"""Synthetic concurrency, bounded API bodies, and search cache lifecycle."""
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="vod-client-cache-")
os.environ.update(VOD_BASE_DIR=TMP.name, VOD_DATA_DIR=TMP.name, AI_PARSE_ENABLED="0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from quark_vod import openlist_client as client, panshare_matrix as matrix


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())


class ClientTests(unittest.TestCase):
    def client(self):
        obj = client.OpenListClient.__new__(client.OpenListClient)
        obj.base = "http://fixture.invalid"
        obj.user = "synthetic-user"
        obj.password = "synthetic-password"
        obj.token = "synthetic-old"
        obj._login_lock = threading.Lock()
        return obj

    def test_parallel_unauthorized_requests_share_one_refresh(self):
        for http_error in (False, True):
            with self.subTest(http_error=http_error):
                obj = self.client()
                initial = threading.Barrier(4)
                logins = []
                def open_response(request, **kwargs):
                    if request.full_url.endswith("/api/auth/login"):
                        logins.append(1)
                        return Response({"code": 200, "data": {"token": "synthetic-new"}})
                    if request.get_header("Authorization") == "synthetic-old":
                        initial.wait(timeout=3)
                        if http_error:
                            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO())
                        return Response({"code": 401})
                    self.assertEqual(request.get_header("Authorization"), "synthetic-new")
                    return Response({"code": 200, "data": {"content": []}})
                with patch.object(client, "_api_urlopen", side_effect=open_response):
                    with concurrent.futures.ThreadPoolExecutor(4) as pool:
                        results = list(pool.map(lambda _: obj.list("/fixture"), range(4)))
                self.assertEqual(results, [[], [], [], []])
                self.assertEqual(len(logins), 1)

    def test_api_login_and_upload_redirects_never_forward_credentials(self):
        sink_hits = []
        class Sink(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                sink_hits.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"code":200,"data":{}}')
            do_POST = do_PUT = do_GET
        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        class Source(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/sink")
                self.end_headers()
            do_PUT = do_POST
        source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        for server in (sink, source):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{source.server_port}"
        obj = self.client()
        obj.base = base
        with self.assertRaises(client.OpenListError):
            obj._request("/api/fs/list", {})
        with self.assertRaises(client.OpenListError):
            obj.put("/fixture", b"data")
        with self.assertRaises(client.OpenListError):
            client.OpenListClient(base, "synthetic-user", "synthetic-password")
        self.assertEqual(sink_hits, [])

    def test_failed_refresh_keeps_old_token_and_propagates(self):
        obj = self.client()
        responses = [
            Response({"code": 401}),
            Response({"code": 500, "message": "synthetic-private-secret"}),
        ]
        with patch.object(client, "_api_urlopen", side_effect=responses) as opened:
            with self.assertRaises(client.OpenListError) as failure:
                obj.list("/fixture")
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(obj.token, "synthetic-old")
        self.assertNotIn("synthetic-private-secret", str(failure.exception))

    def test_second_unauthorized_response_does_not_retry_again(self):
        obj = self.client()
        responses = [
            Response({"code": 401}),
            Response({"code": 200, "data": {"token": "synthetic-new"}}),
            Response({"code": 401}),
        ]
        with patch.object(client, "_api_urlopen", side_effect=responses) as opened:
            with self.assertRaises(client.OpenListError):
                obj.list("/fixture")
        self.assertEqual(opened.call_count, 3)

    def test_malformed_response_is_an_error_without_body_disclosure(self):
        obj = self.client()
        with patch.object(client, "_api_urlopen", return_value=io.BytesIO(b"synthetic-private-secret")):
            with self.assertRaises(client.OpenListError) as failure:
                obj.list("/fixture")
        self.assertNotIn("synthetic-private-secret", str(failure.exception))

    def test_response_size_is_bounded_and_failure_is_not_an_empty_list(self):
        obj = self.client()
        class Oversized(io.BytesIO):
            def read(self, size=-1):
                self.requested = size
                return b"x" * size
        response = Oversized()
        with patch.object(client, "MAX_RESPONSE_BYTES", 32), \
             patch.object(client, "_api_urlopen", return_value=response):
            with self.assertRaises(client.OpenListError):
                obj.list("/fixture")
        self.assertEqual(response.requested, 33)
        self.assertTrue(response.closed)

    def test_upload_response_is_bounded_too(self):
        obj = self.client()
        response = io.BytesIO(b"x" * 33)
        with patch.object(client, "MAX_RESPONSE_BYTES", 32), \
             patch.object(client, "_api_urlopen", return_value=response):
            with self.assertRaises(client.OpenListError):
                obj.put("/fixture", b"data")
        self.assertTrue(response.closed)

    def test_errors_do_not_echo_response_secrets_and_close_http_error(self):
        obj = self.client()
        response = io.BytesIO(b"synthetic-private-secret")
        error = urllib.error.HTTPError(obj.base, 500, "synthetic-private-secret", {}, response)
        with patch.object(client, "_api_urlopen", side_effect=error):
            with self.assertRaises(client.OpenListError) as failure:
                obj._request("/fixture")
        self.assertNotIn("synthetic-private-secret", str(failure.exception))
        self.assertTrue(response.closed)
        with self.assertRaises(client.OpenListError) as failure:
            obj._ok({"code": 500, "message": "synthetic-private-secret"}, "fixture")
        self.assertNotIn("synthetic-private-secret", str(failure.exception))
        with self.assertRaises(client.OpenListError) as failure:
            obj._ok({"code": "synthetic-private-secret"}, "fixture")
        self.assertNotIn("synthetic-private-secret", str(failure.exception))


class SearchCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vod-cache-lifecycle-")
        self.root = Path(self.temp.name)
        self.patch = patch.object(matrix, "SEARCH_CACHE_DIR", self.temp.name)
        self.patch.start()
        matrix._mem_cache.clear()
        matrix._cache_sweep_at = 0
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.patch.stop)

    def test_write_sweeps_old_owned_files_but_preserves_recent_foreign_and_symlinks(self):
        old = self.root / ("ps4_" + "a" * 32 + ".json")
        recent = self.root / ("ps4_" + "b" * 32 + ".json")
        foreign = self.root / "other.json"
        for path in (old, recent, foreign):
            path.write_text("{}")
        then = time.time() - matrix.SEARCH_CACHE_TTL - 10
        for path in (old, foreign):
            os.utime(path, (then, then))
        link = self.root / ("ps4_" + "c" * 32 + ".json")
        link.symlink_to(foreign)
        key = matrix._cache_key("synthetic")
        matrix._cache_put(key, {"items": [], "partial": True})
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(foreign.exists())
        self.assertTrue(link.is_symlink())
        self.assertIsNotNone(matrix._cache_get(key))

    def test_expired_disk_hit_removes_disk_and_memory_copy(self):
        key = matrix._cache_key("expired")
        path = Path(matrix._cache_path(key))
        path.write_text(json.dumps({"magic": matrix._CACHE_MAGIC, "expire": 1, "payload": {}}))
        self.assertIsNone(matrix._cache_get(key))
        self.assertFalse(path.exists())
        self.assertNotIn(key, matrix._mem_cache)

    def test_write_sweep_is_throttled(self):
        matrix._cache_put(matrix._cache_key("first"), {"items": [], "partial": True})
        with patch.object(matrix.os, "scandir") as scan:
            matrix._cache_put(matrix._cache_key("second"), {"items": [], "partial": True})
        scan.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
