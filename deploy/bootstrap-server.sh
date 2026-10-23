#!/usr/bin/env bash
# Первичная настройка чистого сервера под gr (deploy-kit): шаги 1–4 из docs/runbooks/deploy.md.
#
#   sudo bash bootstrap-server.sh [site] [ci-public-key]
#
#   site           имя сайта для TLS: npa.example.ru или 203-0-113-10.sslip.io.
#                  Пусто — берётся <публичный-ip с дефисами>.sslip.io, регистрировать ничего не надо.
#   ci-public-key  публичный ключ CI (текст или путь к .pub). Пусто — пара генерируется здесь,
#                  приватный ключ остаётся в /root/gr-deploy-key: его нужно забрать в секрет
#                  DEPLOY_SSH_KEY и удалить с сервера.
#
# Ubuntu 24.04, запуск от root. Повторный запуск безопасен. Что делает:
#   Docker + compose, пользователь deploy (группа docker, без sudo), каталог /opt/gr,
#   ключ CI в ~deploy/.ssh/authorized_keys, Caddy с автоматическим TLS → 127.0.0.1:8000, ufw 22/80/443.
set -euo pipefail

SITE=${1:-}
CI_KEY=${2:-}
DEPLOY_USER=deploy
DEPLOY_DIR=/opt/gr
APP_PORT=8000
KEY_FILE=/root/gr-deploy-key

say() { printf '\n== %s\n' "$*"; }
[[ $(id -u) == 0 ]] || { echo "нужен root: sudo bash $0 ..." >&2; exit 1; }
command -v apt-get >/dev/null || { echo "только Debian/Ubuntu (apt)" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
has_systemd() { [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null; }

if [[ -z $SITE ]]; then
  ip=$(curl -4 -fsS --max-time 10 https://api.ipify.org || curl -4 -fsS --max-time 10 https://ifconfig.me)
  [[ -n $ip ]] || { echo "не удалось узнать публичный IP; укажите имя сайта первым аргументом" >&2; exit 1; }
  SITE="${ip//./-}.sslip.io"
fi
say "сайт: https://$SITE"

say "пакеты: Docker, compose, Caddy, ufw"
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg debian-keyring debian-archive-keyring apt-transport-https \
  docker.io docker-compose-v2 ufw openssh-client
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt >/etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi
if has_systemd; then systemctl enable --now docker >/dev/null; fi
docker --version && docker compose version && caddy version

say "пользователь $DEPLOY_USER и $DEPLOY_DIR"
id -u "$DEPLOY_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$DEPLOY_USER"
usermod -aG docker "$DEPLOY_USER"
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 755 "$DEPLOY_DIR"

say "ключ CI → ~$DEPLOY_USER/.ssh/authorized_keys"
home=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 700 "$home/.ssh"
touch "$home/.ssh/authorized_keys"; chown "$DEPLOY_USER:$DEPLOY_USER" "$home/.ssh/authorized_keys"; chmod 600 "$home/.ssh/authorized_keys"
if [[ -n $CI_KEY ]]; then
  [[ -f $CI_KEY ]] && CI_KEY=$(cat "$CI_KEY")
  pub=$CI_KEY
  key_note="ключ CI взят из аргумента; приватная часть у вас"
else
  if [[ ! -f $KEY_FILE ]]; then ssh-keygen -q -t ed25519 -N '' -C "gr-ci-deploy" -f "$KEY_FILE"; fi
  pub=$(cat "$KEY_FILE.pub")
  key_note="приватный ключ: $KEY_FILE — заберите в DEPLOY_SSH_KEY и удалите: shred -u $KEY_FILE"
fi
grep -qxF "$pub" "$home/.ssh/authorized_keys" || echo "$pub" >>"$home/.ssh/authorized_keys"

say "Caddy: https://$SITE → 127.0.0.1:$APP_PORT"
cat >/etc/caddy/Caddyfile <<CADDY
# gr (deploy-kit): TLS выдаётся автоматически; приложение слушает только localhost.
$SITE {
	encode zstd gzip
	request_body {
		max_size 25MB
	}
	reverse_proxy 127.0.0.1:$APP_PORT
}
CADDY
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
if has_systemd; then systemctl enable --now caddy >/dev/null; systemctl reload caddy; fi

say "ufw: 22, 80, 443"
if has_systemd; then
  ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null; ufw status | head -1
else
  echo "(без systemd: ufw, docker и caddy как службы пропущены — это контейнер, не сервер)"
fi

host_ip=$(curl -4 -fsS --max-time 10 https://api.ipify.org || hostname -I | awk '{print $1}')
cat <<DONE

Готово. Сервер принимает выкладку из GitHub Actions. Дальше на своей машине:

  gh secret set DEPLOY_HOST --body $host_ip
  gh secret set DEPLOY_USER --body $DEPLOY_USER
  gh secret set DEPLOY_SSH_KEY < <приватный ключ CI>     # $key_note
  gh secret set APP_ENV < .env.prod                      # APP_PUBLIC_URL=https://$SITE, см. docs/deploy.md
  gh variable set DEPLOY_ENABLED --body true

Затем Actions → Deliver → Run workflow (пустой tag = текущий main). Проверка: curl -fsS https://$SITE/healthz
Пока приложение не выложено, https://$SITE отвечает 502 — это нормально, сертификат уже есть.
DONE
