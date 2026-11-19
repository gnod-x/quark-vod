"""Real local HTTP hot-swap regressions; no credentials or public services."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

BOOT = tempfile.TemporaryDirectory(prefix="vod-plugins-boot-")
os.environ.update(VOD_BASE_DIR=BOOT.name, VOD_DATA_DIR=BOOT.name + "/data",
                  AI_PARSE_ENABLED="0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from quark_vod import search_plugins as plugins, panshare_matrix as matrix, search


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        self.server.calls.append((parsed.path, parse_qs(parsed.query)))
        if parsed.path == "/slow":
            self.server.entered.set()
            self.server.release.wait(4)
        if parsed.path.startswith("/html/index.php/"):
            body = b'<a href="/voddetail/1.html">fixture</a>'
            mime = "text/html"
        elif parsed.path == "/html/voddetail/1.html":
            body = b"<title>Sample 2160p</title>https://pan.quark.cn/s/html1"
            mime = "text/html"
        elif parsed.path == "/bad":
            body = b'{"error":"never expose upstream secret text"}'
            mime = "application/json"
        else:
            marker = "aaa" if parsed.path in ("/a", "/slow") else "bbb"
            body = json.dumps({"items": [{"name": "Sample 4K",
                "share_url": "https://pan.quark.cn/s/" + marker}]}).encode()
            mime = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class SearchPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.server.calls = []
        cls.server.entered = threading.Event()
        cls.server.release = threading.Event()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vod-plugin-case-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "search.json"
        self.registry = plugins.SearchRegistry(self.path)
        self.registry_patch = patch.object(plugins, "get_registry", return_value=self.registry)
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.cache_patch = patch.object(matrix, "SEARCH_CACHE_DIR", self.temp.name + "/cache")
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        matrix._mem_cache.clear()
        dead_patch = patch.object(matrix.db, "get_dead_sources_set", return_value=set())
        dead_patch.start()
        self.addCleanup(dead_patch.stop)
        self.server.calls.clear()
        self.server.entered.clear()
        self.server.release.clear()

    def node(self, identifier="fixture", path="/a", **extra):
        return {"id": identifier, "name": identifier, "provider": "json",
                "base": self.base, "path": path, **extra}

    def write(self, nodes):
        replacement = self.path.with_suffix(".new")
        replacement.write_text(json.dumps({"version": 1, "nodes": nodes}))
        os.replace(replacement, self.path)

    def query(self, keyword="Sample", **kwargs):
        return matrix.PanshareMatrix.search_report(keyword, **kwargs)

    def test_html_detail_budget_does_not_truncate_share_rows(self):
        rows = [{"name": "Sample 4K", "share_url": "https://pan.quark.cn/s/a" + str(i)}
                for i in range(40)]
        with patch.object(matrix, "_fetch_node", return_value=(rows, {"errors": 0})):
            found, report = matrix._fetch_provider({"key": "html", "provider": "html"}, "Sample", 24, 3)
        self.assertEqual(len(found), 40)
        self.assertEqual(report["candidates"], 40)

    def test_no_configuration_is_incomplete_not_empty_success(self):
        self.registry = plugins.SearchRegistry()
        result = self.query(use_cache=False)
        self.assertFalse(result["configured"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["items"], [])
        with self.assertRaises(plugins.PluginConfigError):
            self.registry.reload()

    def test_real_json_and_html_provider(self):
        html = self.node("html", provider="html", base=self.base + "/html")
        html.pop("path")
        self.write([self.node(), html])
        result = self.query(use_cache=False)
        self.assertFalse(result["partial"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual({r["provider"] for r in result["nodes"]}, {"json", "html"})

    def test_hot_add_disable_remove_without_restart(self):
        self.write([self.node("a")])
        first = self.query(use_cache=False)
        fingerprint = first["config_fingerprint"]
        self.assertEqual(first["items"][0]["source"], "a")
        self.write([self.node("a", enabled=False), self.node("b", path="/b")])
        second = self.query(use_cache=False)
        self.assertEqual(second["items"][0]["source"], "b")
        self.assertNotEqual(second["config_fingerprint"], fingerprint)
        self.write([])
        self.assertFalse(self.query(use_cache=False)["configured"])
        self.assertEqual(len(self.server.calls), 2)

    def test_new_fingerprint_cannot_hit_previous_node_cache(self):
        self.write([self.node("a")])
        first = self.query()
        again = self.query()
        self.assertFalse(first["cached"])
        self.assertTrue(again["cached"])
        self.assertEqual(len(self.server.calls), 1)
        self.write([self.node("b", path="/b")])
        changed = self.query()
        self.assertFalse(changed["cached"])
        self.assertEqual(changed["items"][0]["share_url"], "https://pan.quark.cn/s/bbb")
        self.assertEqual(len(self.server.calls), 2)

    def test_invalid_auto_reload_retains_last_good_snapshot(self):
        self.write([self.node()])
        good = self.query()
        self.path.write_text('{"version":1,"nodes":[')
        invalid = self.query()
        self.assertEqual(invalid["items"], good["items"])
        self.assertTrue(invalid["partial"])
        self.assertEqual(invalid["configuration_error"], "search_config_invalid_json")
        self.assertEqual(invalid["config_fingerprint"], good["config_fingerprint"])
        self.path.unlink()
        self.assertEqual(self.registry.snapshot().fingerprint, good["config_fingerprint"])
        self.assertEqual(self.registry.status()["error"], "search_config_unavailable")

    def test_explicit_invalid_reload_raises_without_replacing(self):
        self.write([self.node()])
        before = self.registry.snapshot()
        self.write([self.node(), self.node()])
        with self.assertRaises(plugins.PluginConfigError):
            self.registry.reload()
        self.assertIs(self.registry.snapshot(), before)

    def test_schema_rejects_invalid_values_and_never_echoes_secret(self):
        self.write([self.node()])
        previous = self.registry.snapshot()
        cases = [
            [self.node(base="https://user:topsecret@example.invalid")],
            [self.node(base="file:///private/file")],
            [self.node(base="https://example.invalid?token=topsecret")],
            [self.node(timeout=True)], [self.node(timeout=0)],
            [self.node(timeout=8)], [self.node(timeout=float("nan"))],
            [self.node(enabled="false")], [self.node(provider="not-installed")],
            [self.node(id="bad id")], [self.node(path="//other.example/search")],
            [self.node(query_param="bad&token")],
            [self.node(unknown="field")],
            [self.node(str(i)) for i in range(33)],
        ]
        for nodes in cases:
            with self.subTest(nodes=nodes):
                self.write(nodes)
                with self.assertRaises(plugins.PluginConfigError) as error:
                    self.registry.reload()
                self.assertNotIn("topsecret", str(error.exception))
                self.assertEqual(self.registry.snapshot().fingerprint, previous.fingerprint)

    def test_in_flight_request_keeps_its_snapshot(self):
        self.write([self.node("old", path="/slow")])
        results = {}
        def request():
            results["old"] = self.query(use_cache=False)
        worker = threading.Thread(target=request)
        worker.start()
        self.assertTrue(self.server.entered.wait(2))
        try:
            self.write([self.node("new", path="/b")])
            self.registry.reload()
            current = self.query(use_cache=False)
            self.assertEqual(current["items"][0]["source"], "new")
        finally:
            self.server.release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results["old"]["items"][0]["source"], "old")
        self.assertNotEqual(results["old"]["config_fingerprint"], current["config_fingerprint"])

    def test_provider_failure_isolated_and_error_text_not_leaked(self):
        self.write([self.node("bad", path="/bad"), self.node("good")])
        result = self.query(use_cache=False)
        self.assertTrue(result["partial"])
        self.assertEqual(len(result["items"]), 1)
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(next(n for n in result["nodes"] if n["node"] == "bad")["errors"], 1)

    def test_public_status_contains_no_endpoint(self):
        self.write([self.node()])
        status = self.registry.status()
        self.assertTrue(status["configured"])
        self.assertEqual(status["enabled_count"], 1)
        self.assertNotIn(self.base, json.dumps(status))
        self.assertNotIn("path", status["nodes"][0])
        with self.assertRaises(Exception):
            self.registry.snapshot().nodes[0].base = "changed"

    def test_trusted_provider_registration_and_errors(self):
        name = "test-plugin-" + str(id(self))
        def callback(node, keyword, limit, pages):
            return [{"name": keyword, "share_url": "https://pan.quark.cn/s/custom"}], {"errors": 0}
        plugins.register_provider(name, callback)
        self.addCleanup(plugins._PROVIDERS.pop, name, None)
        self.write([self.node(provider=name)])
        result = self.query(use_cache=False)
        self.assertEqual(result["items"][0]["share_url"], "https://pan.quark.cn/s/custom")
        with self.assertRaises(plugins.PluginConfigError):
            plugins.register_provider("html", callback)

    def test_unselected_entrypoints_do_not_load_code(self):
        with patch.object(plugins.metadata, "entry_points") as entrypoints:
            self.assertEqual(plugins.load_entrypoints([]), {})
        entrypoints.assert_not_called()

    def test_entrypoints_only_explicit_selected_name_loads(self):
        name = "entrypoint-test-" + str(id(self))
        called = []
        class Entry:
            def __init__(self, label):
                self.name = label
            def load(self):
                called.append(self.name)
                return lambda *args: ([], {"errors": 0})
        self.addCleanup(plugins._PROVIDERS.pop, name, None)
        self.addCleanup(plugins._ENTRYPOINT_ATTEMPTS.discard, name)
        with patch.object(plugins.metadata, "entry_points", return_value=[Entry("unselected"), Entry(name)]):
            self.assertEqual(plugins.load_entrypoints([name]), {})
        self.assertEqual(called, [name])

    def test_all_query_variants_share_one_source_snapshot(self):
        self.write([self.node()])
        original = matrix.PanshareMatrix.search_report
        fingerprints = []
        def intercept(query, **kwargs):
            fingerprints.append(kwargs["snapshot"].fingerprint)
            if len(fingerprints) == 1:
                self.write([self.node("new", path="/b")])
                self.registry.reload()
            return original(query, **kwargs)
        with patch.object(matrix.PanshareMatrix, "search_report", side_effect=intercept):
            report = search.search_report("Sample (2021)", aliases=("Other name",), use_cache=False)
        self.assertGreater(len(fingerprints), 1)
        self.assertEqual(len(set(fingerprints)), 1)
        self.assertTrue(report["configured"])


class ExplicitEnvFileTests(unittest.TestCase):
    def test_registry_and_entrypoint_allowlist_use_explicit_env_file(self):
        with tempfile.TemporaryDirectory(prefix="vod-plugin-env-") as directory:
            root = Path(directory)
            node_file = root / "nodes.json"
            node_file.write_text(json.dumps({"version": 1, "nodes": [{
                "id": "fromfile", "provider": "json", "base": "https://example.invalid"
            }]}))
            override = root / "override.json"
            override.write_text(json.dumps({"version": 1, "nodes": []}))
            envfile = root / "settings.env"
            envfile.write_text("VOD_SEARCH_CONFIG=" + str(node_file) +
                               "\nVOD_SEARCH_PLUGINS=fixture_file\n")
            code = """
