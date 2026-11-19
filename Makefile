.PHONY: up down restart build logs status test sync sync-jobs test-offline test-ui check-sync verify-runtime

sync:
	@cp -f /home/dong/projects/quark-vod/app/quark_vod/*.py /home/dong/projects/hometheater/scripts/quark_vod/
	@echo "[OK] Synced quark_vod modules to hometheater/scripts/quark_vod"

sync-jobs:
	@set -e; for name in ai_night_worker category_governor daily_update_and_prune people_manager purge_orphan_metadata tmdb_populate vod_search_and_add; do cp /home/dong/projects/hometheater/scripts/$$name.py app/jobs/$$name.py; done

up:
	docker compose up -d

build:
	docker compose build quark-vod && docker compose up -d

restart: sync
	docker compose restart quark-vod

down:
	docker compose down

logs:
	docker logs -f --tail 100 quark-vod

status:
	@docker ps --filter "name=quark-vod" --filter "name=jellyfin2" --filter "name=openlist"
	@curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8099/health | python3 -m json.tool

test: test-offline check-sync

test-ui:
	@node tests/test_portal_ui.js

test-offline:
	@python3 -I tests/test_name_parser.py
	@python3 -I tests/test_search_quality.py
	@python3 -I tests/test_portal_vod.py
	@python3 -I tests/test_stream_and_state.py
	@python3 -I tests/test_playback_coordinator.py
	@python3 -I tests/test_probe_wrapper.py
	@python3 -I tests/test_media_cache.py
	@python3 -I tests/test_sync_check.py
	@python3 -I ../hometheater/scripts/tests/test_daily_prune_and_populate.py
	@python3 -I tests/verify_tmdb_populate.py --offline-fixture
	@python3 -I ../hometheater/scripts/tests/test_ai_worker_concurrency.py
	@python3 -I ../hometheater/scripts/tests/test_ai_extractor.py

check-sync:
	@python3 -I tests/check_sync.py

verify-runtime:
	@python3 -I tests/verify_runtime.py --output /home/dong/.agent-context/verification/cinema-runtime-latest.json
