# IG CRM — Frontend Architecture & Page Specs

> Документ для дизайна: используй чтобы нарисовать мокапы на телефоне.
> После генерации вёрстки — возвращайся, я подключу API и логику.

---

## Стек (рекомендация)

| Слой | Что взять | Почему |
|---|---|---|
| Фреймворк | **React + TypeScript** | FastAPI-бекенд, Pydantic-схемы легко типизировать |
| Сборка | **Vite** | Быстро, минимально конфига |
| Роутинг | **React Router v6** | 4 страницы, просто |
| Стили | **Tailwind CSS** | Быстро, минималистично, тёмная тема из коробки |
| Стейт | **React Context + useReducer** | Для MVP хватит, Redux не нужен |
| HTTP | **fetch / ky** | Лёгкий клиент |
| Графики | **Recharts** | Для дашборда (охваты, followers) |

---

## Карта страниц (4 экрана)

```
/login          — Экран входа
/dashboard      — Главный дашборд (статистика, карточки)
/accounts       — Управление аккаунтами (таблица, CRUD, trust score)
/tasks          — Задачи (история, статусы, логи)
```

---

## Страница 1: `/login` — Вход

### Назначение
Авторизация пользователя. JWT токен сохраняется в localStorage.

### Элементы
```
┌──────────────────────────────────┐
│                                  │
│         [ЛОГОТИП IG CRM]        │
│                                  │
│     ┌──────────────────────┐     │
│     │  Email               │     │
│     └──────────────────────┘     │
│     ┌──────────────────────┐     │
│     │  Password            │     │
│     └──────────────────────┘     │
│                                  │
│     ┌──────────────────────┐     │
│     │     ВОЙТИ             │     │
│     └──────────────────────┘     │
│                                  │
│        Нет аккаунта?            │
│        [Зарегистрироваться]     │
│                                  │
└──────────────────────────────────┘
```

### Состояния
- **Обычное** — форма логина
- **Загрузка** — кнопка disabled + спиннер
- **Ошибка** — красный alert "Wrong email or password"
- **Регистрация** — модалка с email + password (min 8 chars)

### API
- `POST /auth/login` → JWT token
- `POST /auth/register` → user created

---

## Страница 2: `/dashboard` — Главный дашборд

### Назначение
Быстрый обзор всего: сколько аккаунтов, какие активны, что в работе, общие метрики.

### Макет
```
┌──────────────────────────────────────────────────┐
│  SIDEBAR          │  DASHBOARD                   │
│                   │                              │
│  📊 Dashboard     │  ┌────────┐┌────────┐┌──────┐│
│  👤 Accounts      │  │12      ││10      ││2     ││
│  📋 Tasks         │  │Accounts││Active  ││Check ││
│  ⚙️  Settings     │  └────────┘└────────┘└──────┘│
│                   │                              │
│                   │  ┌────────┐┌────────┐┌──────┐│
│                   │  │45.6K  ││82%    ││45    ││
│                   │  │Follow ││Trust  ││Tasks ││
│                   │  │Total  ││Avg    ││24h    ││
│                   │  └────────┘└────────┘└──────┘│
│                   │                              │
│                   │  ┌──────────────────────────┐│
│                   │  │ Followers over time      ││
│                   │  │   (график Recharts)      ││
│                   │  └──────────────────────────┘│
│                   │                              │
│                   │  ┌──────────────────────────┐│
│                   │  │ Recent tasks             ││
│                   │  │ ✓ warmup   completed 2m  ││
│                   │  │ ✓ upload   running   5m  ││
│                   │  │ ✗ validate failed   1h  ││
│                   │  └──────────────────────────┘│
└──────────────────────────────────────────────────┘
```

### Карточки (6 штук, 2 ряда по 3)
| Карточка | Данные | Откуда |
|---|---|---|
| Total Accounts | число | `GET /metrics/dashboard` |
| Active | число | `GET /metrics/dashboard` |
| Checkpoint | число | `GET /metrics/dashboard` |
| Total Followers | число (K/M) | `GET /metrics/dashboard` |
| Trust Score Avg | % | `GET /metrics/dashboard` |
| Tasks 24h | completed / failed | `GET /metrics/dashboard` |

