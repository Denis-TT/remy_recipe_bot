# Локализация в Remy Bot

Локализация — это один из самых «рабочих» слоёв проекта: он решает
две разные задачи одним классом, и именно ему мы доверяем консистентность
данных между ботом, базой и Mini App.

---

## 1. Принцип: «ключ — латинский, отображение — через словарь»

Ключевые поля рецепта хранятся **исключительно** в канонической
латинице:

| Поле | Допустимые значения |
| --- | --- |
| `meal_type` | `breakfast`, `lunch`, `dinner`, `dessert`, `snack`, `salad`, `soup`, `baking`, `drink`, `other` |
| `difficulty` | `easy`, `medium`, `hard` |
| `cuisine` | `italian`, `russian`, `japanese`, `french`, `chinese`, `georgian`, `korean`, `indian`, `thai`, `mexican`, `mediterranean`, `american`, `european`, `asian`, `other` |

Это даёт три плюса:

1. Одинаковые значения в БД независимо от языка UI → простые фильтры
   (`?meal_type=eq.lunch` работает и из бота, и из Mini App).
2. `callback_data` в Telegram (64-байтовый лимит, ASCII-friendly).
3. Возможность легко накатить второй язык — меняется только словарь
   отображения, а БД не трогаем.

Отображение пользователю (русское имя + эмодзи) строится **в коде
приложения** через `Localization`.

---

## 2. Два слоя класса `Localization`

### 2.1. Статический слой: нормализация

Статические методы **не зависят от языка** и всегда возвращают
канонический ключ.

```python
from src.localization import Localization

Localization.normalize_meal_type("Обед")        # → "lunch"
Localization.normalize_meal_type("lunches")     # → "lunch"
Localization.normalize_meal_type("горячее")     # → "lunch"
Localization.normalize_difficulty("Легко")      # → "easy"
Localization.normalize_cuisine("ЯПОНСКАЯ")      # → "japanese"

# Целиком рецепт — НОВЫЙ dict, исходный не мутируется
Localization.normalize_recipe({
    "meal_type": "обед",
    "difficulty": "сложно",
    "cuisine": "японская",
    "title": "Рамен",
})
# → {"meal_type": "lunch", "difficulty": "hard",
#    "cuisine": "japanese", "title": "Рамен"}
```

Для каждого поля есть таблица алиасов (`_MEAL_TYPE_ALIASES`,
`_DIFFICULTY_ALIASES`, `_CUISINE_ALIASES`), которая:

- принимает оба языка (русский / английский);
- нормализует регистр (`ЗАВТРАК` → `breakfast`);
- поддерживает единственное и множественное число (`обед`, `обеды`);
- мягко обрабатывает мусор: `None`, пустые строки, неизвестные слова →
  безопасный default (`other`, `medium`, `other`).

Поиск идёт через приватный helper `_clean(value)` — он приводит
входное значение к lowercase без пробелов, а ещё нормализует
`ё → е` (типичная проблема русских источников).

### 2.2. Экземплярный слой: отображение

Эти методы уже используют язык, выбранный при создании:

```python
loc = Localization("ru")

loc.get_meal_type_display("lunch")    # "🍲 Обеды"
loc.get_difficulty_display("medium")  # "🟡 Средне"
loc.get_cuisine_name("italian")       # "Итальянская"
loc.get_meal_type_emoji("dessert")    # "🍰"
```

Для неизвестных ключей `translate()` возвращает **исходный ключ**
(safe fallback): `loc.get_cuisine_name("vietnamese")` → `"vietnamese"`.
Для неизвестного языка тоже fallback на ключи, чтобы интерфейс
не ломался.

---

## 3. Где `Localization` вызывается

