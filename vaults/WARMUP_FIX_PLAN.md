# WARMUP 3.0 — ФИНАЛЬНЫЙ ПЛАН (v2)

> Дата: 17 мая 2026  
> Статус: готов к реализации, все находки учтены

---

## ЧТО УЖЕ ЕСТЬ И ЧЕГО НЕТ

### behavior.py (1483 строки) — УЖЕ РЕАЛИЗОВАНО

| Метод | Используется в warmup? | Комментарий |
|---|---|---|
| `navigate_left_rail(target)` | ДА | Reels, Profile, Create, Home... |
| `dismiss_interruptions()` → `dismiss_instagram_modals()` | ДА | 3-layer sweep (L1→L2→L3) |
| `safe_click(target)` | ДА | hover + click |
| `safe_click_button(target)` | ДА | scroll to center + settle + click |
| `hover_then_click(target)` | ДА | hover + dwell + click |
| `clear_input_field(target)` | ДА | JS wipe + Ctrl+A/Backspace |
| `type_into(target, text)` | ДА | посимвольный ввод |
| `smooth_scroll(min_y, max_y)` | НЕТ 🔴 | JS-based плавный скролл |
| `micro_scroll()` | НЕТ 🔴 | 1-3 мелких скролла 60-240px |
| `deep_scroll_session(duration_s)` | НЕТ 🔴 | 4 паттерна: slow_read, skim, flick, upward_correction |
| `maybe_like_visible_post(prob)` | НЕТ 🔴 | Поиск Like в section + safe_click_button |
| `browse_comments(prob, like_count)` | НЕТ 🔴 | Открытие → скролл → лайк → закрытие |
| `_ensure_clickable(ele)` | ДА | SVG→button cascade |
| `safe_coordinate_click()` | ДА | Координатный клик с retry+verify |
| `_modal_still_present(page)` | ДА | JS-проверка dialog |

### behavior.py — ЧЕГО НЕТ (добавить)

| Метод | Зачем |
|---|---|
| `has_dialog()` | Быстрая JS-проверка `!!document.querySelector('div[role="dialog"]')` |
| `quick_sweep()` | Если dialog есть → dismiss, если нет → return 0 (экономит время) |
| `find_visible_comment_svg()` | Поиск видимого (во вьюпорте) Comment SVG |
| `find_visible_like_svg(height)` | Поиск видимого Like SVG заданного размера |
| `find_comment_hearts_in_dialog()` | JS-поиск всех нелайкнутых hearts внутри dialog, возвращает координаты |
| `verify_like_worked()` | Проверка `querySelector('svg[aria-label="Unlike"]')` после клика |
| `verify_dialog_opened()` | Проверка `role="dialog"` и `ul>li` |
| `verify_reel_changed(prev_btns)` | Сравнение right-panel кнопок до/после |

### action_warmup.py (642 строки) — УЖЕ РЕАЛИЗОВАНО

| Компонент | Статус |
|---|---|
| `execute_warmup(browser, args)` | Главный цикл ✅ |
| Weight-based выбор действий (60/15/15/10) | ✅ |
| `_try_like_visible_post` | ✅ но сломано (is_already_liked) |
| `_engage_with_comments` | ✅ но сломано (hearts pool=0) |
| `_action_scroll_feed` | ✅ но скроллит первый пост |
| `_action_watch_reels` | ✅ но не переключает рилсы |
| `_action_visit_profile` | ✅ но только через пост |
| `_action_open_comments` | ✅ обёртка над _engage |
| URL guard (robots.txt recovery) | ✅ |
| Per-tick isolation | ✅ |
| Smoke test | ✅ |

### action_warmup.py — ЧЕГО НЕТ (добавить/переписать)

| Компонент | Действие |
|---|---|
| `_is_already_liked` | ПЕРЕПИСАТЬ: проверка через JS `querySelector('[aria-label="Unlike"]')` после клика |
| `_try_like_visible_post` | ПЕРЕПИСАТЬ: использовать `find_visible_like_svg`, кликать parent button, verify |
| `_engage_with_comments` | ПЕРЕПИСАТЬ: клик по parent button, verify dialog, JS-поиск hearts, double-verify лайков |
| `_action_watch_reels` | ПЕРЕПИСАТЬ: find next-reel button, verify смены рилса, per-reel like/comment с verify |
| `_action_scroll_feed` | ДОБАВИТЬ: `verify_new_articles_loaded()` после скролла |
| `find_visible_comment_svg` | ДОБАВИТЬ |
| `find_visible_like_svg` | ДОБАВИТЬ |
| `_verify_scroll_result` | ДОБАВИТЬ: проверка что `<article>` count увеличился |

