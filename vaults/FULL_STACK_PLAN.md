# IG CRM — Full-Stack Integration & Launch Plan

> Создано: 2026-05-22. Цель: связать существующий фронт (Next.js) с беком
> (FastAPI + Celery), достроить недостающее (админка, подписки, агенты,
> capacity), и поднять всё локально с доступом через интернет (туннель)
> для ручного теста.
>
> Решения по итогам обсуждения:
> - Завтрашний тест: **полный сервис с реальным Instagram** (воркер + Chromium + прокси + куки).
> - Ключ OpenAI: **есть** → реальный AI-парсер.
> - Доступ для второго тестировщика: **через интернет → туннель (ngrok)**.
> - Объём: **делаем всё**, но фазами; критический путь к рабочему демо — в приоритете.

---

## 0. Текущее состояние (факты на 2026-05-22)

### Что РАБОТАЕТ
- Бэк: FastAPI + Celery + Postgres + Redis, JWT, оркестратор/fan-out, trust-score,
  spintax, безопасность путей, human-behavior движок, действия warmup/upload/update_profile.
- Бинарники на машине: `chromium`, `google-chrome`, `ffmpeg`, `ffprobe`, `ngrok`, `uv`, `corepack`, `redis`(:6379).
- Фронт (Next.js 16 / React 19 / Tailwind v4 / shadcn): `/login`, `/register`, auth-guard,
  таблица `/accounts` (create/delete) — связаны с беком.

### Что НЕ работает / замокано
- `/dashboard` — все метрики хардкод (mock).
- `/orchestrator` — AI-чат и fan-out замоканы (реальные `api.generateTask`/`api.fanOut` закомментированы).
- `user.id = 'current-user'` хардкод → ломает create account / dashboard / ai при реальном подключении.
- Сайдбар ссылается на несуществующие `/proxies`, `/media`, `/settings`. Нет `/activity`, `/tasks`, `/admin`.
- Кнопки-пустышки: header «Upgrade», «Validate Session» в строке аккаунта.

### Рассинхроны контрактов фронт↔бэк (чинить обязательно)
1. Нет `GET /auth/me` — фронт не получает реальный UUID после логина.
2. `/metrics/dashboard`: бэк отдаёт `{total_accounts,total_followers,total_reel_views,total_tasks_running}`
   и берёт юзера из JWT (без `user_id` в query). Фронт ждёт другой набор полей. → выровнять.
3. `/ai/generate-task`: бэк ждёт поле `user_prompt`; фронт шлёт `prompt`. → переименовать на фронте.
4. Приоритет: бэк `low/normal/high/urgent`, фронт `low/medium/high`. → привести фронт к беку.
5. Часть роутеров без JWT-гарда (account/task/media/proxy/orchestrator) и без скоупа по юзеру.

### Блокеры запуска
- 🔴 БД не поднята: конфиг → `localhost:5433`, там пусто; на `:5432` чужой Postgres (другой пароль).
- 🔴 Docker требует sudo (юзер запускает команду сам).
- 🟡 `backend/.env` пуст: `SECRET_KEY` лежит в корневом `.env`, бэк его не видит → токены ломаются при рестарте.
- 🟡 `OPENAI_API_KEY` не задан.
- 🟡 Фронт без `node_modules`.
- ⚠️ `pyproject` requires-python `>=3.13`, в системе python 3.10; есть `.venv` (проверить версию).

---

## ФАЗА 0 — Фундамент: поднять стек локально

Цель: бэк отвечает на `/health`, фронт открывается, БД с миграциями, Redis жив.

- [ ] 0.1 Создать `backend/.env`: `SECRET_KEY`, `DATABASE_URL` (5433), `REDIS_URL`, `CELERY_*`,
      `OPENAI_API_KEY`, `OPENAI_MODEL`, `MEDIA_ROOT` (абсолютный путь), `CORS_ALLOWED_ORIGINS`.
- [ ] 0.2 Поднять Postgres контейнером на 5433 (**делает юзер, sudo**):
      ```bash
      sudo docker run --name ig_crm_db \
        -e POSTGRES_USER=ig_user -e POSTGRES_PASSWORD=ig_password123 \
        -e POSTGRES_DB=ig_crm -p 5433:5432 -d postgres:15
      ```
