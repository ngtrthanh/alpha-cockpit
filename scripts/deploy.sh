#!/usr/bin/env bash
set -Eeuo pipefail

TARGET_SHA="${1:-}"
REPO_URL="${REPO_URL:-https://github.com/ngtrthanh/alpha-cockpit.git}"
DEPLOY_DIR="${DEPLOY_DIR:-$HOME/alpha-cockpit-prod}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:6868/api/health}"
RUNTIME_URL="${RUNTIME_URL:-http://127.0.0.1:6868/api/runtime}"
LOCK_FILE="${LOCK_FILE:-$HOME/.alpha-cockpit-deploy.lock}"

if [[ -z "$TARGET_SHA" ]]; then
  echo "usage: deploy.sh <git-sha>" >&2
  exit 64
fi

for cmd in git docker curl flock; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "missing required command: $cmd" >&2; exit 69; }
done

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "another Alpha Cockpit deployment is already running" >&2
  exit 75
fi

mkdir -p "$(dirname "$DEPLOY_DIR")"

if [[ ! -d "$DEPLOY_DIR/.git" ]]; then
  if [[ -e "$DEPLOY_DIR" ]] && [[ -n "$(ls -A "$DEPLOY_DIR" 2>/dev/null || true)" ]]; then
    echo "DEPLOY_DIR exists but is not a git checkout: $DEPLOY_DIR" >&2
    exit 73
  fi
  git clone "$REPO_URL" "$DEPLOY_DIR"
fi

cd "$DEPLOY_DIR"
git fetch --prune origin main

if ! git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null; then
  echo "target commit not found after fetch: $TARGET_SHA" >&2
  exit 65
fi

if ! git branch -r --contains "$TARGET_SHA" | grep -qE 'origin/main$'; then
  echo "refusing to deploy commit that is not reachable from origin/main: $TARGET_SHA" >&2
  exit 65
fi

PREVIOUS_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
if [[ "$PREVIOUS_SHA" == "$TARGET_SHA" ]]; then
  echo "already deployed: $TARGET_SHA"
  exit 0
fi

# Prevent delayed workflow_run events from downgrading a newer successful deployment.
if [[ -n "$PREVIOUS_SHA" ]] && git cat-file -e "${PREVIOUS_SHA}^{commit}" 2>/dev/null; then
  if git merge-base --is-ancestor "$TARGET_SHA" "$PREVIOUS_SHA" 2>/dev/null; then
    echo "skipping stale deployment $TARGET_SHA; current deployment is newer: $PREVIOUS_SHA"
    exit 0
  fi
fi

if [[ ! -f .env ]]; then
  echo "missing $DEPLOY_DIR/.env; bootstrap secrets/config before enabling self-deploy" >&2
  exit 78
fi

backup_db() {
  local stamp backup_rel backup_abs
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p data/backups
  backup_rel="data/backups/investment-${stamp}-${PREVIOUS_SHA:0:12}.db"
  backup_abs="$DEPLOY_DIR/$backup_rel"

  if docker compose ps --status running --services 2>/dev/null | grep -qx alpha-cockpit; then
    echo "creating SQLite online backup: $backup_rel"
    docker compose exec -T alpha-cockpit python -c       "import sqlite3; s=sqlite3.connect('/app/data/investment.db'); d=sqlite3.connect('/app/data/backups/$(basename "$backup_rel")'); s.backup(d); d.close(); s.close()"
  elif [[ -f data/investment.db ]]; then
    echo "app is stopped; copying SQLite database: $backup_rel"
    cp -a data/investment.db "$backup_abs"
  else
    backup_abs=""
  fi
  printf '%s' "$backup_abs"
}

wait_for_health() {
  local expected_sha="$1"
  local i health runtime
  for i in $(seq 1 60); do
    health="$(curl -fsS "$HEALTH_URL" 2>/dev/null || true)"
    if [[ -n "$health" ]] && grep -Fq "\"commit\": \"$expected_sha\"" <<<"$health"; then
      runtime="$(curl -fsS "$RUNTIME_URL" 2>/dev/null || true)"
      if grep -Fq '"service": "worker"' <<<"$runtime" && grep -Fq '"status": "ok"' <<<"$runtime"; then
        return 0
      fi
    fi
    sleep 2
  done
  return 1
}

restore_db() {
  local backup="$1"
  [[ -n "$backup" && -f "$backup" ]] || return 0
  echo "restoring SQLite backup: $backup"
  docker compose stop worker alpha-cockpit >/dev/null 2>&1 || true
  cp -a "$backup" data/investment.db
  rm -f data/investment.db-wal data/investment.db-shm
}

BACKUP_PATH="$(backup_db)"

echo "deploying $TARGET_SHA (previous: ${PREVIOUS_SHA:-none})"
git reset --hard "$TARGET_SHA"

# Validate with the production .env before touching running services.
docker compose config >/dev/null

export APP_COMMIT_SHA="$TARGET_SHA"
docker compose build --pull alpha-cockpit worker
docker compose up -d --remove-orphans

if wait_for_health "$TARGET_SHA"; then
  echo "deployment healthy: $TARGET_SHA"
  docker compose ps
  exit 0
fi

echo "deployment health gate failed for $TARGET_SHA" >&2
docker compose ps >&2 || true
docker compose logs --no-color --tail=160 alpha-cockpit worker hermes >&2 || true

if [[ -z "$PREVIOUS_SHA" ]] || ! git cat-file -e "${PREVIOUS_SHA}^{commit}" 2>/dev/null; then
  echo "no previous commit available for rollback" >&2
  exit 1
fi

echo "rolling back to $PREVIOUS_SHA" >&2
restore_db "$BACKUP_PATH"
git reset --hard "$PREVIOUS_SHA"
export APP_COMMIT_SHA="$PREVIOUS_SHA"
docker compose config >/dev/null
docker compose build alpha-cockpit worker
docker compose up -d --remove-orphans

if wait_for_health "$PREVIOUS_SHA"; then
  echo "rollback healthy: $PREVIOUS_SHA" >&2
  exit 1
fi

echo "CRITICAL: rollback also failed" >&2
docker compose ps >&2 || true
docker compose logs --no-color --tail=200 alpha-cockpit worker hermes >&2 || true
exit 2
