# Развёртывание на сервере

Сервис — один процесс FastAPI (`gr serve`) с SQLite и файлами в `DATA_DIR`. Фоновые анализы и
OCR выполняются внутри него, тяжёлый разбор — в дочерних процессах. Наружу сервис выходит через
реверс-прокси с TLS.

> **Docker-образ, `docker-compose.yml` и доставка через GitHub Actions** появятся в задаче T056
> (`/deploy-kit`, изменение CI и деплоя — по согласованию). До неё сервис запускается как
> обычный процесс: раздел «Без Docker» ниже. Требования, настройки, прокси, бэкап и проверки
> одинаковы для обоих способов.

## Требования

**Минимум для одного-двух пользователей** `[Прогноз]`: 2 vCPU, 4 ГБ RAM, 10 ГБ диска под
`DATA_DIR`.

**Программы:**
- Python 3.12 и [uv];
- `tesseract-ocr` с языками `rus` и `eng` — для сканов;
- `libreoffice-writer-nogui` (или полный LibreOffice) — для DOC, RTF и ODT.

Пакеты Debian/Ubuntu: `tesseract-ocr tesseract-ocr-rus libreoffice-writer-nogui`.

**Нагрузка, замеры спайка T073** (aarch64, `OMP_THREAD_LIMIT=1`):
- страница скана распознаётся за ~3 с, скан на 150 страниц — около 8 минут при
  `OCR_CONCURRENCY=1`;
- конвертация DOC → DOCX занимает меньше 1 с.

**Сеть:** исходящий доступ к LLM endpoint, если политика `external_allowed`.

[uv]: https://docs.astral.sh/uv/

## Настройки сервера

Все настройки — переменные окружения. Шаблон со всеми ключами — `.env.example`. На сервере
обязательны:

| Переменная | Значение |
|---|---|
| `APP_ENV` | `server`: без входа сервис не запускается |
| `APP_PUBLIC_URL` | внешний адрес, например `https://npa.example.ru`; по нему проверяется `Origin` форм |
| `AUTH_USERS` | `login:scrypt$…` через запятую; строку даёт `uv run gr hash-password --login <login>` |
| `DATA_DIR` | каталог данных, например `/var/lib/gr` |
| `FORWARDED_ALLOW_IPS` | адрес реверс-прокси (`127.0.0.1`, если он на той же машине) — тогда uvicorn верит его `X-Forwarded-*` |
| `LLM_*` | политика, endpoint и модели — см. README, раздел «LLM» |
| `LOG_FORMAT` | `json` для сборщика логов |

Ключ LLM (`LLM_API_KEY`) и пароли в репозиторий не попадают. Файл с переменными доступен
только пользователю сервиса.

## Без Docker (systemd)

```bash
sudo useradd --system --home /opt/gr gr
sudo -u gr git clone <репозиторий> /opt/gr/app && cd /opt/gr/app
sudo -u gr uv sync --frozen --no-dev
sudo install -d -o gr -g gr -m 700 /var/lib/gr
```

`/etc/systemd/system/gr.service`:

```ini
[Unit]
Description=AI-анализ НПА
After=network-online.target

[Service]
User=gr
WorkingDirectory=/opt/gr/app
EnvironmentFile=/etc/gr/gr.env
Environment=OMP_THREAD_LIMIT=1
ExecStart=/opt/gr/app/.venv/bin/gr serve --host 127.0.0.1
Restart=on-failure
# Остановка даёт фоновым задачам завершиться штатно: прерванный анализ становится
# «прерван», прерванный OCR возвращается в очередь при следующем старте.
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now gr
curl -fsS http://127.0.0.1:8000/healthz      # {"status":"ok"}
```

Сервис работает строго в одном процессе (`workers=1`): очередь анализов и блокировка записи
SQLite живут в нём. Масштабирование — только вертикальное.

## Реверс-прокси и TLS

Пример для nginx: лимит тела чуть больше `MAX_UPLOAD_MB` (20 МБ), таймаут — больше времени
синхронного разбора, заголовки `X-Forwarded-*`.

```nginx
server {
    listen 443 ssl;
    server_name npa.example.ru;
    ssl_certificate     /etc/letsencrypt/live/npa.example.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/npa.example.ru/privkey.pem;

    client_max_body_size 25m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_read_timeout 120s;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
    }
}

server {
    listen 80;
    server_name npa.example.ru;
    return 301 https://$host$request_uri;
}
```

`APP_PUBLIC_URL` должен совпадать с адресом, который видит браузер: формы с другого `Origin`
отклоняются (`403 csrf_rejected`).

## Проверки работоспособности

| Адрес | Вход | Что значит |
|---|---|---|
| `GET /healthz` | не нужен | процесс жив (для Docker и мониторинга) |
| `GET /api/v1/readyz` | нужен | база доступна, LLM настроен по политике, OCR доступен (языки) |

`readyz.ocr.available=false` с причиной означает, что сканы сохраняются, но не распознаются, пока
не установлен tesseract. После установки — перезапуск сервиса и «Повторить распознавание» на
странице документа.

## Бэкап и восстановление

Всё состояние — в `DATA_DIR`:
- `gr.db` — документы, структура, анализы, решения эксперта;
- `files/` — исходные файлы по sha256.

```bash
# горячая копия базы (сервис работает) и файлов
sqlite3 /var/lib/gr/gr.db ".backup '/backup/gr-$(date +%F).db'"
rsync -a /var/lib/gr/files/ /backup/files/
```

Восстановление: остановить сервис, положить `gr.db` и `files/` в `DATA_DIR`, запустить.

## Обновление

```bash
cd /opt/gr/app && sudo -u gr git pull && sudo -u gr uv sync --frozen --no-dev
sudo systemctl restart gr
```

При старте недостающие таблицы создаются. Миграций в MVP нет: если версия схемы в базе не
совпадает с версией кода, сервис не стартует (`SchemaVersionError`), и перед таким обновлением
нужен бэкап и перенос данных. Анализ, прерванный перезапуском, получает статус «прерван» и
продолжается кнопкой «Продолжить» с первого незавершённого этапа.

## Ручная проверка Q13: перезапуск во время анализа

1. Загрузить документ и запустить анализ.
2. Пока идёт этап S4, перезапустить сервис: `sudo systemctl restart gr`, а после T056 —
   `docker compose restart app`.
3. Открыть анализ: статус «прерван», готовые этапы отмечены выполненными.
4. Нажать «Продолжить»: анализ завершается. Готовые этапы не повторяются: число «Вызовов LLM»
   на экране анализа растёт только на незавершённых этапах.

Автоматический вариант этой проверки — `tests/integration/test_restart_resume.py`.
