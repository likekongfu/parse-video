#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/deploy.sh parse-video-py
  ./scripts/deploy.sh document-converter-word2pdf
  ./scripts/deploy.sh media-converter
  ./scripts/deploy.sh all
  ./scripts/deploy.sh status
Options:
  --no-pull   Skip git pull
EOF
}

TARGET="${1:-}"
PULL=1

if [[ -z "$TARGET" ]]; then
  usage
  exit 2
fi
shift || true

for arg in "$@"; do
  case "$arg" in
    --no-pull) PULL=0 ;;
    *) echo "Unknown option: $arg"; usage; exit 2 ;;
  esac
done

case "$TARGET" in
  parse-video-py|document-converter-word2pdf|media-converter)
    SERVICES=("$TARGET")
    ;;
  all)
    SERVICES=(parse-video-py document-converter-word2pdf media-converter)
    ;;
  status)
    docker compose ps
    exit 0
    ;;
  *)
    echo "Unknown target: $TARGET"
    usage
    exit 2
    ;;
esac

for network in longtian_gateway longtian_internal; do
  docker network inspect "$network" >/dev/null 2>&1 || {
    echo "Missing external Docker network: $network"
    exit 1
  }
done

for file in \
  env/parse-video.env \
  env/document-converter.env \
  env/media-converter.env \
  env/longtian-mysql.env
do
  [[ -f "$file" ]] || {
    echo "Missing server environment file: $file"
    exit 1
  }
done

if [[ "$PULL" -eq 1 ]]; then
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Working tree has tracked changes; refusing to pull."
    git status --short
    exit 1
  fi
  git pull --ff-only
fi

docker compose config -q

MYSQL_HEALTH="$(
  docker inspect longtian-mysql \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    2>/dev/null || true
)"
if [[ "$MYSQL_HEALTH" != "healthy" ]]; then
  echo "longtian-mysql is not healthy: ${MYSQL_HEALTH:-missing}"
  exit 1
fi

AVAILABLE_KB="$(df -Pk / | awk 'NR==2 {print $4}')"
MIN_KB=$((3 * 1024 * 1024))
if (( AVAILABLE_KB < MIN_KB )); then
  echo "Less than 3 GiB free on /. Build cancelled."
  df -h /
  exit 1
fi

wait_healthy() {
  local service="$1"
  local status=""
  for _ in $(seq 1 80); do
    status="$(
      docker inspect "$service" \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        2>/dev/null || true
    )"
    printf '%s: %s\n' "$service" "${status:-missing}"
    [[ "$status" == "healthy" ]] && return 0
    [[ "$status" == "unhealthy" || "$status" == "exited" || "$status" == "dead" ]] && break
    sleep 3
  done

  docker compose logs --tail=150 "$service" || true
  return 1
}

for service in "${SERVICES[@]}"; do
  echo "===== Building $service ====="
  docker compose build "$service"

  echo "===== Recreating $service ====="
  docker compose up -d --no-deps "$service"

  wait_healthy "$service"
done

docker compose ps
