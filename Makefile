# Muster — local durable runtime.
#
# `make dev` is the one command that should give you a working system:
# infrastructure up, Muster's Restate service running, deployment registered.
#
# Nothing in here hides a failure: no `|| true`, no discarded exit codes, and
# stderr is redirected in exactly one place (the connection probe in
# `wait-service`, where a refused connection is the expected state while
# waiting, and the real failure is reported loudly at the end).

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
UV ?= uv

RESTATE_ADMIN_URL ?= http://localhost:9070
RESTATE_INGRESS_URL ?= http://localhost:8080

MUSTER_SERVICE_HOST ?= 0.0.0.0
MUSTER_SERVICE_PORT ?= 9080
# Restate runs in a container and reaches the host through host.docker.internal,
# which docker-compose.yml maps to host-gateway so WSL2 behaves like Desktop.
MUSTER_SERVICE_URI ?= http://host.docker.internal:$(MUSTER_SERVICE_PORT)/

POSTGRES_HOST ?= localhost
POSTGRES_PORT ?= 5432
POSTGRES_DB ?= muster
POSTGRES_USER ?= muster
POSTGRES_PASSWORD ?= change-me
DATABASE_URL ?= postgresql+psycopg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)

.PHONY: help deps up down dev serve register migrate test test-integration logs ps clean wait-service

help:
	@echo "Muster targets:"
	@echo "  make deps              install the durable + postgres extras"
	@echo "  make up                start Restate and Postgres, wait for healthy"
	@echo "  make down              stop them (volumes kept)"
	@echo "  make dev               up + run the Muster service + register it"
	@echo "  make serve             run the Muster service in the foreground"
	@echo "  make register          register the running service with Restate"
	@echo "  make migrate           create the Postgres schema"
	@echo "  make test              unit suite (no Docker, no Restate, no Postgres)"
	@echo "  make test-integration  integration suite (needs 'make up')"
	@echo "  make logs / make ps    inspect the stack"
	@echo "  make clean             down + delete the named volumes"

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

# -------------------------------------------------------------- muster service

# Uvicorn is HTTP/1.1 only; `register` tells Restate so. Swap in hypercorn for
# HTTP/2 if throughput ever justifies the extra dependency.
serve:
	$(UV) run --extra durable uvicorn --factory app.runtime.durable:create_app \
		--host $(MUSTER_SERVICE_HOST) --port $(MUSTER_SERVICE_PORT)

# Restate discovers handlers by calling the service, so the service must
# already be listening when this runs.
register:
	curl -fsS -X POST $(RESTATE_ADMIN_URL)/deployments \
		-H 'content-type: application/json' \
		-d '{"uri": "$(MUSTER_SERVICE_URI)", "use_http_11": true, "force": true}'
	@echo
	@echo "registered $(MUSTER_SERVICE_URI) with $(RESTATE_ADMIN_URL)"

wait-service:
	for attempt in $$(seq 1 60); do \
		if (exec 3<>/dev/tcp/127.0.0.1/$(MUSTER_SERVICE_PORT)) 2>/dev/null; then \
			exit 0; \
		fi; \
		sleep 0.5; \
	done; \
	echo "muster service never opened port $(MUSTER_SERVICE_PORT)" >&2; \
	exit 1

dev: up migrate
	$(UV) run --extra durable uvicorn --factory app.runtime.durable:create_app \
		--host $(MUSTER_SERVICE_HOST) --port $(MUSTER_SERVICE_PORT) & \
	APP_PID=$$!; \
	trap 'kill $$APP_PID' EXIT INT TERM; \
	$(MAKE) wait-service; \
	$(MAKE) register; \
	echo "muster is up — ingress $(RESTATE_INGRESS_URL), admin UI $(RESTATE_ADMIN_URL)"; \
	wait $$APP_PID

# ------------------------------------------------------------------- database

migrate:
	$(UV) run --extra postgres python -c 'from app.db.repository import Repository; Repository.from_url("$(DATABASE_URL)").init_schema(); print("schema ready on $(POSTGRES_DB)")'

# ---------------------------------------------------------------------- tests

# pyproject pins `-m "not integration"`, so this needs no infrastructure.
test:
	$(UV) run pytest

test-integration:
	$(UV) run pytest -m integration