- [ ] 0.3 `alembic upgrade head` (из корня `IG_CRM/`).
- [ ] 0.4 Проверить `.venv` (импорт `app.api.main`, версия python). При несовпадении 3.13 — `uv sync`.
- [ ] 0.5 Установить фронт: `corepack enable && pnpm install` в каталоге фронта.
- [ ] 0.6 Smoke: `uvicorn app.api.main:app` → `GET /health`, `/docs`; `pnpm dev` → открыть `:3000`.

**От юзера на этой фазе:** запустить docker-команду (0.2), вписать `OPENAI_API_KEY` в `backend/.env`.

---

## ФАЗА 1 — Связать существующие страницы с реальным беком

- [ ] 1.1 **/auth/me**: новый эндпоинт (возвращает `id/email/tier/role`); `auth-context` после
      `login` дёргает `/auth/me` и кладёт реальный `user.id`. Убрать `'current-user'`.
- [ ] 1.2 **Dashboard**: выровнять контракт метрик (бэк ↔ фронт), отрисовать карточки из реальных
      данных, график followers через `GET /accounts/{id}/metrics` (recharts уже в зависимостях).
- [ ] 1.3 **Orchestrator**: раскомментить `api.generateTask` (поле `user_prompt`) и `api.fanOut`;
      привести типы плана/приоритета; сделать **редактор таск-плана** (добавить/удалить/изменить
      команду и args), выбор таргета «все аккаунты / по тегам / выбранные».
- [ ] 1.4 **Accounts**: реальный `user_id` из контекста; скоуп списка по юзеру; кнопка
      «Validate Session» → `POST /orchestrator/accounts/{id}/validate`; селект прокси в форме.
- [ ] 1.5 Убрать мёртвые ссылки сайдбара (или вести на заглушки), оживить header «Upgrade» → `/settings`.

---

## ФАЗА 2 — Авторизация и мультитенантность (безопасность)

- [ ] 2.1 Включить `Depends(get_current_user)` на account/task/media/proxy/orchestrator;
      `user_id` брать из токена, а не из query/form. Проверка владения (IDOR) на детальных роутах.
- [ ] 2.2 Добавить роль пользователю: колонка `role` (`user`/`admin`), миграция; зависимость
      `require_admin`. Первый зарегистрированный или из списка `ADMIN_EMAILS` → admin (для тестов).
- [ ] 2.3 (Закрыть открытые items из ARCHITECTURE §7.6, по возможности) — XSS в имени файла,
      шифрование `ig_password`/cookies — пометить как пост-MVP, не блокер теста.

---

## ФАЗА 3 — Недостающие страницы фронта

- [ ] 3.1 **/proxies** — CRUD прокси (нужно для реального IG). `GET/POST/PATCH/DELETE /proxies`.
- [ ] 3.2 **/media** — загрузка файла (`POST /media/upload`), список ассетов, «уникализировать N копий».
- [ ] 3.3 **/tasks** — история задач, фильтр по статусу, деталь задачи, retry.
- [ ] 3.4 **/activity** — «живой» экран: на каком шаге агент, что происходит (поллинг `/tasks?status=running`
      + деталь). Это фишка, которую юзер хочет показать.
- [ ] 3.5 **/settings** — профиль, текущая подписка/тариф, (опц.) ключи.
- [ ] Таскбар «как в Симс»: плавающая панель управления задачей в оркестраторе/активности
      (drag, быстрые действия, редактирование). Сделать на shadcn + dnd.

---

## ФАЗА 4 — Подписки, агенты, capacity, админка

Модель «агентов»: агент = слот параллельного исполнения задач. Тариф даёт лимит слотов;
админ может вручную выдать N слотов себе/коллегам для теста.

- [ ] 4.1 Расширить `Subscription`: `agents_limit` (int). Тарифы → дефолтные лимиты
      (free=1, pro=5, enterprise=10) + ручной override админом.
- [ ] 4.2 **Concurrency control**: при fan-out/диспетче не запускать больше `agents_limit`
      одновременных RUNNING-задач на юзера (очередь/отказ с понятным сообщением).
      Реализация: счётчик активных задач юзера + Celery очередь/семафор.
- [ ] 4.3 **Capacity-монитор**: `psutil` (CPU%, RAM%, число активных Chromium). Глобальный потолок
      одновременных браузеров, чтобы не положить тестовый комп. Эндпоинт `GET /admin/capacity`.
      Перед запуском новой задачи — проверка свободных ресурсов.
