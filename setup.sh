#!/usr/bin/env bash
#
# Muster local setup.
#
# Gets you from a clean checkout to a passing test suite. Does NOT need Docker:
# the whole unit suite runs with no Restate and no Postgres. Add --durable when
# you want the real stack.
#
# Nothing here hides a failure. Every step's exit code is checked, no output is
# discarded, and the script stops at the first problem with a readable message.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_VERSION="3.12"
WITH_DURABLE=0
RUN_TESTS=1
RELOCK=0
INSTALL_UV=0

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
info()  { printf '  %s\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die()   { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
usage: ./setup.sh [options]

  --durable      also install the Restate + Postgres extras (needed for `make dev`)
  --install-uv   install uv if it is missing, instead of just telling you how
  --lock         regenerate uv.lock and the requirements*.txt files, then exit
  --no-tests     skip the verification run
  -h, --help     show this

Default: install the kernel + web dependencies and run the unit suite.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --durable)     WITH_DURABLE=1 ;;
        --install-uv)  INSTALL_UV=1 ;;
        --lock)        RELOCK=1 ;;
        --no-tests)    RUN_TESTS=0 ;;
        -h|--help)     usage; exit 0 ;;
        *)             usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

# ---------------------------------------------------------------------- uv

bold "1. Toolchain"
if ! command -v uv >/dev/null 2>&1; then
    if [[ "$INSTALL_UV" -eq 1 ]]; then
        info "installing uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        # The installer puts uv here; pick it up without needing a new shell.
        export PATH="$HOME/.local/bin:$PATH"
        command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH; open a new shell and re-run"
    else
        die "uv is not installed. Install it with:

    curl -LsSf https://astral.sh/uv/install.sh | sh

or re-run this script with --install-uv to do it automatically.
Muster uses uv because the system Python is often too old (it needs ${PYTHON_VERSION}+)."
    fi
fi
ok "uv $(uv --version | awk '{print $2}')"

# uv downloads and manages the interpreter, so the system Python is never used.
uv python pin "$PYTHON_VERSION" >/dev/null
ok "python ${PYTHON_VERSION} pinned"

# --------------------------------------------------------------- relock only

if [[ "$RELOCK" -eq 1 ]]; then
    bold "Regenerating lockfile and requirements"
    uv lock --upgrade
    header='# Generated from pyproject.toml + uv.lock — do not hand-edit.\n# Regenerate with:  ./setup.sh --lock\n'
    { printf "$header"; uv export --no-hashes --no-emit-project --no-annotate --no-dev \
        --extra durable --extra postgres --format requirements-txt | grep -v '^#'; } > requirements.txt
    { printf "$header"; uv export --no-hashes --no-emit-project --no-annotate \
        --extra durable --extra postgres --group dev --format requirements-txt | grep -v '^#'; } > requirements-dev.txt
    ok "uv.lock, requirements.txt, requirements-dev.txt"
    exit 0
fi

# --------------------------------------------------------------- dependencies

bold "2. Dependencies"
if [[ "$WITH_DURABLE" -eq 1 ]]; then
    uv sync --extra durable --extra postgres
    ok "kernel + web + durable stack (restate-sdk, pydantic-ai, psycopg)"
else
    uv sync
    ok "kernel + web (add --durable for the Restate stack)"
fi

# ---------------------------------------------------------------- environment

bold "3. Environment"
if [[ -f .env ]]; then
    ok ".env already present, left untouched"
else
    cp .env.example .env
    ok ".env created from .env.example"
    info "no edits needed to run the tests or the demo — the LLM defaults to a"
    info "deterministic stub, so no model endpoint or API key is required."
fi

mkdir -p data/artifacts
ok "data/artifacts ready"

# --------------------------------------------------------------- verification

if [[ "$RUN_TESTS" -eq 1 ]]; then
    bold "4. Verify"
    info "running the unit suite (no Docker, no Restate, no Postgres)..."
    uv run pytest
    ok "suite passed"
fi

# ----------------------------------------------------------------- next steps

bold "Ready."
cat <<'NEXT'

  uv run pytest                      run the tests again
  make help                          see every target

  Durable stack (needs Docker):
    ./setup.sh --durable             install the extras
    make up                          start Restate + Postgres
    make migrate                     create the schema, seed subscriptions
    make dev                         run the service and register it
    uv run python -m app.main web    timeline + approvals on :8000

NEXT
