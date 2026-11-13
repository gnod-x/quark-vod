.PHONY: up down restart build logs status test sync

sync:
	@cp -f /home/dong/projects/quark-vod/app/quark_vod/*.py /home/dong/projects/hometheater/scripts/quark_vod/
	@echo "[OK] Synced quark_vod modules to hometheater/scripts/quark_vod"

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
	@echo "\n--- 8099 Capability ---"
	@curl -s http://127.0.0.1:8099/capability | python3 -m json.tool

test:
	@echo "=== Running Quark-VOD Engine Verification Suite ==="
	@docker compose config >/dev/null && echo "[PASS] 1. quark-vod compose syntax valid"
	@[ "$$(cat /sys/bus/pci/devices/0000:01:00.0/power_state)" = "D3cold" ] && echo "[PASS] 2. dGPU is D3cold" || (echo "[FAIL] 2. dGPU woken up!" && exit 1)
	@curl -sf http://127.0.0.1:8099/health >/dev/null && echo "[PASS] 3. 8099 /health is online"
	@curl -sf http://127.0.0.1:8099/admin >/dev/null && echo "[PASS] 4. 8099 /admin Whitebox Console reachable"
	@curl -sf http://127.0.0.1:8099/docs >/dev/null && echo "[PASS] 5. 8099 /docs Swagger UI reachable"
	@python3 -c "import urllib.request, json, sys; d=json.loads(urllib.request.urlopen('http://127.0.0.1:8099/capability').read()); print('[PASS] 6. Capability ready:', d) if d.get('ready') else sys.exit(1)"
	@python3 -m py_compile app/main.py app/quark_vod/*.py app/jobs/*.py && echo "[PASS] 7. All Python modules compile cleanly"
	@diff -q /home/dong/projects/quark-vod/app/quark_vod/strm_manager.py /home/dong/projects/hometheater/scripts/quark_vod/strm_manager.py >/dev/null && echo "[PASS] 8. quark_vod dual-directory in sync" || (echo "[FAIL] 8. quark_vod drift detected!" && exit 1)
	@python3 -I tests/test_name_parser.py >/tmp/name_parser_test.log 2>&1 && echo "[PASS] 9. name_parser offline regression (red-line 7/8 golden samples)" || (echo "[FAIL] 9. name_parser regression!" && tail -20 /tmp/name_parser_test.log && exit 1)
	@for f in $$(ls app/quark_vod/*.py); do diff -q $$f /home/dong/projects/hometheater/scripts/quark_vod/$$(basename $$f) >/dev/null || (echo "[FAIL] 10. drift: $$f" && exit 1); done; echo "[PASS] 10. all quark_vod modules zero drift"