### График
Followers за последние 7-30 дней (line chart).

### Последние задачи
Таблица из 5 последних задач: иконка статуса, тип, аккаунт, время.

### API
- `GET /metrics/dashboard?user_id=...&days=7`
- `GET /tasks?status=running&limit=5`

---

## Страница 3: `/accounts` — Управление аккаунтами

### Назначение
Таблица всех Instagram-аккаунтов. Создание, редактирование, фильтрация, trust score.

### Макет
```
┌──────────────────────────────────────────────────┐
│  SIDEBAR          │  ACCOUNTS                    │
│                   │                              │
│                   │  [+ Add Account]  [Filter ▾] │
│                   │                              │
│                   │  ┌──────────────────────────┐│
│                   │  │ Search + tag filter      ││
│                   │  └──────────────────────────┘│
│                   │                              │
│                   │  ┌─TABLE────────────────────┐│
│                   │  │User  │Tags  │Proxy│Trust│ │
│                   │  │──────┼──────┼─────┼─────│ │
│                   │  │@cryp │crypto│🟢   │85   │ │
│                   │  │@fash │style │🟡   │62   │ │
│                   │  │@meme │meme  │🔴   │34   │ │
│                   │  └──────────────────────────┘│
│                   │                              │
│                   │  (клик по строке → модалка)  │
└──────────────────────────────────────────────────┘
```

### Таблица (колонки)
| Колонка | Данные |
|---|---|
| Username | `ig_username` |
| Status | цветной индикатор: active / checkpoint_required / validating |
| Tags | чипсы (crypto, fashion...) |
| Platform | windows / macos / linux |
| Proxy | 🟢/🟡/🔴 + proxy host |
| Trust Score | число 0-100 + цветная полоска |
| Last Check | дата |
| Actions | ⋮ (edit, delete, validate) |

### Модалка создания/редактирования
```
┌─────────────────────────────────┐
│  Add Account / Edit Account     │
│                                 │
│  Username:   [_______________]  │
│  Password:   [_______________]  │
│  Auth:       [password ▾]       │
│  Platform:   [windows ▾]        │
│  Proxy:      [Select... ▾]      │
│  Tags:       [crypto] [fashion] │
│              [+ add tag]        │
│  Cookies:    [paste JSON]       │
│                                 │
│  Trust Score: ───●── 85/100     │
│  Last Check:  2026-05-16        │
│                                 │
│  [Save]  [Cancel]               │
└─────────────────────────────────┘
```

### Состояния
- **Пусто** — "No accounts yet. Add your first one."
- **Загрузка** — скелетон таблицы
- **Ошибка** — alert
- **Фильтр** — по тегам, статусу, trust score

### API
- `GET /accounts?user_id=...`
- `POST /accounts`
- `PATCH /accounts/{id}`
- `DELETE /accounts/{id}`
- `POST /orchestrator/accounts/{id}/validate`

---

## Страница 4: `/tasks` — Задачи

### Назначение
История всех задач. Статусы, логи, перезапуск.

### Макет
```
┌──────────────────────────────────────────────────┐
│  SIDEBAR          │  TASKS                       │
│                   │                              │
│                   │  [All][Running][Done][Failed]│
│                   │                              │
│                   │  ┌─TABLE────────────────────┐│
│                   │  │Type   │Account│Status│Age││
│                   │  │───────┼───────┼──────┼───││
│                   │  │warmup │@crypto│⏳    │2m ││
│                   │  │upload │@style │✓     │1h ││
│                   │  │warmup │@meme  │✗     │3h ││
│                   │  └──────────────────────────┘│
│                   │                              │
│                   │  (клик → детали задачи)       │
└──────────────────────────────────────────────────┘
```

