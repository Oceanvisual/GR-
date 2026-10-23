# Деплой gr (deploy-kit)

Сейчас: каждый зелёный CI на `main` публикует образ `ghcr.io/<owner>/<repo>:<sha>` (+`latest`).
На сервер ничего не выкатывается, пока не выполнены шаги ниже.

## Как устроено
- `Dockerfile` — non-root, `HEALTHCHECK` на `/healthz` (порт 8000).
- `.github/workflows/deliver.yml` — publish после CI «CI»; deploy при `vars.DEPLOY_ENABLED == 'true'`.
- `deploy/compose.prod.yml` + `deploy/remote.sh` копируются на сервер в `DEPLOY_DIR` (по умолчанию `/opt/gr`).
  Git и исходники на сервере не нужны.
- Успех деплоя = контейнер стал `healthy`. Иначе `remote.sh` откатывает на тег из `.deployed-tag` и job падает.
- История: `DEPLOY_DIR/.deploy-history`.

## Включить деплой (один раз на сервер)

Быстрый путь — шаги 1, 2 и 4 одной командой на чистом Ubuntu 24.04
(`deploy/bootstrap-server.sh`): Docker и compose, пользователь `deploy`, `/opt/gr`, ключ CI,
Caddy с автоматическим TLS, ufw. Имя сайта без домена — `<ip-через-дефисы>.sslip.io`, оно
подставится само:
```bash
scp deploy/bootstrap-server.sh root@<ip>:/root/ && ssh root@<ip> 'bash /root/bootstrap-server.sh'
# свой домен и ключ CI, сгенерированный у себя (ssh-keygen -t ed25519 -f deploy_key -N ''):
ssh root@<ip> 'bash /root/bootstrap-server.sh npa.example.ru "$(cat deploy_key.pub)"'
```
В конце скрипт печатает команды `gh secret set` с подставленными значениями. Остаётся шаг 3 и 5.

Руками:
1. Сервер с Docker + compose plugin. Пользователь `deploy` в группе `docker`:
   `sudo useradd -m -G docker deploy && sudo mkdir -p /opt/gr && sudo chown deploy /opt/gr`
2. SSH-ключ только для CI: `ssh-keygen -t ed25519 -f deploy_key -N ''`, публичный → `~deploy/.ssh/authorized_keys`.
3. Секреты и переменные репо:
   ```bash
   gh secret set DEPLOY_HOST --body <ip>
   gh secret set DEPLOY_USER --body deploy
   gh secret set DEPLOY_SSH_KEY < deploy_key
   gh secret set APP_ENV < .env.prod          # весь .env прода одним секретом
   # приватный образ: PAT с read:packages
   gh secret set REGISTRY_USER --body <github-login>; gh secret set REGISTRY_TOKEN --body <pat>
   gh variable set DEPLOY_DIR --body /opt/gr   # опционально
   gh variable set DEPLOY_ENABLED --body true
   ```
4. Reverse proxy с TLS (пример Caddy): `gr.example.com { reverse_proxy 127.0.0.1:8000 }`.
5. Содержимое секрета `APP_ENV` — пример и обязательные ключи в [docs/deploy.md](../deploy.md),
   раздел Docker. `AUTH_USERS` — **в одинарных кавычках**: иначе compose съедает `$…` из хэша
   пароля, и контейнер не стартует (`AUTH_USERS: … expected login:scrypt$<salt>$<hash>`).
   `FORWARDED_ALLOW_IPS=*`, `APP_PUBLIC_URL` — внешний https-адрес.

## Специфика gr
- Образ включает tesseract (`rus`, `eng`) и LibreOffice: OCR сканов и `.doc`/`.rtf`/`.odt`.
- Данные — том `data` (`/data`): SQLite и исходные файлы; бэкап — docs/deploy.md.
- Один процесс сервиса: не масштабировать `--scale app=N`, очередь анализов и SQLite — в процессе.
- `stop_grace_period: 30s`: при выкладке идущий анализ получает статус «прерван», его продолжают
  кнопкой «Продолжить»; прерванный OCR встаёт в очередь сам.

## Операции
- Откат: Actions → Deliver → Run workflow → `tag=<sha>` (или на сервере: `IMAGE=... TAG=<sha> ./remote.sh`).
- Выключить деплой, не трогая сборку: `gh variable set DEPLOY_ENABLED --body false`.
- Локальная проверка образа: `docker build -t gr:dev . && docker run --rm -p 8000:8000 --env-file .env gr:dev`.
