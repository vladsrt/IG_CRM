# Instagram DOM Structure Reference — May 2026 (VERIFIED)

> Все данные подтверждены через JS querySelector на реальном аккаунте ds_user_id=66998291245.
> Скриншоты в `vaults/dom_research/`.

---

## 1. FEED (Главная страница)

### Структура `<article>`
```
<article>
  <div class="x78zum5 xdt5ytf x5yr21d xa1mljc xh8yej3 x1bs97v6 x1q0q8m5 x11aubdm xnc8uc2 x1qhh985">
    <div class="xsag5q8 x2vl965 x1onr9mi">
      <div class="x6s0dn4 x78zum5 x1q0g3np x1nhvcw1 xh8yej3">
        └── header (username, Follow, More options)
        └── media (img/video)
        └── action bar:
            ├── svg[aria-label="Like"] height="24" fill="currentColor"
            ├── svg[aria-label="Comment"] height="24"
            ├── svg[aria-label="Repost"] height="24"
            ├── svg[aria-label="Share Post"] height="24"
            └── svg[aria-label="Save"] height="24"
        └── caption + comment preview
```

### Ключевые селекторы
| Селектор | Находит | Примечание |
|---|---|---|
| `article` | Все посты | 3-5 на экране |
| `svg[aria-label="Like"]` | Like кнопки (h=24) | fill="currentColor" |
| `svg[aria-label="Unlike"]` | Unlike состояние | **появляется только после клика Like** |
| `svg[aria-label="Comment"]` | Comment кнопки (h=24) | |

### ⚠️ ВАЖНО про Like состояние
- **ДО лайка**: SVG имеет `aria-label="Like"`, `fill="currentColor"`
- **ПОСЛЕ лайка**: SVG меняет `aria-label="Unlike"` (НЕ fill!)
- fill="currentColor" НЕ МЕНЯЕТСЯ — цвет через CSS на родителе
- Определять состояние НЕ по fill, а по наличию `svg[aria-label="Unlike"]` в DOM

---

## 2. COMMENTS

### Как открыть (правильно!)
**НЕЛЬЗЯ кликать по SVG** — он может иметь `pointer-events: none`.
Надо кликать по **родительской кнопке**:
```
xpath://*[@role="button"][.//svg[@aria-label="Comment"]]
```
Клик через `ele.click(by_js=True)` чтобы обойти hydration overlay.

### Структура открытого модала
```
div[role="dialog"] >
  div (header: username, Close)
  div (scrollable: ul > li > комментарий)
    li:
      ├── svg[aria-label="Like"] height="12"  ← маленькое сердечко
      ├── svg[aria-label="Unlike"] height="12" ← после лайка
      ├── текст комментария
      └── Reply button
  div (footer: поле ввода)
```

### Как искать hearts (JS)
```javascript
const d = document.querySelector('div[role="dialog"]');
const hearts = d.querySelectorAll('svg[aria-label="Like"]');
// Фильтруем: height="12" = comment heart (не post heart height="24")
```

---

## 3. REELS

### Навигация
- `navigate_left_rail("reels")` работает ✅
- URL: `https://www.instagram.com/reels/{REEL_ID}/`

### Right-panel кнопки (подтверждено JS)
| Кнопка | Позиция x | y |
|---|---|---|
| Like | 689 | 535 |
| Like counter | 687 | 559 |
| Comment | 681 | 591 |
| Repost | 689 | 663 |
| Share | 689 | 727 |
| Save | 689 | 779 |
| More | 681 | 823 |

### Переключение рилсов
- `page.scroll.down(vh)` **НЕ РАБОТАЕТ** (подтверждено: right-panel кнопки не изменились)
- Есть кнопка `div[aria-label="Navigate to next Reel"]` — нужно проверить работает ли
- Fallback: JS-scroll контейнера через `container.scrollBy({top: vh})`

---

## 4. LEFT RAIL (левый рейл)

### Селекторы которые РАБОТАЮТ
| Цель | Селектор |
|---|---|
| Reels | `xpath://a[contains(@href,"/reels/")]` |
| Create | `xpath://a[contains(@href,"/create/")]` |
| Home | `css:svg[aria-label="Home"]` |

### Селекторы которые НЕ РАБОТАЮТ
| Цель | Селектор | Проблема |
|---|---|---|
| Profile | `xpath://nav//span[normalize-space()="Profile"]` | В реальности отображается username, не "Profile" |

### ⚠️ `querySelector('nav')` возвращает null
Причина: nav загружается асинхронно или находится внутри другого элемента.
Решение: искать через DrissionPage `page.ele('tag:nav', timeout=5)` — он умеет ждать появления.

---

## 5. PROFILE PAGE

Страница: `https://www.instagram.com/{username}/`

### Структура
```
header > section >
  img (аватар) — alt="{username}'s profile picture"
  h2 или h1 (username)
  span (bio)
  ul (followers/following counts)
  
main >
  div[role="tablist"] (Posts / Reels / Tagged)
  div (сетка постов) > a[href*="/p/"] или a[href*="/reel/"]
```

### Переход из левого рейла
Селектор который работает: ссылка с `img[alt$=" profile picture"]` внутри nav.

---

## 6. MODAL DISMISSAL

### Типы модалов
1. "Turn on Notifications" — L1: Not Now ✅
2. "Save Login Info" — L1: Not Now ✅
3. Comment dialog — `div[role="dialog"]` — L2 backdrop / L3 ESC ✅
4. Post detail — `div[role="dialog"]` — L2 backdrop / L3 ESC ✅

### Оптимизация (quick_sweep)
```python
def quick_sweep(page):
    if not has_dialog(page):  # JS: !!document.querySelector('div[role="dialog"]')
        return 0  # нет модала — мгновенный выход
    return dismiss_instagram_modals(page, per_selector_timeout_s=0.3, max_dismissals=2)
```
Экономит 5-7 секунд на каждом вызове когда модалов нет.

---

## 7. SCROLLING

### Feed scroll
- `page.scroll.down(N)` ✅
- `deep_scroll_session(duration)` — уже есть в behavior.py, 4 паттерна
- `smooth_scroll(min_y, max_y)` — JS-based, уже есть
- `micro_scroll()` — 1-3 мелких скролла, уже есть

### Comments scroll внутри dialog
- `d.querySelector('ul').parentElement.scrollBy(0, N)` через JS

### Reels scroll
- `div[aria-label="Navigate to next Reel"]` — кнопка (проверить)
- `container.scrollBy({top: window.innerHeight})` — JS fallback

---

## 8. СВОДКА: что использовать из существующего кода

| Метод в behavior.py | Использовать для |
|---|---|
| `deep_scroll_session(duration)` | скролла ленты (вместо `page.scroll.down(500)`) |
| `smooth_scroll(min, max)` | JS-плавного скролла |
| `micro_scroll()` | мелких «человеческих» скроллов |
| `maybe_like_visible_post(prob)` | лайка поста (но нужен fallback селектор) |
| `browse_comments(prob, like_count)` | открытия/чтения/лайка комментариев |
| `_ensure_clickable(ele)` | SVG→clickable parent |
| `safe_coordinate_click(page, sel, verify=...)` | координатного клика с retry+verify |
| `navigate_left_rail("reels")` | перехода в Reels |
| `dismiss_instagram_modals(page)` | очистки модалов |
