PYTHON ?= python3

.PHONY: install test test-offline package build up down restart logs status

install:
	$(PYTHON) -m pip install -e '.[test]'

test: test-offline

test-offline:
	@set -e; for file in tests/test_*.py; do \
	  case "$$file" in tests/test_portal_vod.py|tests/test_probe_wrapper.py) continue ;; esac; \
	  $(PYTHON) -BI "$$file"; \
	done

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

-include Makefile.homelab