- [ ] 4.4 **/admin** (только admin): список юзеров, их тарифы/лимиты/активность, кнопки
      «выдать tier», «выдать N агентов», глобальная стата (всего аккаунтов/задач/успех-фейл),
      загрузка сервера (из 4.3).
- [ ] 4.5 Биллинг-UI (мок): экран тарифов «+1/+5/+10 агентов», кнопка Upgrade (без реальной оплаты).

---

## ФАЗА 5 — Хостинг для теста (туннель)

- [ ] 5.1 Запустить 4 процесса: Postgres+Redis, FastAPI(:8000), Celery worker(+beat), Next.js(:3000).
- [ ] 5.2 Туннель `ngrok` на фронт (и на бэк, если фронт ходит в API из браузера кента).
      Прописать `NEXT_PUBLIC_API_URL` = публичный URL бэка. Пересобрать/перезапустить фронт.
- [ ] 5.3 Финальный сквозной прогон: регистрация → аккаунт+прокси → AI-план → fan-out → активность.

### Ранбук запуска (копипаст)

```bash
# 0) БД (один раз)
sudo docker start ig_crm_db   # или docker run ... (см. 0.2)

# 1) Бэк (терминал 1) — из IG_CRM/backend
cd /home/sk8ver/Documents/Projects/CRM/IG_CRM/backend
../.venv/bin/python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --reload

# 2) Celery worker (терминал 2) — из IG_CRM/backend  [нужен для реального IG]
cd /home/sk8ver/Documents/Projects/CRM/IG_CRM/backend
../.venv/bin/python -m celery -A app.core.celery_app.celery_app worker --loglevel=info \
  -Q ig_crm.default --include=app.workers.celery_tasks,app.workers.media_tasks

# 3) Celery beat (терминал 3) — джанитор stale-задач
cd /home/sk8ver/Documents/Projects/CRM/IG_CRM/backend
../.venv/bin/python -m celery -A app.core.celery_app.celery_app beat --loglevel=info

# 4) Фронт (терминал 4) — из каталога фронта
cd /home/sk8ver/Documents/Projects/CRM/IG_CRM/frontend/front_IG/instagram-crm-architecture
pnpm dev   # http://localhost:3000

# 5) Туннели (терминал 5+6)
ngrok http 8000   # публичный URL бэка → вписать в NEXT_PUBLIC_API_URL фронта
ngrok http 3000   # публичный URL фронта → дать кенту
```

### Лекция: как это «листится/хостится» (коротко)
- «Поднять сервис» = запустить процессы (выше) + сделать их доступными нужным людям.
- 3 способа доступа: **localhost** (только ты), **LAN** (та же Wi-Fi, по локальному IP),
  **туннель** (кент из любой точки интернета).
- Туннель (ngrok) даёт публичный `https://...` на твой localhost — **без** проброса портов
  роутера и без засветки домашнего IP. Идеально для теста на день.
- Главный подвох: фронт-JS выполняется **в браузере кента** и дёргает API напрямую, поэтому
  `NEXT_PUBLIC_API_URL` должен быть **публичным** адресом бэка (туннель), а не `localhost`.
- Реальный деплой (VPS, домен, TLS-сертификат, CI) — следующая стадия, не для завтра.

---

## Что нужно ОТ ЮЗЕРА (только он может)
1. Запустить docker-команду для БД (sudo).
2. Вписать `OPENAI_API_KEY` в `backend/.env`.
3. Для реального IG: рабочие **прокси** (host/port/login/pass) и валидные **cookies** аккаунтов
   (или логин/пароль) — добавить через UI `/proxies` и `/accounts`.
4. (Опц.) аккаунт ngrok + authtoken (бесплатного хватит).

## Идеи на улучшение (предложения)
- WebSocket/SSE для `/activity` вместо поллинга — реально «живой» прогресс по шагам.
- Шифрование секретов аккаунтов (Fernet) до прод-деплоя.
- Rate-limit на `/auth/*`, audit-log админских действий.
- Health-страница со статусом Postgres/Redis/Celery/Chromium для быстрой диагностики на тесте.
- Сидер демо-данных (1 admin + пара аккаунтов + фейковые метрики), чтобы дашборд не был пустым.
```