### Детали задачи (модалка или раскрытие)
```
┌─────────────────────────────────┐
│  Task: warmup                   │
│  Status: completed ✓            │
│  Account: @crypto               │
│  Created: 2026-05-17 10:00      │
│  Started: 2026-05-17 10:01      │
│  Done:    2026-05-17 10:16      │
│                                 │
│  Result:                        │
│  ┌─────────────────────────────┐│
│  │ posts_liked: 2              ││
│  │ comments_liked: 5           ││
│  │ reels_watched: 4            ││
│  │ profiles_visited: 1         ││
│  └─────────────────────────────┘│
│                                 │
│  Error log: (none)              │
│                                 │
│  [Retry Task]  [Close]          │
└─────────────────────────────────┘
```

### Состояния
- **Загрузка** — скелетон
- **Пусто** — "No tasks yet."
- **Фильтр по статусу** — табы: All / Pending / Running / Completed / Failed

### API
- `GET /tasks?user_id=...&status=...`
- `GET /tasks/{id}`
- `PATCH /tasks/{id}` (для ручного перезапуска: status → "pending")

---

## Сайдбар (общий для dashboard, accounts, tasks)

```
┌──────────────┐
│              │
│  [ЛОГО]      │
│              │
│  📊 Dashboard│
│  👤 Accounts │
│  📋 Tasks    │
│              │
│  ─────────── │
│              │
│  Tier: Pro   │
│  user@mail   │
│  [Logout]    │
└──────────────┘
```

### Элементы
- Логотип сверху
- 3 пункта меню (активный подсвечен)
- Снизу: tier бейдж, email, кнопка выхода
- Сворачиваемый на мобилке (бургер)

---

## Компонентное дерево

```
App
├── AuthProvider (context: user, token, login, logout)
├── Router
│   ├── LoginPage
│   │   ├── LoginForm
│   │   └── RegisterModal
│   ├── ProtectedLayout
│   │   ├── Sidebar
│   │   │   ├── NavItem (Dashboard)
│   │   │   ├── NavItem (Accounts)
│   │   │   ├── NavItem (Tasks)
│   │   │   └── UserFooter
│   │   └── <Outlet>
│   │       ├── DashboardPage
│   │       │   ├── StatCard (×6)
│   │       │   ├── FollowersChart
│   │       │   └── RecentTasksTable
│   │       ├── AccountsPage
│   │       │   ├── AccountToolbar (search, filter, add)
│   │       │   ├── AccountTable
│   │       │   │   └── AccountRow (×N)
│   │       │   └── AccountModal (create/edit)
│   │       └── TasksPage
│   │           ├── StatusTabs
│   │           ├── TaskTable
│   │           │   └── TaskRow (×N)
│   │           └── TaskDetailModal
```

---

## API-клиент (структура файла)

```typescript
// src/api/client.ts — я напишу это
const BASE = 'http://localhost:8000';

// Автоматически добавляет JWT
function authHeader(): HeadersInit { ... }
function handleResponse(res: Response) { ... }

export const api = {
  auth: {
    login(email, password): Promise<{access_token, token_type}>,
    register(email, password): Promise<User>,
  },
  accounts: {
    list(userId): Promise<Account[]>,
    get(id): Promise<Account>,
    create(data): Promise<Account>,
    update(id, data): Promise<Account>,
    remove(id): Promise<void>,
  },
  tasks: {
    list(filters?): Promise<Task[]>,
    get(id): Promise<Task>,
    update(id, data): Promise<Task>,
  },
  metrics: {
    dashboard(userId, days?): Promise<DashboardData>,
    accountMetrics(accountId): Promise<Metric[]>,
  },
  media: {
    upload(file, userId): Promise<Asset>,
    uniqueize(assetId, copies): Promise<void>,
  },
};
```

---

## План действий

1. **Ты** — рисуешь мокапы для этих 4 страниц на телефоне
2. **Ты** — скармливаешь мокапы + этот документ другой модели для генерации HTML/CSS
3. **Я** — пишу API-клиент (`src/api/client.ts`) и типы (`src/types.ts`)
4. **Я** — подключаю вёрстку к API, добавляю состояния (loading, empty, error)
5. **Я** — настраиваю роутинг, авторизацию, защищённые маршруты
6. Вместе дорабатываем

Что скажешь? Идём по этому плану?
