# Muster — local durable runtime.
#
# `make dev` is the one command that should give you a working system:
# infrastructure up, schema created, Muster's Restate service running and its
# deployment registered.
#
# Nothing in here hides a failure: no `|| true`, no discarded exit codes.
# stderr is redirected in exactly one place — the connection probe in
# WAIT_FOR_SERVICE, where "connection refused" is the expected state while
# waiting; the real timeout failure is reported on stderr and exits 1.
#
# The service itself is NOT containerised, so you can restart it without a
# rebuild. Restate reaches it over host.docker.internal (see docker-compose.yml).

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
UV ?= uv

RESTATE_ADMIN_URL ?= http://localhost:9070
RESTATE_INGRESS_URL ?= http://localhost:8080

MUSTER_SERVICE_HOST ?= 0.0.0.0
MUSTER_SERVICE_PORT ?= 9080
MUSTER_SERVICE_URI ?= http://host.docker.internal:$(MUSTER_SERVICE_PORT)/

POSTGRES_HOST ?= localhost
POSTGRES_PORT ?= 5432
POSTGRES_DB ?= muster
POSTGRES_USER ?= muster
POSTGRES_PASSWORD ?= change-me
DATABASE_URL ?= postgresql+psycopg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)

# Uvicorn is HTTP/1.1 only, which is why registration sets use_http_11. Swap in
# hypercorn if HTTP/2 throughput ever justifies the extra dependency.
SERVE_CMD = $(UV) run --extra durable uvicorn --factory app.runtime.durable:create_app --host $(MUSTER_SERVICE_HOST) --port $(MUSTER_SERVICE_PORT)

# Kept as single-line variables so `dev` can chain them inside one shell (and so
# `make -n dev` stays a dry run — a recipe mentioning $(MAKE) would really run).
WAIT_FOR_SERVICE = for attempt in $$(seq 1 60); do if (exec 3<>/dev/tcp/127.0.0.1/$(MUSTER_SERVICE_PORT)) 2>/dev/null; then break; fi; if [ $$attempt -eq 60 ]; then echo "muster service never opened port $(MUSTER_SERVICE_PORT)" >&2; exit 1; fi; sleep 0.5; done
REGISTER_DEPLOYMENT = curl -fsS -X POST $(RESTATE_ADMIN_URL)/deployments -H 'content-type: application/json' -d '{"uri": "$(MUSTER_SERVICE_URI)", "use_http_11": true, "force": true}'

.PHONY: help deps up down clean logs ps dev serve register wait-service migrate test test-integration

help:
	@echo "Muster targets:"
	@echo "  make deps              install the durable + postgres extras"
	@echo "  make up                start Restate and Postgres, wait for healthy"
	@echo "  make down              stop them (named volumes kept)"
	@echo "  make dev               up + migrate + run the service + register it"
	@echo "  make serve             run the Muster service in the foreground"
	@echo "  make register          register the running service with Restate"
	@echo "  make migrate           create the Postgres schema"
	@echo "  make run               run a team in-process and print the timeline"
	@echo "  make demo              two-team bus demo (no Docker)"
	@echo "  make test              unit suite (no Docker, no Restate, no Postgres)"
	@echo "  make test-integration  integration suite (needs 'make up' + 'make deps')"
	@echo "  make logs / make ps    inspect the stack"
	@echo "  make clean             down + delete the named volumes"
	@echo
	@echo "  ingress   $(RESTATE_INGRESS_URL)"
	@echo "  admin/UI  $(RESTATE_ADMIN_URL)"
	@echo "  service   http://localhost:$(MUSTER_SERVICE_PORT)"

# ------------------------------------------------------------------ toolchain

deps:
	$(UV) sync --extra durable --extra postgres

# ------------------------------------------------------------- infrastructure

# --wait blocks on the compose healthchecks and exits non-zero if either
# container never becomes healthy, so a broken stack fails here, not later.
up:
	$(COMPOSE) up -d --wait

down:
	$(COMPOSE) down

clean:
	$(COMPOSE) down --volumes

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps

# ------------------------------------------------------------- muster service

serve:
	$(SERVE_CMD)

wait-service:
	$(WAIT_FOR_SERVICE)

# Restate discovers handlers by calling the service, so it must already be
# listening when this runs. `make dev` sequences that for you.
register:
	$(REGISTER_DEPLOYMENT)
	@echo
	@echo "registered $(MUSTER_SERVICE_URI) with $(RESTATE_ADMIN_URL)"

dev: up migrate
	$(SERVE_CMD) & \
	APP_PID=$$!; \
	trap 'kill $$APP_PID' EXIT INT TERM; \
	$(WAIT_FOR_SERVICE); \
	$(REGISTER_DEPLOYMENT); \
	echo; \
	echo "muster is up — ingress $(RESTATE_INGRESS_URL), admin UI $(RESTATE_ADMIN_URL)"; \
	wait $$APP_PID

# ------------------------------------------------------------------- database

# Creates the schema AND seeds the subscription table. Without the seed,
# publish() resolves to zero subscribers and the demo silently does nothing.
migrate:
	$(UV) run --extra postgres python -m app.main migrate

# ------------------------------------------------------------------ local run

# Runs the investment team here and now. OBJECTIVE and FLAGS are overridable:
#   make run FLAGS=--cross-team OBJECTIVE="Is Acme cheap?"
OBJECTIVE ?= Evaluate whether Company X is attractive at its current valuation.
FLAGS ?=
run:
	$(UV) run python -m app.main run $(FLAGS) "$(OBJECTIVE)"

# ----------------------------------------------------------------------- demo

# The Day-2 demonstration: two independently defined teams, one bus session,
# a cross-team command and a cross-team event. Needs no infrastructure.
demo:
	$(UV) run pytest tests/test_two_team_demo.py tests/test_demo_flow.py -v

# ---------------------------------------------------------------------- tests

# pyproject pins `-m "not integration"`, so this needs no infrastructure.
test:
	$(UV) run pytest

test-integration:
	$(UV) run pytest -m integration
