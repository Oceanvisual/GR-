#!/usr/bin/env bash
# deploy-kit: выкладка на сервере (из CI по ssh или руками). Идемпотентно.
#   IMAGE, TAG                       — что выкатывать (TAG = sha коммита, неизменяемый)
#   APP_ENV                          — опц.: полное содержимое .env (иначе берётся существующий .env)
#   REGISTRY_USER, REGISTRY_TOKEN    — опц.: логин в реестр для приватных образов
#   HEALTH_TIMEOUT (сек, 120), SKIP_PULL=1 (для локальных тестов)
# Успех = контейнер app стал healthy по своему HEALTHCHECK. Иначе — откат на прошлый тег и exit 1.
set -euo pipefail
cd "$(dirname "$0")"
: "${IMAGE:?IMAGE не задан}" "${TAG:?TAG не задан}"

compose() { local tag=$1; shift; IMAGE="$IMAGE" TAG="$tag" docker compose -f compose.prod.yml "$@"; }

wait_healthy() {
  local tag=$1 id status deadline=$((SECONDS + ${HEALTH_TIMEOUT:-120}))
  id=$(compose "$tag" ps -q app)
  while (( SECONDS < deadline )); do
    status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo missing)
    case $status in
      healthy) return 0 ;;
      unhealthy|exited|dead|missing) echo "status=$status"; return 1 ;;
    esac
    sleep 3
  done
  echo "status=$status (таймаут)"; return 1
}

if [[ -n "${APP_ENV:-}" ]]; then (umask 077; printf '%s\n' "$APP_ENV" > .env); fi
[[ -f .env ]] || (umask 077; : > .env)

if [[ -n "${REGISTRY_TOKEN:-}" ]]; then
  printf '%s' "$REGISTRY_TOKEN" | docker login "${IMAGE%%/*}" -u "${REGISTRY_USER:?}" --password-stdin >/dev/null
  trap 'docker logout "${IMAGE%%/*}" >/dev/null 2>&1 || true' EXIT
fi

prev=$(cat .deployed-tag 2>/dev/null || true)
[[ "${SKIP_PULL:-0}" == 1 ]] || compose "$TAG" pull app
compose "$TAG" up -d --remove-orphans

if wait_healthy "$TAG"; then
  echo "$TAG" > .deployed-tag
  echo "$(date -u +%FT%TZ) $TAG" >> .deploy-history
  docker image prune -f >/dev/null || true
  echo "deployed $IMAGE:$TAG"
  exit 0
fi

echo "::error::$IMAGE:$TAG не стал healthy"
compose "$TAG" logs --tail 80 app || true
if [[ -n "$prev" && "$prev" != "$TAG" ]]; then
  echo "откат на $prev"
  compose "$prev" up -d --remove-orphans
  wait_healthy "$prev" && echo "откат на $prev успешен" || echo "::error::откат тоже не поднялся"
fi
exit 1
