.PHONY: up down restart build logs status test

up:
	docker compose up -d

build:
	docker compose build quark-vod && docker compose up -d

restart:
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