| Место | Как используется |
| --- | --- |
| `RecipeNormalizer._postprocess` | В конце нормализации AI-ответа, перед возвратом в хендлер — **единая точка** приведения к латинице. |
| `SupabaseStorage.save_recipe` | Страховочный повторный `normalize_recipe` перед `INSERT` — защищает от случайной записи русского в БД (например, при ручных вызовах). |
| `src/handlers/messages.py → format_recipe` | Переводы для бейджей в карточке рецепта («🍲 Обеды», «🟡 Средне», «Русская»). |
| `src/keyboards.py → categories_keyboard` | Текст кнопок категорий («🍲 Обеды (5)»). |
| `mini_app/index.html → LOCALE` | Копия словарей (без Python-специфики) для рендера Mini App. |

**Мнемоническое правило**: если данные уходят **в БД** — нормализуй в
латиницу через `Localization.normalize_*`. Если данные идут **к
пользователю** — прогоняй ключ через `Localization.get_*_display/name`.

---

## 4. Полный справочник ключей и эмодзи

### 4.1. `meal_type`

| Ключ | Эмодзи | RU название |
| --- | --- | --- |
| `breakfast` | 🍳 | Завтраки |
| `lunch` | 🍲 | Обеды |
| `dinner` | 🍽️ | Ужины |
| `dessert` | 🍰 | Десерты |
| `snack` | 🥨 | Перекусы |
| `salad` | 🥗 | Салаты |
| `soup` | 🥣 | Супы |
| `baking` | 🧁 | Выпечка |
| `drink` | 🥤 | Напитки |
| `other` | 📦 | Другое |

### 4.2. `difficulty`

| Ключ | Эмодзи | RU название |
| --- | --- | --- |
| `easy` | 🟢 | Легко |
| `medium` | 🟡 | Средне |
| `hard` | 🔴 | Сложно |

### 4.3. `cuisine`

| Ключ | RU название |
| --- | --- |
| `italian` | Итальянская |
| `russian` | Русская |
| `japanese` | Японская |
| `french` | Французская |
| `chinese` | Китайская |
| `georgian` | Грузинская |
| `korean` | Корейская |
| `indian` | Индийская |
| `thai` | Тайская |
| `mexican` | Мексиканская |
| `mediterranean` | Средиземноморская |
| `american` | Американская |
| `european` | Европейская |
| `asian` | Азиатская |
| `other` | Другая |

Для кухни из списка, не попавшего в переводы (например, `vietnamese`),
отображение просто покажет исходный ключ — а в БД запись останется
в нижнем регистре латиницы без изменений.

---

## 5. Как расширить словарь

### 5.1. Добавить новый тип блюда

1. В `src/localization.py` правим class-level атрибуты `Localization`:
   ```python
   # было: ["breakfast", "lunch", …, "other"]
   VALID_MEAL_TYPES = [..., "brunch"]

   TRANSLATIONS["ru"]["meal_type_brunch"] = "Поздний завтрак"
   MEAL_TYPE_EMOJIS["brunch"] = "🥞"

   _MEAL_TYPE_ALIASES.update({
       "бранч": "brunch",
       "brunch": "brunch",
       "поздний завтрак": "brunch",
   })
   ```
2. В `mini_app/index.html`, объект `LOCALE.meal_type`:
   ```js
   brunch: { name: "Поздний завтрак", emoji: "🥞" },
   ```
3. В `SYSTEM_PROMPT` (`src/normalizer.py`) перечень значений для LLM —
   дописать `brunch`, чтобы GPT мог им пользоваться.
4. Запустить `python -m src.localization` — self-тесты должны пройти
   (и докинуть ассерт на новый алиас).
5. Ничего мигрировать в БД не надо: `meal_type` — `TEXT`.

### 5.2. Добавить новую кухню

Для кухни отдельного `VALID_*`-списка нет: валидными считаются любые
значения из `_CUISINE_ALIASES`, а в БД `cuisine` — просто `TEXT`.
Поэтому добавление делается двумя правками на бэкенде и одной на фронте:

```python
# src/localization.py
TRANSLATIONS["ru"]["cuisine_vietnamese"] = "Вьетнамская"
_CUISINE_ALIASES.update({
    "вьетнамская": "vietnamese",
    "vietnamese": "vietnamese",
})
```

```js
// mini_app/index.html, LOCALE.cuisine
vietnamese: "Вьетнамская",
```

