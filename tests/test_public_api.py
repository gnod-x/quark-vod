"""Standalone API regressions; synthetic credentials and temporary state only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

TMP = tempfile.TemporaryDirectory(prefix="vod-api-")
SEARCH = Path(TMP.name) / "search.json"
SEARCH.write_text(json.dumps({"version": 1, "nodes": []}))
os.environ.update(VOD_BASE_DIR=TMP.name, VOD_SEARCH_CONFIG=str(SEARCH),
                  VOD_STARTUP_SCAN="0", VOD_STARTUP_WRITE_PROBE="0", SELFHEAL_ENABLED="0",
                  VOD_MAINTENANCE_ENABLED="0", VOD_JELLYFIN_ENABLED="0", AI_PARSE_ENABLED="0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from fastapi.testclient import TestClient
import main

AUTH = {"Authorization": "Bearer synthetic-api-test-only"}


class PublicAPI(unittest.TestCase):
    def setUp(self):
        self.auth = patch.object(main.config, "get_admin_token", return_value="synthetic-api-test-only")
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def test_cold_start_and_status_do_not_probe_external_services(self):
        with patch.object(main.legacy_srv, "get_openlist", side_effect=AssertionError("external probe")), \
             patch.object(main.QuarkTransfer, "check_cookie_alive", side_effect=AssertionError("cloud request")):
            with TestClient(main.app) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/admin").status_code, 200)
                self.assertEqual(client.get("/capability", headers=AUTH).status_code, 200)
                self.assertEqual(client.get("/api/admin/overview", headers=AUTH).status_code, 200)

    def test_admin_methods_require_auth_before_side_effects(self):
        with patch.object(main.config, "set_quark_cookie") as write:
            for method, path, extra in [
                ("get", "/stats", {}), ("head", "/stats", {}),
                ("get", "/capability", {}), ("get", "/preheat", {}),
                ("get", "/api/admin/overview", {}),
                ("post", "/api/admin/cookie", {"json": {"cookie": "synthetic"}}),
                ("post", "/api/admin/plugins/reload", {}),
                ("post", "/api/admin/selfheal", {}),
                ("delete", "/api/admin/transfer/1", {}),
            ]:
                with self.subTest(path=path, method=method):
                    response = getattr(self.client, method)(path, **extra)
                    self.assertEqual(response.status_code, 401)
            write.assert_not_called()

    def test_missing_admin_configuration_disables_management(self):
        with patch.object(main.config, "get_admin_token", return_value=""):
            self.assertEqual(self.client.get("/api/admin/plugins", headers=AUTH).status_code, 503)
            self.assertEqual(self.client.get("/health").status_code, 200)

    def test_token_rotation_applies_to_next_request(self):
        with patch.object(main.config, "get_admin_token", side_effect=["first", "second", "second"]):
            self.assertEqual(self.client.get("/api/admin/plugins", headers={"Authorization": "Bearer first"}).status_code, 200)
            self.assertEqual(self.client.get("/api/admin/plugins", headers={"Authorization": "Bearer first"}).status_code, 401)
            self.assertEqual(self.client.get("/api/admin/plugins", headers={"Authorization": "Bearer second"}).status_code, 200)

    def test_bad_reload_retains_active_configuration(self):
        good = {"version": 1, "nodes": []}
        SEARCH.write_text(json.dumps(good))
        before = self.client.post("/api/admin/plugins/reload", headers=AUTH).json()
        SEARCH.write_text("{")
        try:
            response = self.client.post("/api/admin/plugins/reload", headers=AUTH)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["active"]["fingerprint"], before["fingerprint"])
            self.assertNotIn(str(SEARCH), response.text)
        finally:
            SEARCH.write_text(json.dumps(good))
            self.client.post("/api/admin/plugins/reload", headers=AUTH)

    def test_probe_is_explicit_and_authenticated(self):
        with patch.object(main, "_probe_capability", return_value={"ready": True}) as probe:
            self.assertEqual(self.client.get("/capability", headers=AUTH).status_code, 200)
            probe.assert_not_called()
            self.assertEqual(self.client.post("/api/admin/capability/probe").status_code, 401)
            probe.assert_not_called()
            self.assertEqual(self.client.post("/api/admin/capability/probe", headers=AUTH).status_code, 200)
            probe.assert_called_once()

    def test_probe_does_not_report_search_ready_without_enabled_nodes(self):
        store = Mock()
        store.check_write_capable.return_value = (True, "synthetic ready")
        transfer = Mock()
        transfer.check_cookie_alive.return_value = (True, "synthetic ready")
        with patch.object(main.legacy_srv, "get_openlist", return_value=store), \
             patch.object(main.legacy_srv, "get_transfer", return_value=transfer):
            response = self.client.post("/api/admin/capability/probe", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["panshare_ready"])
        self.assertFalse(response.json()["ready"])
        self.assertFalse(self.client.get("/capability", headers=AUTH).json()["ready"])

    def test_preheat_pin_uses_the_resolved_library_title(self):
        server = main.legacy_srv
        with patch.object(server, "_resolve_strm_row", return_value=("电影", 0, 0, None)), \
             patch.object(server.db, "library_title", return_value="Fixture (2026)"), \
             patch.object(server, "_submit", return_value=(Mock(), True)), \
             patch.object(server.db, "pin_item") as pin:
            self.assertEqual(server.trigger_preheat_task("电影", "Fixture"), (True, "started"))
        pin.assert_called_once_with("电影", "Fixture (2026)", reason="preheated")

    def test_rejected_cookie_does_not_disclose_value_or_probe_account(self):
        with patch.object(main.QuarkTransfer, "check_cookie_alive") as probe:
            for cookie in ["synthetic-private-value\nHeader: value", "synthetic-private-value\x00"]:
                response = self.client.post("/api/admin/cookie", headers=AUTH, json={"cookie": cookie})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("synthetic-private-value", response.text)
            with patch.object(main.config, "set_quark_cookie", side_effect=OSError("synthetic-private-path")):
                response = self.client.post("/api/admin/cookie", headers=AUTH, json={"cookie": "synthetic-private-value"})
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("synthetic-private", response.text)
            probe.assert_not_called()

    def test_disabled_maintenance_cannot_modify_library(self):
        with patch.object(main.config, "VOD_MAINTENANCE_ENABLED", False), \
             patch.object(main.selfheal, "scan_and_heal") as heal:
            self.assertEqual(self.client.post("/api/admin/selfheal", headers=AUTH).status_code, 409)
            heal.assert_not_called()
            self.assertEqual(self.client.delete("/api/admin/transfer/1", headers=AUTH).status_code, 409)

    def test_public_clean_proxy_rejects_unconfigured_origins(self):
        with patch.object(main.cleaner, "open_clean_stream") as opened:
            for url in ["http://127.0.0.1:8123/api/", "file:///etc/passwd",
                        "http://example.invalid/d/video", "http://user:pass@localhost:5244/d/video"]:
                response = self.client.get("/clean", params={"url": url})
                self.assertEqual(response.status_code, 403)
            opened.assert_not_called()

    def test_media_origin_is_configurable_and_download_scoped(self):
        with patch.object(main.config, "OPENLIST_PUBLIC", "https://media.example.invalid/list"), \
             patch.object(main.config, "OPENLIST_API", "http://localhost:5244"):
            self.assertTrue(main.media_url_allowed("https://media.example.invalid/list/d/video.mkv"))
            self.assertTrue(main.media_url_allowed("http://localhost:5244/d/%E7%94%B2.mkv"))
            for bad in ["https://media.example.invalid/list/api/admin", "https://media.example.invalid/list/d/../api",
                        "https://media.example.invalid/list/d/%2e%2e/api", "https://media.example.invalid:444/list/d/video"]:
                self.assertFalse(main.media_url_allowed(bad))


if __name__ == "__main__":
    unittest.main()
