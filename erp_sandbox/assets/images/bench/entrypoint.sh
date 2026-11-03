#!/bin/bash
# Wait for the services the bench needs, then hand off to honcho.
#
# Compose's depends_on only says "started", not "usable". MariaDB in particular
# reports itself alive several seconds before it can answer a query -- see
# decision #26 -- and a bench that starts into that window fails with database
# errors that read like application bugs. So the wait happens here as well as in
# the healthcheck: the healthcheck governs orchestration, this governs the one
# process that would otherwise be misled.
#
# Everything configurable arrives in the environment from .env, which came from
# config.toml. This script hardcodes no port and no hostname.

set -euo pipefail

# The compose service names, which double as hostnames on the container
# network. Architecture, not configuration (decision #3) -- overridable only so
# that this script can be exercised outside compose.
MARIADB_HOST="${MARIADB_HOST:-mariadb}"
REDIS_HOST="${REDIS_HOST:-redis}"

WAIT_TIMEOUT="${WAIT_TIMEOUT:-120}"

log() { printf '[entrypoint] %s\n' "$*"; }
die() { printf '[entrypoint] FATAL: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Only starting the long-running stack gets the full treatment.
#
# Anything else -- `bash` to look around, a one-off `bench` command, `node
# --version` from erp_sandbox doctor -- runs straight away. Making an interactive shell
# block for two minutes on a database that is deliberately stopped would be a
# poor way to investigate why the database is stopped.
# ---------------------------------------------------------------------------
if [ "${1:-}" != "honcho" ]; then
    exec "$@"
fi

# ---------------------------------------------------------------------------
# The bench tree, checked before anything slow
#
# It is bind-mounted, so it can be missing or half-built in ways an image never
# can. These checks are local and instant, so they come first: a missing
# Procfile should fail now, not after two minutes of waiting for a database it
# was never going to use.
# ---------------------------------------------------------------------------
[ -n "${BENCH_DIR:-}" ] || die "BENCH_DIR is not set; the image sets it, so this container is not the bench image."
[ -d "${BENCH_DIR}" ]   || die "${BENCH_DIR} is not mounted."
[ -f "${BENCH_DIR}/Procfile" ] || die \
    "${BENCH_DIR}/Procfile is missing. Run erp-sandbox render on the host."
[ -d "${BENCH_DIR}/apps/frappe" ] || die \
    "${BENCH_DIR}/apps/frappe is missing. Run `erp-sandbox fetch` on the host."
[ -x "${BENCH_DIR}/env/bin/python" ] || die \
    "${BENCH_DIR}/env is not built. That is the Phase 5 step; the bench cannot start without it."

require() {
    local name="$1"
    if [ -z "${!name:-}" ]; then
        die "$name is not set. It comes from .env, which erp-sandbox render generates from config.toml."
    fi
}

require INTERNAL_MARIADB_PORT
require INTERNAL_REDIS_PORT

# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------

wait_for() {
    local label="$1" timeout="$2"; shift 2
    local waited=0
    log "waiting for ${label} (up to ${timeout}s)"
    until "$@" >/dev/null 2>&1; do
        waited=$((waited + 1))
        if [ "$waited" -ge "$timeout" ]; then
            printf '[entrypoint] FATAL: %s did not become ready within %ss.\n' "$label" "$timeout" >&2
            printf '[entrypoint] the last attempt reported:\n' >&2
            # Not --silent, and not redirected: the whole point of this line is
            # to show what the failure actually was.
            "$@" 2>&1 | sed 's/^/[entrypoint]   /' >&2 || true
            exit 1
        fi
        sleep 1
    done
    log "${label} ready after ${waited}s"
}

# Decision #26: -h forces TCP. MariaDB's entrypoint runs its temporary init
# server with --skip-networking, so a socket ping would answer during init and
# this would proceed against a database that is not really there. No credential
# is passed: reaching the port at all is the whole signal, and mariadb-admin exits
# 0 on "Access denied" anyway.
#
# --silent is deliberately omitted so the diagnostic above has something to
# print. During polling the output is discarded regardless.
wait_for "mariadb at ${MARIADB_HOST}:${INTERNAL_MARIADB_PORT}" "$WAIT_TIMEOUT" \
    mariadb-admin ping -h "$MARIADB_HOST" -P "$INTERNAL_MARIADB_PORT"

wait_for "redis at ${REDIS_HOST}:${INTERNAL_REDIS_PORT}" "$WAIT_TIMEOUT" \
    redis-cli -h "$REDIS_HOST" -p "$INTERNAL_REDIS_PORT" ping

log "starting: $*"
exec "$@"
