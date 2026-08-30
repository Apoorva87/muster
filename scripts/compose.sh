#!/usr/bin/env bash
#
# One compose file, two engines.
#
#   Docker            `docker compose` — what Coolify runs on the server.
#   Apple Container   `container-compose` — Apple's native Linux-container
#                     runtime on macOS, via a shim that reads the same
#                     docker-compose.yml. No Docker Desktop required.
#
# Auto-detects, or force one with MUSTER_ENGINE=docker|apple.
#
# Usage:  scripts/compose.sh up|down|ps|logs|wait [args...]

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
RESTATE_ADMIN_PORT="${RESTATE_ADMIN_PORT:-9070}"
export POSTGRES_PORT RESTATE_ADMIN_PORT

die() { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '  %s\n' "$*"; }

# ------------------------------------------------------------ engine choice

detect_engine() {
    if [[ -n "${MUSTER_ENGINE:-}" ]]; then
        echo "$MUSTER_ENGINE"; return
    fi
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        echo docker; return
    fi
    if command -v container-compose >/dev/null 2>&1; then
        echo apple; return
    fi
    echo none
}

ENGINE="$(detect_engine)"

case "$ENGINE" in
    docker) COMPOSE=(docker compose -f "$COMPOSE_FILE") ;;
    apple)  COMPOSE=(container-compose -f "$COMPOSE_FILE") ;;
    none)
        die "no container engine found.

  macOS (recommended here):
      brew install container container-compose
      container system start
      container system kernel set --recommended

  Or Docker Desktop / Docker Engine, then start the daemon.

  Force a choice with MUSTER_ENGINE=docker|apple."
        ;;
    *) die "unknown MUSTER_ENGINE=$ENGINE (expected 'docker' or 'apple')" ;;
esac

# --------------------------------------------------------------- port guard

port_in_use() {
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

check_ports() {
    # A native Postgres on 5432 is common on a dev laptop, and the container
    # failure it causes ("Address already in use", errno 48) is opaque.
    if port_in_use "$POSTGRES_PORT"; then
        local owner
        owner="$(lsof -nP -iTCP:"$POSTGRES_PORT" -sTCP:LISTEN 2>/dev/null \
                 | awk 'NR==2 {print $1}')"
        die "port $POSTGRES_PORT is already held by '${owner:-something}'.

  Pick another and re-run, e.g.:
      POSTGRES_PORT=5433 make up

  Then use the same value for migrate/serve, or set it in .env."
    fi
}

# ---------------------------------------------------------------- readiness

wait_ready() {
    # container-compose has no `--wait`, sohealthchecks do not gate startup.
    # Poll the two endpoints that actually matter instead.
    local deadline=$((SECONDS + ${MUSTER_WAIT_TIMEOUT:-180}))
    info "waiting for Restate on :$RESTATE_ADMIN_PORT and Postgres on :$POSTGRES_PORT"
    while (( SECONDS < deadline )); do
        local restate_ok=0 pg_ok=0
        curl -fsS -o /dev/null "http://127.0.0.1:${RESTATE_ADMIN_PORT}/health" 2>/dev/null && restate_ok=1
        if [[ "$ENGINE" == apple ]]; then
            container exec muster-postgres pg_isready -q -U "${POSTGRES_USER:-muster}" >/dev/null 2>&1 && pg_ok=1
        else
            docker exec muster-postgres pg_isready -q -U "${POSTGRES_USER:-muster}" >/dev/null 2>&1 && pg_ok=1
        fi
        if (( restate_ok && pg_ok )); then
            info "both healthy"
            return 0
        fi
        sleep 2
    done
    die "stack did not become healthy within ${MUSTER_WAIT_TIMEOUT:-180}s. Try: scripts/compose.sh logs"
}

# ------------------------------------------------------------------ actions

action="${1:-up}"; shift || true

case "$action" in
    engine) echo "$ENGINE" ;;
    up)
        check_ports
        info "engine: $ENGINE"
        "${COMPOSE[@]}" up -d "$@"
        wait_ready
        ;;
    down)
        # container-compose has no --volumes; Apple Container manages named
        # volumes through its own `container volume` command instead.
        purge=0
        args=()
        for a in "$@"; do
            case "$a" in
                --volumes|-v) purge=1 ;;
                *) args+=("$a") ;;
            esac
        done
        if [[ "$ENGINE" == apple ]]; then
            "${COMPOSE[@]}" down "${args[@]+"${args[@]}"}" || true
            if (( purge )); then
                # A stopped container still holds its volume, so remove the
                # containers before the volumes or the delete silently no-ops.
                for name in muster-restate muster-postgres; do
                    container delete "$name" >/dev/null 2>&1 || true
                done
                for vol in $(container volume list 2>/dev/null | awk 'NR>1 && $1 ~ /^muster_/ {print $1}'); do
                    info "removing volume $vol"
                    container volume delete "$vol" >/dev/null 2>&1 \
                        || info "  (still in use — run 'container list -a')"
                done
            fi
        else
            (( purge )) && args+=(--volumes)
            "${COMPOSE[@]}" down "${args[@]+"${args[@]}"}"
        fi
        ;;
    build)  "${COMPOSE[@]}" build "$@" ;;
    wait)   wait_ready ;;
    ps)
        if [[ "$ENGINE" == apple ]]; then container list "$@"; else docker compose -f "$COMPOSE_FILE" ps "$@"; fi
        ;;
    logs)
        if [[ "$ENGINE" == apple ]]; then
            for name in muster-restate muster-postgres; do
                printf '\n=== %s ===\n' "$name"; container logs "$name" 2>&1 | tail -40
            done
        else
            docker compose -f "$COMPOSE_FILE" logs "$@"
        fi
        ;;
    *) die "unknown action '$action' (up|down|build|wait|ps|logs|engine)" ;;
esac
