# grow-my-money — Python analogue of bookhunt's npm docker scripts.
# Nothing is on PATH; the venv is used explicitly.

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
export GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
export BUILD_TIME := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

.PHONY: venv test up down logs stop resume status once report dash

venv:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

test:
	$(PY) -m pytest -q

# Build + (re)start the container, stamping GIT_SHA / BUILD_TIME like bookhunt.
up:
	GIT_SHA=$(GIT_SHA) BUILD_TIME=$(BUILD_TIME) docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

# Kill switch (works from host; state/ is bind-mounted).
stop:
	$(PY) -m src.cli stop

resume:
	$(PY) -m src.cli resume

status:
	$(PY) -m src.cli status

once:
	$(PY) -m src.cli once

report:
	$(PY) -m src.cli report-now --dry-run

# Read-only web dashboard (local loopback only).
dash:
	@echo open http://127.0.0.1:8420
	@curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8420/