Плюс — дописать `vietnamese` в список разрешённых кухонь в
`SYSTEM_PROMPT` (`src/normalizer.py`), чтобы модель возвращала именно
этот ключ, а не фоллбек `other`. Эмодзи для кухни в коде не хранится —
в бейджах мы используем общий `🍽` и название.

---

## 6. Как добавить новый язык UI (пример: английский)

### 6.1. Бэкенд

```python
# src/localization.py

class Localization:
    TRANSLATIONS: Dict[str, Dict[str, str]] = {
        "ru": { ... },
        "en": {
            "meal_type_breakfast": "Breakfasts",
            "meal_type_lunch": "Lunches",
            # ... остальные meal_type
            "difficulty_easy": "Easy",
            "difficulty_medium": "Medium",
            "difficulty_hard": "Hard",
            "cuisine_italian": "Italian",
            # ... остальные cuisine
        },
    }
```

Инициализация:

```python
# src/bot.py  →  RemyBot.__init__
self.loc = Localization(self.config.language)   # например "en"
```

Добавить `LANGUAGE` в `config.py` (опциональная env-переменная с
дефолтом `"ru"`) — и все вызовы `get_*_display/name` сразу начнут
возвращать английские строки.

### 6.2. Mini App

`mini_app/index.html` — скопировать структуру `LOCALE` и **выбрать
язык** либо по query-параметру (`?lang=en`), либо по
`Telegram.WebApp.initDataUnsafe.user.language_code`:

```js
const LOCALES = {
    ru: { meal_type: { … }, difficulty: { … }, cuisine: { … } },
    en: { meal_type: { breakfast: { name: "Breakfasts", emoji: "🍳" }, … }, … },
};
const lang = (tg?.initDataUnsafe?.user?.language_code || "ru").slice(0, 2);
const LOCALE = LOCALES[lang] || LOCALES.ru;
```

### 6.3. Тексты в хендлерах

Статические русские тексты (`WELCOME_TEXT`, `HELP_TEXT`, «🔍 Читаю
страницу…» и т. д.) сейчас вшиты в `src/handlers/*.py`. Для полной
мультиязычности имеет смысл вынести их в тот же
`Localization.TRANSLATIONS` под префиксом `ui_`:

```python
TRANSLATIONS["ru"]["ui_welcome"] = "👨‍🍳 Привет! Я Remy — ..."
TRANSLATIONS["en"]["ui_welcome"] = "👨‍🍳 Hi! I'm Remy — ..."

# в handlers/commands.py
await message.reply_text(bot.loc.translate("welcome", "ui"))
```

Это намеренно оставлено за скопом MVP — проект и так на русском
как language-of-record.

---

## 7. Тестирование

`src/localization.py` имеет встроенный блок `if __name__ == "__main__":`
с 30+ ассертами. Запуск:

```bash
python -m src.localization
# → ✅ Все тесты локализации пройдены!
```

Проверяются:

- Все ветки нормализации (`meal_type`, `difficulty`, `cuisine`).
- Default-значения для `None`, пустой строки, чисел и прочего мусора.
- Что `normalize_recipe` **не мутирует** исходный словарь.
- Неизвестные ключи и неизвестные языки → safe fallback.

Любое изменение словарей должно сопровождаться новым ассертом в этом
блоке — это наша защита от регрессии.

---

## 8. Шпаргалка

```python
from src.localization import Localization

# Один раз на бот
loc = Localization("ru")

# ==== 1. На пути «к БД» ====
db_row = Localization.normalize_recipe(raw_recipe_from_ai)

# ==== 2. На пути «к пользователю» ====
text = (
    f"{loc.get_meal_type_emoji(db_row['meal_type'])} "
    f"{loc.get_meal_type_name(db_row['meal_type'])} · "
    f"{loc.get_difficulty_display(db_row['difficulty'])} · "
    f"{loc.get_cuisine_name(db_row['cuisine'])}"
)
# → "🍲 Обеды · 🟡 Средне · Русская"
```

Этого достаточно, чтобы никогда не смешивать «сырые» и «показываемые»
значения.
