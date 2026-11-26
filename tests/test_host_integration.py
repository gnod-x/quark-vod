"""Packaged host integration contract and migrated offline fixture suites."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = Path(__file__).resolve().parent
CASE_DIR = HERE / "host_integration_cases"

class HostMigrationTests(unittest.TestCase):
    def run_case(self, name):
        with tempfile.TemporaryDirectory(prefix="vod-host-case-") as root:
            env = self.environment(root)
            result = subprocess.run([sys.executable, str(CASE_DIR / (name + ".py"))],
                                    env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def environment(self, root):
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("VOD_", "HOMETHEATER_", "JELLYFIN", "AI_", "OPENAI", "TMDB_", "QUARK_")):
                env.pop(key, None)
        env.update(VOD_BASE_DIR=root, VOD_DATA_DIR=root + "/data",
                   VOD_STATE_DB=root + "/data/quark_plays.db", PYTHONDONTWRITEBYTECODE="1")
        return env

    def script(self, body):
        with tempfile.TemporaryDirectory(prefix="vod-host-contract-") as root:
            result = subprocess.run([sys.executable, "-c", textwrap.dedent(body)],
                    env=self.environment(root), capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_content_fixture_suite(self):
        self.run_case("test_ai_worker_content")

    def test_subtitle_fixture_suite(self):
        self.run_case("test_ai_worker_subtitles")

    def test_atomic_write_fixture_suite(self):
        self.run_case("test_ai_worker_write_safety")

    def test_daily_protection_fixture_suite(self):
        self.run_case("test_daily_prune_and_populate")

    def test_skip_write_optin_fixture_suite(self):
        self.run_case("test_skip_write_gate")

    def test_wishlist_configuration_fixture_suite(self):
        self.run_case("test_wishlist_explicit_config")

    def test_portal_file_selection_fixture_suite(self):
        self.run_case("test_portal_file_selection")

    def test_portal_variant_routing_fixture_suite(self):
        self.run_case("test_portal_variant_routing")

    def test_portal_add_coordination_fixture_suite(self):
        self.run_case("test_portal_add_coordination")

    def test_portal_search_identity_fixture_suite(self):
        self.run_case("test_portal_search_identity")

    def test_portal_library_notify_fixture_suite(self):
        self.run_case("test_portal_library_notify")

    def test_portal_replace_result_fixture_suite(self):
        self.run_case("test_portal_replace_result")

    def test_portal_search_diagnostics_fixture_suite(self):
        self.run_case("test_portal_search_diagnostics")

    def test_vod_evolution_fixture_suite(self):
        self.run_case("test_vod_evolution")

    def test_all_modules_import_without_network_or_state_writes(self):
        self.script("""
            import importlib, pkgutil, os
            from pathlib import Path
            from unittest.mock import patch
            from quark_vod.integrations import hometheater
            with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
                for module in pkgutil.iter_modules(hometheater.__path__):
                    importlib.import_module(hometheater.__name__ + "." + module.name)
            assert list(Path(os.environ["VOD_BASE_DIR"]).rglob("*")) == []
        """)

    def test_disabled_ai_never_sends_a_request(self):
        self.script("""
            from unittest.mock import patch
            from quark_vod.integrations.hometheater import ai, ai_night_worker as night, subtitle_ad_cleaner as sub, ai_extractor as names
            with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")) as net:
                assert ai.models() == []
                assert ai.request_chat([], task="plot") is None
                assert night.call_llm("fixture") == (None, None)
                assert night._request_chat("fixture", "fixture", "fixture") is None
                assert sub.request_ai_ad_judgment([{"id": 1, "text": "synthetic"}]) == {}
                assert names.request_ai_completions([]) == ""
                net.assert_not_called()
        """)

    def test_portal_uses_configured_non_default_api_port_and_exact_file(self):
        self.script("""
            import json, threading
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
            from types import SimpleNamespace
            from unittest.mock import patch
            from quark_vod.integrations.hometheater import vod_search_and_add as portal
            from quark_vod import config
            received=[]
            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                                     self.headers.get("Authorization")))
                    body=b'{"ok":true,"state":"ready"}'
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                def log_message(self, *args): pass
            httpd=ThreadingHTTPServer(("127.0.0.1",0), Handler)
            thread=threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
            try:
                with patch.object(config, "HOST", "0.0.0.0"), patch.object(config, "PORT", httpd.server_port):
                    base=portal._api_base()
                    assert base == "http://127.0.0.1:" + str(httpd.server_port)
                selected={"work":{"item":{"cat":"Movies"},"folder":"Synthetic (2026)","season":0,"episode":0},
                          "candidate":SimpleNamespace(share_url="https://pan.quark.cn/s/synthetic"),
                          "selected_fid":"exact-synthetic-file"}
                with patch.object(portal, "VOD_API", base), patch.object(config, "get_admin_token", return_value="synthetic-only"):
                    assert portal._core_resolve(selected)["ok"] is True
                path, body, auth=received[0]
                assert path == "/api/admin/resolve"
                assert body["selected_fid"] == "exact-synthetic-file"
                assert auth == "Bearer synthetic-only"
            finally:
                httpd.shutdown(); httpd.server_close(); thread.join()
        """)

    def test_portal_api_override_refuses_credentials_and_queries(self):
        self.script("""
            import os
            from unittest.mock import patch
            from quark_vod.integrations.hometheater import vod_search_and_add as portal
            for value in ("http://user:secret@fixture.invalid", "http://fixture.invalid?token=x", "file:///tmp/file", "http://fixture.invalid#fragment"):
                with patch.dict(os.environ, VOD_API_BASE=value):
                    try: portal._api_base()
                    except ValueError: pass
                    else: raise AssertionError("unsafe override accepted")
        """)

    def test_models_fallback_in_configured_order_and_use_configured_effort(self):
        self.script("""
            import io, json, urllib.error
            from unittest.mock import patch
            from quark_vod.integrations.hometheater import ai
            snapshot = {"enabled": True, "base_url": "http://fixture.invalid/v1",
                "primary_model": "first", "fallback_models": ["second", "first"],
                "reasoning": {"filename": "medium"}}
            seen=[]
            def request(req, timeout):
                body=json.loads(req.data); seen.append(body)
                if body["model"] == "first":
                    raise urllib.error.URLError("synthetic")
                return io.BytesIO(json.dumps({"choices":[{"message":{"content":"answer"}}]}).encode())
            with patch.object(ai, "SNAPSHOT", snapshot), patch("urllib.request.urlopen", side_effect=request):
                assert ai.request_chat([], task="filename") == "answer"
            assert [x["model"] for x in seen] == ["first", "second"]
            assert all(x["reasoning_effort"] == "medium" for x in seen)
        """)

    def test_non_ai_night_stages_run_and_ai_enrichment_stays_disabled(self):
        self.script("""
            from contextlib import ExitStack
            from unittest.mock import patch, Mock
            from quark_vod.integrations.hometheater import ai_night_worker as night
            with ExitStack() as stack:
                stack.enter_context(patch.object(night, "log"))
                stack.enter_context(patch.object(night, "is_timeout", return_value=False))
                probe=stack.enter_context(patch.object(night, "probe_free_models"))
                stages=[stack.enter_context(patch.object(night, name)) for name in
                    ("run_yesterday_playback_selfheal", "run_library_integrity_check",
                     "run_vod_source_audit_and_lock", "run_clean_all_subtitles")]
                plot=stack.enter_context(patch.object(night, "run_refine_movie_plots_and_tags"))
                people=stack.enter_context(patch.object(night, "run_enrich_peoples_knowledge"))
                assert night.main() == 0
                assert all(x.call_count == 1 for x in stages)
                plot.assert_not_called(); people.assert_not_called()
        """)

    def test_night_busy_timeout_and_unknown_outcomes_are_not_success(self):
        self.script("""
            from contextlib import ExitStack
            from unittest.mock import patch
            from quark_vod.integrations.hometheater import ai_night_worker as night
            with patch.object(night, "acquire_single_instance_lock", return_value=False), patch.object(night, "log"):
                assert night.main() == 75
            for unknown in (False, True):
                with ExitStack() as stack:
                    stack.enter_context(patch.object(night, "log"))
                    stack.enter_context(patch.object(night, "is_timeout", return_value=not unknown))
                    for name in ("probe_free_models", "run_yesterday_playback_selfheal",
                         "run_library_integrity_check", "run_clean_all_subtitles"):
                        stack.enter_context(patch.object(night, name))
                    stack.enter_context(patch.object(night, "run_vod_source_audit_and_lock",
                        side_effect=lambda: night.incomplete("unknown") if unknown else None))
                    assert night.main() == 1
                    assert night._lock_fp.closed
        """)

    def test_traditional_sync_requires_explicit_optin(self):
        self.script("""
            from unittest.mock import patch
            from quark_vod.integrations.hometheater import auto_sync, sync_all
            with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
                for fn in (auto_sync.main, sync_all.run_sync, sync_all.run_post_scan_worker):
                    try: fn()
                    except RuntimeError as exc: assert "disabled" in str(exc)
                    else: raise AssertionError("implicit traditional library task")
        """)

    def test_traditional_tail_work_selects_traditional_server(self):
        self.script("""
            import os, tempfile
            from unittest.mock import patch, Mock
            from quark_vod.integrations.hometheater import sync_all
            with tempfile.TemporaryDirectory() as tmp:
                with patch.dict(os.environ, VOD_HOST_TRADITIONAL_ENABLED="1", INTRO_SKIPPER_WRITE_ENABLED="1"), patch.object(sync_all.paths, "LOG_DIR", tmp), patch.object(sync_all, "wait_for_jellyfin_scan_complete"), patch.object(sync_all.time, "sleep"), patch.object(sync_all, "ensure_fallback_posters"), patch.object(sync_all.subprocess, "run", return_value=Mock(returncode=0)) as run:
                    sync_all.run_post_scan_worker()
                    command=run.call_args.args[0]
                    assert command[-3:] == ["--server", "jellyfin", "auto-tag"]
        """)

    def test_execute_accepts_only_fixed_ids_and_disabled_task_does_not_dispatch(self):
        self.script("""
            from unittest.mock import patch
            from quark_vod.integrations.hometheater import execute
            assert execute.main(["../../anything"]) == 2
            assert execute.main(["ai_night_worker", "arbitrary"]) == 2
            with patch.object(execute, "dispatch", side_effect=AssertionError("disabled")) as run:
                assert execute.main(["digest"]) == 1
                run.assert_not_called()
        """)

    def test_explicit_tmdb_key_and_paths_do_not_require_old_repository(self):
        self.script("""
            import os, tempfile
            from pathlib import Path
            from unittest.mock import patch
            from quark_vod.integrations.hometheater import paths, tmdb_populate
            from quark_vod import config
            assert paths.DATA_DIR == config.DATA_DIR
            assert paths.CINEMA2_DIR == config.CINEMA2_DIR
            assert paths.JELLYFIN2_DB == config.JELLYFIN2_DB
            with patch.dict(os.environ, TMDB_API_KEY="synthetic-only"):
                assert tmdb_populate.read_tmdb_key() == "synthetic-only"
            with patch.dict(os.environ, TMDB_API_KEY="", TMDB_API_KEY_FILE=""), patch.object(tmdb_populate, "TMDB_XML", ""):
                try: tmdb_populate.read_tmdb_key()
                except RuntimeError: pass
                else: raise AssertionError("missing TMDB reported success")
        """)

    def test_wrapper_resource_export_refuses_overwrite_and_symlinks(self):
        self.script("""
            import os, stat, tempfile, subprocess
            from pathlib import Path
            from quark_vod.integrations.jellyfin.__main__ import export
            with tempfile.TemporaryDirectory() as tmp:
                target=Path(tmp)/"wrappers"
                export(target)
                for name in ("ffmpeg", "ffprobe"):
                    path=target/name
                    assert stat.S_IMODE(path.stat().st_mode) == 0o755
                    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
                try: export(target)
                except FileExistsError: pass
                else: raise AssertionError("implicit overwrite")
                (target/"ffmpeg").unlink()
                outside=Path(tmp)/"outside"; outside.write_text("keep")
                (target/"ffmpeg").symlink_to(outside)
                try: export(target, force=True)
                except ValueError: pass
                else: raise AssertionError("symlink overwritten")
                assert outside.read_text() == "keep"
        """)

    def test_ffmpeg_wrapper_invokes_configured_upstream_binary(self):
        self.script("""
            import json, os, subprocess, tempfile
            from pathlib import Path
            from quark_vod.integrations.jellyfin.__main__ import export
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); export(root/"wrappers")
                binary=root/"upstream"
                binary.write_text(chr(10).join(["#!/usr/bin/env python3", "import json,sys", "print(json.dumps(sys.argv[1:]))", ""]))
                binary.chmod(0o755)
                env=dict(os.environ, FFMPEG_BINARY=str(binary))
                out=subprocess.check_output([str(root/"wrappers/ffmpeg"), "-f", "matroska", "-i", "/tmp/synthetic.mkv", "-f", "null", "-"],env=env,text=True)
                assert json.loads(out) == ["-f", "matroska", "-i", "/tmp/synthetic.mkv", "-f", "null", "-"]
        """)

if __name__ == "__main__":
    unittest.main()