import os, sys
sys.path.insert(0, sys.argv[1])
from quark_vod import search_plugins as p
loaded = []
class Entry:
    def __init__(self, name): self.name = name
    def load(self):
        loaded.append(self.name)
        return lambda *args, **kwargs: ([], {})
p.metadata.entry_points = lambda **kwargs: [Entry("fixture_file"), Entry("fixture_process")]
registry = p.get_registry()
assert registry.path == __import__("pathlib").Path(sys.argv[2])
assert registry.snapshot().nodes[0].id == "fromfile"
assert loaded == ["fixture_file"]
os.environ["VOD_SEARCH_CONFIG"] = sys.argv[3]
os.environ["VOD_SEARCH_PLUGINS"] = "fixture_process"
assert p.get_registry().path == __import__("pathlib").Path(sys.argv[3])
assert loaded == ["fixture_file", "fixture_process"]
os.environ["VOD_SEARCH_CONFIG"] = ""
os.environ["VOD_SEARCH_PLUGINS"] = ""
assert p.get_registry().path is None
assert loaded == ["fixture_file", "fixture_process"]
"""
            env = dict(os.environ)
            env.pop("VOD_SEARCH_CONFIG", None)
            env.pop("VOD_SEARCH_PLUGINS", None)
            env.update(VOD_ENV_FILE=str(envfile), VOD_BASE_DIR=str(root / "state"))
            result = subprocess.run([sys.executable, "-I", "-c", code,
                str(Path(__file__).resolve().parents[1] / "app"),
                str(node_file), str(override)], env=env, text=True,
                capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