---

## ПЛАН ПО ЭТАПАМ

### ЭТАП 1: Фундамент (без него ничего не заработает)

#### 1.1 behavior.py — добавить `_LEFT_RAIL_TARGETS["profile"]` запасной селектор

```python
"profile": [
    # Existing (оставляем)
    'xpath://nav//a[@role="link"][.//img[contains(@alt," profile picture")]]',
    'css:nav img[alt$=" profile picture"]',
    'xpath://nav//span[normalize-space()="Profile"]',
    # NEW: последняя ссылка в nav как fallback
    'xpath:(//nav//a)[last()]',
],
```

#### 1.2 behavior.py — добавить `has_dialog()` и `quick_sweep()`

```python
# Module-level helpers (не требуют engine)
def has_dialog(page) -> bool:
    """True если div[role="dialog"] в DOM."""
    try:
        r = page.run_js("return !!document.querySelector('div[role=\"dialog\"]')")
        return r == "true"
    except: return False

def quick_sweep(page):
    """Быстрая очистка: проверяем dialog → если есть, вызываем dismiss."""
    if not has_dialog(page):
        return 0
    return dismiss_instagram_modals(page, per_selector_timeout_s=0.3, max_dismissals=2)
```

#### 1.3 behavior.py — добавить `find_visible_comment_svg` и `find_visible_like_svg`

```python
def find_visible_comment_svg(page):
    """Найти Comment SVG который виден во вьюпорте (не первый в DOM)."""
    try:
        vh = int(page.run_js("return window.innerHeight") or "900")
        all_svgs = list(page.eles('css:svg[aria-label="Comment"]', timeout=3) or [])
        for svg in all_svgs:
            try:
                y = svg.rect.midpoint[1]
                if 100 < y < vh - 100:
                    return svg
            except: continue
        return all_svgs[-1] if all_svgs else None
    except: return None

def find_visible_like_svg(page, height="24"):
    """Найти Like SVG заданного размера, видимый во вьюпорте."""
    try:
        vh = int(page.run_js("return window.innerHeight") or "900")
        all_svgs = list(page.eles('css:svg[aria-label="Like"]', timeout=3) or [])
        for svg in all_svgs:
            try:
                if svg.attr('height') != height: continue
                y = svg.rect.midpoint[1]
                if 50 < y < vh - 50:
                    return svg
            except: continue
        return None
    except: return None
```

#### 1.4 behavior.py — добавить `find_comment_hearts_in_dialog`

```python
def find_comment_hearts_in_dialog(page):
    """JS-поиск нелайкнутых сердечек (h=12) внутри dialog. Возвращает [{x,y},...]."""
    js_code = """
    (()=>{
        const d=document.querySelector('div[role="dialog"]');
        if(!d) return '[]';
        const hearts=d.querySelectorAll('svg[aria-label="Like"]');
        return JSON.stringify(Array.from(hearts)
            .filter(h=>h.getAttribute('height')==='12')
            .map(h=>{
                const r=h.getBoundingClientRect();
                return {x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)};
            })
        );
    })()
    """
    try:
        import json
        raw = page.run_js(js_code) or "[]"
        return json.loads(raw)
    except:
        return []
```

#### 1.5 behavior.py — добавить verify-хелперы

```python
def verify_like_worked(page, timeout=2.0):
    """Проверить что после клика Like появился Unlike SVG."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = page.run_js("return !!document.querySelector('svg[aria-label=\"Unlike\"]')")
        if r == "true": return True
        time.sleep(0.3)
    return False

def verify_dialog_opened(page, timeout=5.0):
    """Проверить что dialog открылся и содержит ul>li."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = page.run_js("var d=document.querySelector('div[role=\"dialog\"]'); return !!(d && d.querySelector('ul li'))")
        if r == "true": return True
        time.sleep(0.5)
    return False
```

#### 1.6 action_warmup.py — переписать `_is_already_liked`

```python
def _is_already_liked(page, svg_or_ele) -> bool:
    """Проверка что пост/коммент уже лайкнут. 
    Использует JS querySelector для точного определения."""
    # Просто проверяем: есть ли Unlike SVG в DOM сейчас
    try:
        r = page.run_js("return !!document.querySelector('svg[aria-label=\"Unlike\"]')")
        return r == "true"
    except:
        return False  # не можем определить — считаем что не лайкнуто
```

#### 1.7 action_warmup.py — переписать `_try_like_visible_post`

