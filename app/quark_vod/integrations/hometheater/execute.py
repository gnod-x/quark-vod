"""Internal fixed task dispatcher. Public execution goes through automation_runner."""
import importlib
import sys
from quark_vod import db
from quark_vod.coordination import Busy

TASKS = {
    "ai_night_worker": ("ai_night_worker", ()),
    "rss_subscriber": ("rss_subscriber", ()),
    "auto_sync": ("auto_sync", ("--full",)),
    "daily_update_and_prune": ("daily_update_and_prune", ()),
    "airing_chase": ("airing_chase", ()),
    "digest": ("vod_automation", ("digest",)),
    "quality_upgrade": ("vod_automation", ("upgrade",)),
    "douban_watchlist_sync": ("douban_watchlist_sync", ()),
    "wishlist_bot": ("wishlist_bot", ()),
}

def dispatch(task_id):
    """No dynamic code path or arbitrary arguments are accepted."""
    if task_id not in TASKS:
        raise ValueError("unknown task")
    module, args = TASKS[task_id]
    original_argv = sys.argv
    sys.argv = [module, *args]
    try:
        # Freeze settings once before importing any job modules.
        from . import ai
        if task_id in {"ai_night_worker", "daily_update_and_prune", "airing_chase", "quality_upgrade"}:
            from quark_vod import config
            if not config.VOD_JELLYFIN_ENABLED:
                raise RuntimeError("Jellyfin integration must be explicitly enabled")
            if task_id in {"ai_night_worker", "daily_update_and_prune", "quality_upgrade"} and not config.VOD_MAINTENANCE_ENABLED:
                raise RuntimeError("maintenance must be explicitly enabled")
        target = importlib.import_module(__package__ + "." + module)
        if module == "rss_subscriber":
            from quark_vod.coordination import file_lock
            with file_lock("rss-subscriber"):
                target.run_subscriber()
            return 0
        if module == "douban_watchlist_sync":
            result = target.run_sync()
            return int(bool(result.get("errors")))
        if module == "wishlist_bot":
            result = target.poll_telegram()
            return int(bool(result.get("errors")))
        result = target.main()
        return result if type(result) is int else 0
    except Busy:
        return 75
    finally:
        sys.argv = original_argv
        db.close()

def main(argv=None):
    values = sys.argv[1:] if argv is None else argv
    if len(values) != 1 or values[0] not in TASKS:
        print("expected one fixed task ID", file=sys.stderr)
        return 2
    try:
        from quark_vod.automation_settings import get_settings
        settings = get_settings()
        if not settings["tasks"][values[0]]["enabled"]:
            print("task disabled", file=sys.stderr)
            return 1
        return dispatch(values[0])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        print("task failed: " + type(exc).__name__, file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
