# IG CRM — Deploy / Run Runbook (для теста)

> Схема на завтра: **фронт на Vercel** (публичный https) → **ngrok** (https) →
> **бэк локально** (FastAPI :8000) + Postgres + Redis + Celery worker/beat.
> Кент открывает Vercel-ссылку из любой точки интернета.

---

## 0. Карта портов и процессов

| Что | Где | Порт | Команда (кратко) |
|---|---|---|---|
| PostgreSQL | docker локально | **5433** | `sudo docker start ig_crm_db` |
| Redis | локально | 6379 | (уже запущен как сервис) |
| FastAPI (бэк) | локально | **8000** | `uvicorn app.api.main:app` |
| Celery worker | локально | — | `celery ... worker` (нужен для реального IG) |
| Celery beat | локально | — | `celery ... beat` (джанитор) |
| Next.js (фронт) | **Vercel** | 443 | деплой из репо |
| ngrok | туннель на бэк | — | `ngrok http 8000` |

⚠️ Главное правило: фронт (в браузере) дёргает API напрямую, поэтому
`NEXT_PUBLIC_API_URL` на Vercel = **публичный https-URL ngrok**, а НЕ localhost.

---

## 1. Поднять данные (один раз)

```bash
# Postgres в docker на порту 5433 (если контейнера ещё нет — создать):
sudo docker run --name ig_crm_db \
  -e POSTGRES_USER=ig_user -e POSTGRES_PASSWORD=ig_password123 \
  -e POSTGRES_DB=ig_crm -p 5433:5432 -d postgres:15

# если контейнер уже создан раньше — просто запустить:
sudo docker start ig_crm_db

# проверить, что слушает 5433:
ss -ltn | grep 5433
```

Redis уже работает на 6379 (проверка: `redis-cli ping` → `PONG`).

## 2. Накатить миграции (из IG_CRM/backend)

```bash
cd /home/sk8ver/Documents/Projects/CRM/IG_CRM/backend
# NOTE: use `python -m` — the .venv console scripts (alembic/uvicorn/celery)
# have a stale shebang from an earlier project path and won't run directly.
../.venv/bin/python -m alembic -c ../alembic.ini upgrade head
```

## 3. Запустить бэк (терминал 1)

```bash
cd /home/sk8ver/Documents/Projects/CRM/IG_CRM/backend
../.venv/bin/python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --reload
# Проверка: http://localhost:8000/health  → {"status":"ok"}
# Доки:     http://localhost:8000/docs
```

## 4. Celery worker + beat (терминалы 2 и 3) — для реального IG

```bash
# worker
cd /home/sk8ver/Documents/Projects/CRM/IG_CRM/backend
../.venv/bin/python -m celery -A app.core.celery_app.celery_app worker --loglevel=info \
  -Q ig_crm.default --include=app.workers.celery_tasks,app.workers.media_tasks

# beat (в отдельном терминале)
cd /home/sk8ver/Documents/Projects/CRM/IG_CRM/backend
../.venv/bin/python -m celery -A app.core.celery_app.celery_app beat --loglevel=info
```

## 5. Туннель на бэк (терминал 4)

```bash
# одноразовый URL (меняется при каждом запуске):
ngrok http 8000

# ЛУЧШЕ: бесплатный статический домен (URL не меняется между запусками)
# 1) на dashboard.ngrok.com забери свой free static domain
# 2) ngrok config add-authtoken <твой токен>
# 3) ngrok http --domain=<твой-домен>.ngrok-free.app 8000
```

Скопируй https-URL вида `https://xxxx.ngrok-free.app` — это адрес API.

## 6. Фронт на Vercel

1. Залей каталог фронта `frontend/front_IG/instagram-crm-architecture/` на GitHub
   (или `vercel` CLI из этого каталога).
2. В Vercel → Project → **Settings → Environment Variables** добавь:
   ```
   NEXT_PUBLIC_API_URL = https://xxxx.ngrok-free.app   (URL ngrok из шага 5)
   ```
3. **Redeploy** (env вшивается при сборке).
4. Открой выданную Vercel-ссылку — это и есть адрес для кента.

> ⚠️ `NEXT_PUBLIC_*` вшивается на сборке. Если ngrok-URL поменялся (одноразовый),
> надо обновить переменную и **передеплоить**. Поэтому ставь **static domain** (шаг 5).

### Альтернатива без Vercel (быстрее для дебага, но без доступа кенту извне)
Поднять фронт локально и тоже затуннелить:
```bash
cd /home/sk8ver/Documents/Projects/CRM/IG_CRM/frontend/front_IG/instagram-crm-architecture
echo 'NEXT_PUBLIC_API_URL=https://xxxx.ngrok-free.app' > .env.local
# запускай напрямую (corepack pnpm dev спотыкается на проверке sharp):
./node_modules/.bin/next dev   # http://localhost:3000
# отдельным ngrok: ngrok http 3000  → ссылку дать кенту
```

---

## 7. CORS

Бэк сейчас разрешает все origins (`CORS_ALLOWED_ORIGINS=["*"]`) — для теста ок.
Перед публичным продом сузить до домена Vercel в `backend/app/core/config.py`.

## 8. Что нужно ТОЛЬКО от тебя
1. `sudo docker ...` для Postgres (шаг 1).
2. Реальные **прокси** (добавить на `/proxies`) и **cookies** аккаунтов (`/accounts`) — для живого IG.
3. ngrok authtoken (бесплатный) для статического домена.
4. OpenAI/Ollama ключ уже вписан в `backend/.env` (модель `gpt-oss:20b`).

## 9. Траблшутинг
| Симптом | Причина / фикс |
|---|---|
| Фронт: «Failed to fetch» / CORS | `NEXT_PUBLIC_API_URL` не публичный, или ngrok не запущен |
| Mixed content (https→http) | API должен быть https → используй ngrok (он https) |
| 401 на всех запросах | токен протух/не залогинен; перелогинься |
| Бэк не стартует, ошибка БД | контейнер `ig_crm_db` не запущен / порт не 5433 |
| AI-чат 502 | проверь ключ/модель в `backend/.env`, доступ к ollama.com |
| Задачи висят в RUNNING | не запущен `celery beat` (джанитор) |
| Воркер падает на Chromium | проверь, что аккаунту назначен рабочий прокси и валидные cookies |
| ngrok «too many connections» (free) | подними static domain или перезапусти туннель |

## 10. Лекция «как это листится/хостится» (на пальцах)
- «Хостинг» = твои процессы крутятся и доступны нужным людям.
- 3 уровня доступа: **localhost** (ты), **LAN** (одна Wi-Fi), **туннель** (весь интернет).
- ngrok = безопасный туннель: публичный https-URL на твой localhost, **без**
  проброса портов роутера и без засветки домашнего IP.
- Vercel = бесплатный хостинг для Next.js фронта (он статикой/SSR живёт у них),
  а тяжёлый бэк (браузеры, ffmpeg, очереди) остаётся у тебя локально.
- Реальный прод (VPS + домен + TLS + CI) — следующая стадия, не для завтра.
