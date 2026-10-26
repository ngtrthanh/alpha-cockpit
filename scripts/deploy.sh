#!/usr/bin/env bash
set -Eeuo pipefail

TARGET_SHA="${1:-}"
REPO_URL="${REPO_URL:-https://github.com/ngtrthanh/alpha-cockpit.git}"
DEPLOY_DIR="${DEPLOY_DIR:-$HOME/alpha-cockpit-prod}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:6868/api/health}"
RUNTIME_URL="${RUNTIME_URL:-http://127.0.0.1:6868/api/runtime}"
LOCK_FILE="${LOCK_FILE:-$HOME/.alpha-cockpit-deploy.lock}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/ngtrthanh/alpha-cockpit}"

if [[ -z "$TARGET_SHA" ]]; then
  echo "usage: deploy.sh <git-sha>" >&2
  exit 64
fi

for cmd in git docker curl flock python3; do
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
PREVIOUS_CONTAINER_ID="$(docker compose ps -q alpha-cockpit 2>/dev/null || true)"
PREVIOUS_IMAGE=""
if [[ -n "$PREVIOUS_CONTAINER_ID" ]]; then
  PREVIOUS_IMAGE="$(docker inspect "$PREVIOUS_CONTAINER_ID" --format '{{.Config.Image}}' 2>/dev/null || true)"
fi
TARGET_IMAGE="$IMAGE_REPO:sha-$TARGET_SHA"

json_field() {
  local field="$1"
  python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$field"
}

current_health_sha() {
  curl -fsS "$HEALTH_URL" 2>/dev/null | json_field commit 2>/dev/null || true
}

worker_status() {
  curl -fsS "$RUNTIME_URL" 2>/dev/null | python3 -c '
import json,sys
data=json.load(sys.stdin)
for row in data.get("services", []):
    if row.get("service") == "worker":
        print(row.get("status", ""))
        break
' 2>/dev/null || true
}

CURRENT_HEALTH_SHA="$(current_health_sha)"
if [[ "$PREVIOUS_SHA" == "$TARGET_SHA" && "$CURRENT_HEALTH_SHA" == "$TARGET_SHA" && "$(worker_status)" == "ok" ]]; then
  echo "already deployed and healthy: $TARGET_SHA"
  exit 0
fi

# Prevent delayed workflow_run events from downgrading a newer healthy deployment.
if [[ -n "$PREVIOUS_SHA" && "$CURRENT_HEALTH_SHA" == "$PREVIOUS_SHA" ]] &&
   git cat-file -e "${PREVIOUS_SHA}^{commit}" 2>/dev/null &&
   git merge-base --is-ancestor "$TARGET_SHA" "$PREVIOUS_SHA" 2>/dev/null; then
  echo "skipping stale deployment $TARGET_SHA; healthy deployment is newer: $PREVIOUS_SHA"
  exit 0
fi

if [[ ! -f .env ]]; then
  echo "missing $DEPLOY_DIR/.env; bootstrap secrets/config before enabling self-deploy" >&2
  exit 78
fi

backup_db() {
  local stamp backup_rel backup_abs backup_name
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p data/backups
  backup_rel="data/backups/investment-${stamp}-${PREVIOUS_SHA:0:12}.db"
  backup_abs="$DEPLOY_DIR/$backup_rel"
  backup_name="$(basename "$backup_rel")"

  if docker compose ps --status running --services 2>/dev/null | grep -qx alpha-cockpit; then
    echo "creating SQLite online backup: $backup_rel" >&2
    docker compose exec -T alpha-cockpit python -c       "import sqlite3; s=sqlite3.connect('/app/data/investment.db'); d=sqlite3.connect('/app/data/backups/$backup_name'); s.backup(d); d.close(); s.close()"
  elif [[ -f data/investment.db ]]; then
    echo "app is stopped; copying SQLite database: $backup_rel" >&2
    cp -a data/investment.db "$backup_abs"
  else
    backup_abs=""
  fi
  printf '%s' "$backup_abs"
}

wait_for_health() {
  local expected_sha="$1"
  local allow_legacy="${2:-false}"
  local i health health_sha health_ok worker
  for i in $(seq 1 60); do
    health="$(curl -fsS "$HEALTH_URL" 2>/dev/null || true)"
    health_sha=""
    health_ok=""
    if [[ -n "$health" ]]; then
      health_sha="$(printf '%s' "$health" | json_field commit 2>/dev/null || true)"
      health_ok="$(printf '%s' "$health" | json_field ok 2>/dev/null || true)"
    fi
    worker="$(worker_status)"
    if [[ "$worker" == "ok" ]]; then
      if [[ "$health_sha" == "$expected_sha" ]]; then
        return 0
      fi
      if [[ "$allow_legacy" == "true" && "$health_ok" == "True" && ( -z "$health_sha" || "$health_sha" == "unknown" ) ]]; then
        return 0
      fi
    fi
    sleep 2
  done
  return 1
}

restore_db() {
  local backup="$1"
  local image="$2"
  [[ -n "$backup" && -f "$backup" ]] || return 0
  local backup_name
  backup_name="$(basename "$backup")"
  echo "restoring SQLite backup: $backup_name" >&2
  docker compose stop worker alpha-cockpit >/dev/null 2>&1 || true
  docker run --rm     -v "$DEPLOY_DIR/data:/app/data"     "$image"     python -c "import os,shutil; shutil.copy2('/app/data/backups/$backup_name','/app/data/investment.db'); [os.remove(p) for p in ('/app/data/investment.db-wal','/app/data/investment.db-shm') if os.path.exists(p)]"
}

BACKUP_PATH="$(backup_db)"

echo "deploying $TARGET_SHA using $TARGET_IMAGE (previous: ${PREVIOUS_SHA:-none})"
git reset --hard "$TARGET_SHA"

export APP_COMMIT_SHA="$TARGET_SHA"
export ALPHA_IMAGE="$TARGET_IMAGE"

# Validate production config and pull the exact CI-published multi-arch artifact
# before touching running services.
docker compose config >/dev/null
docker pull "$TARGET_IMAGE"

docker compose up -d --no-build --remove-orphans

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

echo "rolling back to $PREVIOUS_SHA using previous image ${PREVIOUS_IMAGE:-<rebuild>}" >&2
restore_db "$BACKUP_PATH" "$TARGET_IMAGE"
git reset --hard "$PREVIOUS_SHA"
export APP_COMMIT_SHA="$PREVIOUS_SHA"

if [[ -n "$PREVIOUS_IMAGE" ]]; then
  export ALPHA_IMAGE="$PREVIOUS_IMAGE"
  docker compose config >/dev/null
  docker compose up -d --no-build --remove-orphans
else
  unset ALPHA_IMAGE
  docker compose config >/dev/null
  docker compose build alpha-cockpit worker
  docker compose up -d --remove-orphans
fi

if wait_for_health "$PREVIOUS_SHA" true; then
  echo "rollback healthy: $PREVIOUS_SHA" >&2
  exit 1
fi

echo "CRITICAL: rollback also failed" >&2
docker compose ps >&2 || true
docker compose logs --no-color --tail=200 alpha-cockpit worker hermes >&2 || true
exit 2
