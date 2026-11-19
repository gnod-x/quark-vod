PYTHON ?= python3
HOMETHEATER_DIR ?= ../hometheater

.PHONY: install test test-offline test-homelab test-ui package build up down restart logs status sync sync-jobs check-sync verify-runtime

install:
	$(PYTHON) -m pip install -e '.[test]'

test: test-offline

test-offline:
	@set -e; for file in tests/test_*.py; do \
	  case "$$file" in tests/test_portal_vod.py|tests/test_probe_wrapper.py) continue ;; esac; \
	  PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -I "$$file"; \
	done

test-homelab: test-offline
	@set -e; test -d "$(HOMETHEATER_DIR)/scripts/tests"; \
	for file in tests/test_portal_vod.py tests/test_probe_wrapper.py; do PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -I "$$file"; done; \
	for file in "$(HOMETHEATER_DIR)"/scripts/tests/test_*.py; do PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -I "$$file"; done
	$(PYTHON) -I tests/verify_tmdb_populate.py --offline-fixture
	$(MAKE) check-sync

test-ui:
	node tests/test_portal_ui.js

package:
	$(PYTHON) -m build

build:
	docker compose build quark-vod

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart quark-vod

logs:
	docker compose logs --tail 100 -f quark-vod

status:
	docker compose ps

sync:
	@test -d "$(HOMETHEATER_DIR)/scripts/quark_vod"
	cp -f app/quark_vod/*.py "$(HOMETHEATER_DIR)/scripts/quark_vod/"

sync-jobs:
	@set -e; for name in ai_night_worker category_governor daily_update_and_prune people_manager purge_orphan_metadata tmdb_populate vod_search_and_add; do cp "$(HOMETHEATER_DIR)/scripts/$$name.py" "app/jobs/$$name.py"; done

check-sync:
	$(PYTHON) -I tests/check_sync.py --destination "$(HOMETHEATER_DIR)/scripts/quark_vod"

verify-runtime:
	@test -n "$(OUTPUT)" || (echo "Set OUTPUT=/path/to/report.json"; exit 2)
	$(PYTHON) -I tests/verify_runtime.py --output "$(OUTPUT)"