```python
def _try_like_visible_post(browser, behavior, counters, *, counter_key):
    """Лайкнуть видимый пост с double-verify."""
    page = browser.page
    
    # Найти видимый Like SVG
    svg = find_visible_like_svg(page, height="24")
    if not svg:
        _sweep_modals_safely(browser, label="like-button lookup miss")
        svg = find_visible_like_svg(page, height="24")
    if not svg:
        return False
    
    # Кликнуть по родительской кнопке (не SVG!)
    btn = _walk_up_to_clickable(svg)
    if btn:
        try:
            btn.click(by_js=True)
        except:
            safe_coordinate_click(page, btn)
    else:
        safe_coordinate_click(page, 'css:svg[aria-label="Like"]')
    
    # Double-verify
    if verify_like_worked(page):
        counters[counter_key] += 1
        behavior.idle(0.7, 1.6)
        return True
    
    # Fallback: координатный клик по SVG
    safe_coordinate_click(page, 'css:svg[aria-label="Like"]')
    if verify_like_worked(page):
        counters[counter_key] += 1
        behavior.idle(0.7, 1.6)
        return True
    
    return False
```

#### 1.8 action_warmup.py — переписать `_engage_with_comments`

Полностью переписать используя:
1. Клик по parent button (через `_walk_up_to_clickable`)
2. `verify_dialog_opened` вместо проверки по таймауту
3. `find_comment_hearts_in_dialog` для получения hearts
4. Double-verify для каждого лайка: клик → проверка fill/aria-label

#### 1.9 action_warmup.py — переписать `_action_watch_reels`

1. `navigate_left_rail("reels")` ✅ (уже работает)
2. Для каждого рилса:
   a. Найти right-panel кнопки через JS (сохранить «слепок»)
   b. `maybe_like` / `maybe_comment` с verify
   c. **Переход к следующему**:
      - Попробовать `div[aria-label="Navigate to next Reel"]` клик
      - Проверить что right-panel изменился (сравнить слепки)
      - Если нет → JS-scroll на vh → проверить снова
      - Если всё ещё нет → выйти из Reels

### ЭТАП 2: Double-Verify везде

#### 2.1 action_warmup.py — добавить verify в scroll

```python
def _verify_new_articles_loaded(page, prev_count):
    """Проверить что количество <article> увеличилось после скролла."""
    try:
        new_count = int(page.run_js("return document.querySelectorAll('article').length") or "0")
        return new_count > prev_count
    except: return False
```

#### 2.2 action_warmup.py — использовать `quick_sweep` вместо полного `dismiss_instagram_modals` для in-loop вызовов

Заменить все `_sweep_modals_safely` → `quick_sweep(page)`.

### ЭТАП 3: Хьюманизация (используем УЖЕ существующие методы из behavior.py!)

#### 3.1 Использовать `deep_scroll_session` вместо простого `page.scroll.down(500)`

`deep_scroll_session` УЖЕ реализует 4 паттерна: slow_read, skim, flick, upward_correction. Нужно просто заменить `page.scroll.down(500)` в `_action_scroll_feed` на:

```python
def _action_scroll_feed(browser, behavior, rng, counters):
    # Pre-sweep
    quick_sweep(browser.page)
    
    # Используем готовый deep_scroll_session с duration пропорциональным оставшемуся времени
    scroll_duration = min(rng.uniform(3.0, 8.0), end_time - time.monotonic())
    scroll_counters = behavior.deep_scroll_session(duration_s=scroll_duration)
    # Мержим counters
    for k, v in scroll_counters.items():
        counters[f"scroll_{k}"] = counters.get(f"scroll_{k}", 0) + v
    
    # После скролла — взаимодействие с видимым постом
    if rng.random() < _LIKE_POST_PROBABILITY:
        _try_like_visible_post(...)
    if rng.random() < _OPEN_COMMENTS_PROBABILITY:
        _engage_with_comments(...)
```

#### 3.2 Scroll vibe — рандомизация внутри `deep_scroll_session`

`deep_scroll_session` уже параметризован: `upward_correction_probability`, `flick_probability`. 
Разные сессии передают разные параметры для вариативности:

```python
# Каждый запуск прогрева — разные вероятности
scroll_configs = [
    {"upward_correction_probability": 0.10, "flick_probability": 0.25},  # агрессивный скроллер
    {"upward_correction_probability": 0.25, "flick_probability": 0.05},  # вдумчивый читатель
    {"upward_correction_probability": 0.15, "flick_probability": 0.15},  # сбалансированный
]
config = rng.choice(scroll_configs)
# В течение сессии — использовать один конфиг (консистентный «стиль» пользователя)
```

#### 3.3 Использовать `maybe_like_visible_post` из behavior.py

