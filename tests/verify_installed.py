"""Smoke an installed distribution outside its source tree, using local state only."""
import importlib
from importlib.metadata import version
from importlib.resources import files
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    import quark_vod

    expected = sys.argv[1] if len(sys.argv) > 1 else version("quark-vod")
    assert quark_vod.__version__ == version("quark-vod") == expected
    assert Path(quark_vod.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    resources = files("quark_vod")
    page = resources.joinpath("static/admin.html").read_text()
    assert "api/admin/cinema" in page and "api/admin/automation" in page
    assert not resources.joinpath("integrations/hometheater/category_governor.py").is_file()

    # Never inherit operator account, service, library or AI settings.
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "SSL_CERT_FILE"}}
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    executable = Path(sys.prefix) / "bin/quark-vod"
    token = "installed-distribution-fixture"
    checks = ["installed_origin", "version", "ui_resources"]

    with tempfile.TemporaryDirectory(prefix="vod-installed-") as temporary:
        root = Path(temporary)
        runtime = root / "runtime"
        configuration = root / "runtime.env"
        configuration.write_text(
            "VOD_ADMIN_TOKEN=" + token + "\n"
            "VOD_JELLYFIN_ENABLED=false\nVOD_MAINTENANCE_ENABLED=false\n"
            "VOD_STARTUP_SCAN=false\nVOD_STARTUP_WRITE_PROBE=false\n"
            "SELFHEAL_ENABLED=false\nAI_PARSE_ENABLED=false\n"
            "VOD_HOST_TRADITIONAL_ENABLED=false\n"
        )
        configuration.chmod(0o600)
        common = ["--env", str(configuration), "--data-dir", str(runtime)]

        def cli(*arguments):
            result = subprocess.run([str(executable), *arguments], cwd=root, env=environment,
                                    text=True, capture_output=True, timeout=30)
            assert result.returncode == 0, ("CLI failed", arguments, result.returncode)
            return result.stdout

        assert cli("--version").strip() == "Quark VOD " + expected
        assert "jobs" in cli("--help")
        jobs = json.loads(cli("jobs", *common, "list"))
        assert len(jobs["jobs"]) == 9
        settings = jobs["settings"]
        assert not settings["enabled"] and not settings["ai"]["enabled"]
        assert all(not spec["enabled"] for spec in settings["tasks"].values())
        checks.extend(["cli", "nine_tasks_disabled"])

        with socket.socket() as address:
            address.bind(("127.0.0.1", 0))
            port = address.getsockname()[1]
        base = "http://127.0.0.1:" + str(port)

        def request(path, body=None, *, auth=True, method=None):
            headers = {"Authorization": "Bearer " + token} if auth else {}
            if body is not None:
                headers["Content-Type"] = "application/json"
            req = Request(base + path, data=None if body is None else json.dumps(body).encode(),
                          headers=headers, method=method)
            try:
                with urlopen(req, timeout=5) as response:
                    raw = response.read()
                    content = response.headers.get("Content-Type", "")
                    return response.status, json.loads(raw) if "json" in content else raw.decode()
            except HTTPError as error:
                return error.code, json.loads(error.read())

        with (root / "api.log").open("wb") as log:
            server = subprocess.Popen([str(executable), "serve", *common,
                                       "--host", "127.0.0.1", "--port", str(port)],
                                      cwd=root, env=environment, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 25
                while True:
                    assert server.poll() is None, "Installed HTTP process exited"
                    try:
                        status, health = request("/health", auth=False)
                        assert status == 200 and health["status"] == "ok"
                        assert health["version"] == expected
                        break
                    except (URLError, OSError):
                        if time.monotonic() >= deadline:
                            raise AssertionError("Installed HTTP process did not become healthy")
                        time.sleep(0.1)
                assert request("/admin", auth=False)[0] == 200
                for endpoint in ("/api/admin/automation", "/api/admin/automation/jobs",
                                 "/api/admin/automation/runs", "/api/admin/cinema/discovery"):
                    assert request(endpoint, auth=False)[0] == 401
                assert len(request("/api/admin/automation/jobs")[1]["jobs"]) == 9
                status, discovery = request("/api/admin/cinema/discovery")
                assert status == 200 and discovery["code"] == 0
                checks.extend(["http_health", "bearer_auth", "cinema_empty_library"])

                status, document = request("/api/admin/automation")
                assert status == 200
                payload = {key: value for key, value in document.items()
                           if key not in {"source", "version"}}
                payload["tasks"]["digest"]["enabled"] = True
                payload["ai"].update(base_url="https://example.invalid/v1",
                                     primary_model="fixture-primary",
                                     fallback_models=["fixture-fallback"])
                status, saved = request("/api/admin/automation", payload, method="PUT")
                assert status == 200 and saved["revision"] == payload["revision"] + 1
                assert request("/api/admin/automation", payload, method="PUT")[0] == 409
                status, queued = request("/api/admin/automation/tasks/digest/run",
                                         {"revision": saved["revision"], "dry_run": True})
                assert status == 202 and queued["dry_run"] is True
                result = json.loads(cli("jobs", *common, "worker", "--once"))
                assert result["status"] == "dry_run"
                status, history = request("/api/admin/automation/runs")
                row = next(row for row in history["runs"] if row["id"] == queued["run_id"])
                assert status == 200 and row["status"] == "dry_run"
                assert row["settings_revision"] == saved["revision"]
                checks.extend(["model_configuration", "revision_conflict",
                               "shared_cli_api_state", "worker_dry_run"])
            finally:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)

        # Import the shipped integration from site-packages without external I/O.
        os.environ.clear()
        os.environ.update(environment, VOD_BASE_DIR=str(runtime), VOD_ENV_FILE=str(configuration))
        for name in ("vod_search_and_add", "ai_night_worker", "rss_subscriber", "auto_sync",
                     "daily_update_and_prune", "airing_chase", "vod_automation", "execute",
                     "douban_watchlist_sync", "wishlist_bot"):
            module = importlib.import_module("quark_vod.integrations.hometheater." + name)
            assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
        execute = importlib.import_module("quark_vod.integrations.hometheater.execute")
        assert set(execute.TASKS) == set(settings["tasks"])
        wrappers = root / "wrappers"
        subprocess.run([sys.executable, "-BI", "-m", "quark_vod.integrations.jellyfin",
                        "--output", str(wrappers)], env=environment, cwd=root,
                       check=True, capture_output=True, timeout=10)
        for name in ("ffmpeg", "ffprobe"):
            wrapper = wrappers / name
            assert wrapper.read_bytes().startswith(b"#!")
            assert os.access(wrapper, os.X_OK)
        checks.extend(["package_local_job_imports", "wrapper_export"])
    print(json.dumps({"version": expected, "checks": checks, "result": "passed"}))


if __name__ == "__main__":
    main()