Вместо дублирования в `action_warmup.py` — вызывать `behavior.maybe_like_visible_post()`.

**НО**: текущий `maybe_like_visible_post` ищет `section svg[aria-label="Like"]`. Нужно добавить fallback селекторы для новой структуры Instagram.

#### 3.4 Использовать `browse_comments` из behavior.py

Вместо `_engage_with_comments` — вызывать `behavior.browse_comments()`.

**НО**: нужно проверить что он работает с текущим Instagram. Если нет — починить внутри `browse_comments`.

#### 3.5 Разные паузы между типами действий

```python
# В execute_warmup:
PAUSE_PROFILES = {
    "scroll_feed":   (0.8, 2.5),
    "open_comments": (2.0, 5.0),
    "watch_reels":   (1.0, 3.0),
    "visit_profile": (3.0, 8.0),
}
# После выполнения действия:
behavior.idle(*PAUSE_PROFILES.get(action_name, (1.2, 3.4)))
```

#### 3.6 «Отвлёкся» — долгая пауза

```python
# Раз в ~30 тиков
if rng.random() < 0.03:
    zoned_out = rng.uniform(15, 45)
    logger.info("[warmup] zoned out for %.1fs", zoned_out)
    time.sleep(zoned_out)
```

#### 3.7 Pogo-sticking

```python
# В _engage_with_comments, после закрытия модала:
if rng.random() < 0.05:
    behavior.idle(1.5, 4.0)
    # Снова открыть комментарии того же поста
    _engage_with_comments(browser, behavior, rng, counters)
```

#### 3.8 Scroll-to-top

```python
# Раз в ~20 тиков
if rng.random() < 0.05:
    browser.page.run_js("window.scrollTo({top: 0, behavior: 'smooth'})")
    behavior.idle(2.0, 5.0)
```

#### 3.9 Вариативность Reels

```python
# В _action_watch_reels, для каждого рилса:
reel_action = rng.choices(
    ["watch_only", "like", "comment", "like_and_comment"],
    weights=[60, 25, 10, 5]
)[0]
if reel_action in ("like", "like_and_comment"):
    _try_like_reel(...)
if reel_action in ("comment", "like_and_comment"):
    _engage_reel_comments(...)
```

#### 3.10 Адаптивный скролл комментариев

```python
# В _engage_with_comments:
li_count = int(page.run_js(
    "var d=document.querySelector('div[role=\"dialog\"]'); return d?d.querySelectorAll('ul li').length:0"
) or "0")
scrolls = {0:0, 1:1, 2:1, 3:2, 5:3}.get(li_count, rng.randint(3, 8)) if li_count <= 5 else rng.randint(3, 8)
```

---

## КОДОВАЯ БАЗА — ЧТО МЕНЯТЬ

### `backend/workers/core/behavior.py` — ДОБАВИТЬ:

1. `has_dialog(page)` — module-level
2. `quick_sweep(page)` — module-level
3. `find_visible_comment_svg(page)` — module-level
4. `find_visible_like_svg(page, height)` — module-level
5. `find_comment_hearts_in_dialog(page)` — module-level
6. `verify_like_worked(page, timeout)` — module-level
7. `verify_dialog_opened(page, timeout)` — module-level
8. `verify_reel_changed(page, prev_snapshot)` — module-level
9. Обновить `_LEFT_RAIL_TARGETS["profile"]` — добавить fallback

### `backend/workers/actions/action_warmup.py` — ПЕРЕПИСАТЬ:

1. Удалить `_is_already_liked` (заменить на JS-проверку)
2. Переписать `_try_like_visible_post` (parent button click + double-verify)
3. Переписать `_engage_with_comments` (parent button click + JS hearts + double-verify)
4. Переписать `_action_watch_reels` (кнопка next + verify смены рилса + per-reel verify)
5. Переписать `_action_scroll_feed` (использовать `deep_scroll_session`)
6. Заменить `_sweep_modals_safely` на `quick_sweep`
7. Добавить `_verify_new_articles_loaded`
8. Добавить PAUSE_PROFILES
9. Добавить «отвлёкся», pogo-sticking, scroll-to-top
10. Добавить вариативность Reels
11. Добавить адаптивный скролл комментариев

---

## ПОРЯДОК РЕАЛИЗАЦИИ

1. behavior.py: добавить все helpers (#1-9 из списка выше) — 30 мин
2. action_warmup.py: фундамент (#1-5) — 1 час
3. action_warmup.py: double-verify (#6-7) — 30 мин
4. action_warmup.py: хьюманизация (#8-11) — 1 час
5. Тестовый прогон на 5-10 минут — 15 мин
6. Исправление багов — итеративно
