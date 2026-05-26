# Remy — Telegram-бот для парсинга и сохранения рецептов

**Remy** (`remy_recipe_bot`) — это Telegram-бот, который превращает ссылку
на любой рецепт в аккуратную карточку с ингредиентами, шагами и КБЖУ,
складывает её в облачную базу и показывает через Mini App «Книга рецептов».

- 🔗 Парсит рецепты с любых веб-сайтов по ссылке.
- 🤖 Нормализует текст через AI (**GPT-4o-mini** через GitHub Models).
- 🔥 Рассчитывает КБЖУ на порцию и на всё блюдо.
- 🗄️ Сохраняет рецепты в **Supabase** (PostgreSQL + REST API).
- 📖 Показывает «Книгу рецептов» как нативное **Telegram Mini App**.
- 🌍 Все тексты и логи — на русском (локализация с нормализацией
  русских и английских синонимов → канонические латинские ключи).

---

## 🛠 Технологии

| Слой | Технология |
| --- | --- |
| Runtime | Python 3.11+ |
| Telegram | [`python-telegram-bot`](https://docs.python-telegram-bot.org/) 22.x |
| HTTP | `aiohttp` 3.x |
| HTML-парсинг | `beautifulsoup4` + `lxml` + `readability-lxml` |
| AI | **GitHub Models** → GPT-4o-mini (Chat Completions API) |
| База данных | Supabase (PostgreSQL, JSONB, RLS, REST) |
| Mini App | Vanilla HTML/CSS/JS + Telegram WebApp SDK |
| Деплой | Railway (Nixpacks) + любой HTTPS-хост для Mini App |

Подробные обоснования решений — в [`docs/architecture.md`](docs/architecture.md).

---

## 🚀 Быстрый старт

```bash
# 1. Клонировать репозиторий
git clone https://github.com/<you>/remy_recipe_bot.git
cd remy_recipe_bot

# 2. Создать виртуальное окружение и поставить зависимости
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Заполнить переменные окружения
cp .env.example .env
# открой .env и впиши свои токены (см. раздел «Переменные окружения»)

# 4. Создать таблицы в Supabase
#    Supabase Studio → SQL Editor → вставить содержимое sql/create_tables.sql → Run

# 5. Запустить бота локально
python run.py
```

Бот стартует с единичным экземпляром, пишет логи в stdout и в
`logs/remy.log`, поднимает healthcheck на `http://localhost:8081/health`.

---

## 🔑 Переменные окружения

| Переменная | Обязательно | Назначение |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | ✅ | Токен бота от [@BotFather](https://t.me/BotFather). |
| `GITHUB_TOKEN` | ✅ | Personal access token с правом `models:read` (GitHub Models). |
| `SUPABASE_URL` | ✅ | URL Supabase-проекта, например `https://xxx.supabase.co`. |
| `SUPABASE_KEY` | ✅ | `anon` ключ Supabase (публичный). |
| `LOG_LEVEL` |  — | `DEBUG`/`INFO`/`WARNING`/…, по умолчанию `INFO`. |
| `WEBAPP_URL` |  — | HTTPS-URL задеплоенного `mini_app/index.html`. Если пусто — кнопка Mini App в боте просто не появится. |
| `ENVIRONMENT` |  — | `development` или `production` (по умолчанию `production`). |

Шаблон лежит в [`.env.example`](./.env.example).

---

## 📁 Структура проекта

```
remy_recipe_bot/
├── config.py                    # загрузка и валидация .env → dataclass Config
├── run.py                       # точка входа: single-instance, логи, healthcheck, graceful shutdown
├── railway.toml                 # деплой-конфиг для Railway (Nixpacks)
├── requirements.txt             # Python-зависимости
├── .env.example                 # шаблон переменных окружения
│
├── src/
│   ├── bot.py                   # класс RemyBot — сборка всех модулей + polling
│   ├── keyboards.py             # все Reply/Inline-клавиатуры и WebApp-кнопки
│   ├── localization.py          # нормализация ключей + RU-переводы + эмодзи
│   ├── parser.py                # BaseParser / WebParser / ParserRegistry
│   ├── normalizer.py            # RecipeNormalizer: GPT-4o-mini + постобработка
│   ├── handlers/
│   │   ├── commands.py          # /start, /menu, /help
│   │   ├── messages.py          # текст + URL-пайплайн (parse → normalize → save)
│   │   └── callbacks.py         # inline-кнопки (save, dishtype_*, ingredient_*, view_*, delete_*, …)
│   └── storage/
│       ├── base.py              # абстрактный BaseStorage (CRUD + categories + search + health)
│       └── supabase_storage.py  # реализация поверх Supabase REST API
│
├── mini_app/
│   └── index.html               # Telegram Mini App «Книга рецептов» (HTML+CSS+JS в одном файле)
│
├── sql/
│   └── create_tables.sql        # DDL для Supabase (таблица recipes, индексы, RLS, триггеры)
│
├── docs/
│   ├── architecture.md          # архитектура и поток данных
│   ├── localization.md          # гайд по локализации
│   └── deploy.md                # инструкция по деплою
│
└── README.md                    # ← этот файл
```

---

## 💬 Что умеет бот

### Команды

| Команда | Что делает |
| --- | --- |
| `/start` | Приветствие + Reply-клавиатура «📋 Меню» (+ WebApp-кнопка, если задан `WEBAPP_URL`). |
| `/menu` | Inline-меню с кнопками «📚 Сохранённые рецепты», «ℹ️ Помощь», «✉️ Обратная связь» и «📖 Книга рецептов». |
| `/help` | Инструкция по использованию. |

### Работа с ссылкой

1. Пользователь отправляет URL → бот отвечает «🔍 Читаю страницу...».
2. `WebParser` через `aiohttp` + `readability-lxml` + `BeautifulSoup`
   извлекает «сырой» текст страницы.
3. `RecipeNormalizer` передаёт текст в GPT-4o-mini по строгой JSON-схеме,
   ретраит на один раз при невалидном JSON, прогоняет результат через
   `Localization.normalize_recipe` (канонические латинские ключи) и
   набор эвристик постобработки (единицы, диапазоны, нумерация шагов).
4. Бот отображает красиво отформатированную карточку с бейджами
   (кухня/тип/сложность/время), КБЖУ, ингредиентами и шагами, плюс
   inline-кнопки **✅ Сохранить** / **❌ Не сохранять**.
5. По «Сохранить» запись уходит в Supabase через `SupabaseStorage.save_recipe`.

### Просмотр сохранённого

- Внутри бота — `📚 Сохранённые рецепты` → категории → список → детальный вид с кнопкой «🗑 Удалить».
- Через Mini App — одно касание «📖 Книга рецептов»: тот же контент в полноценном мобильном UI.

---

## 🧪 Разработка и тесты

Большая часть модулей содержит встроенный `if __name__ == "__main__":`
блок с self-тестами. Запуск:

```bash
# Индивидуально
python -m src.localization
python -m src.parser
python -m src.normalizer            # требует GITHUB_TOKEN, 1 реальный запрос к GPT
python -m src.storage.supabase_storage  # требует SUPABASE_URL/KEY + выполненный DDL
python -m src.bot                   # smoke-тесты клавиатур и форматтера (без сети)
```

В `src/bot.py` smoke-блок не лезет в сеть — проверяет сборку
`RemyBot`, TTL-очистку `temp_recipes`, форматирование рецепта и
структуру клавиатур. Для зелёного прогона достаточно подсунуть
фиктивные переменные окружения:

```bash
TELEGRAM_BOT_TOKEN=test GITHUB_TOKEN=test \
SUPABASE_URL=https://example.supabase.co SUPABASE_KEY=test \
python -m src.bot
```

Интеграционные проверки хендлеров делаются через `unittest.mock` —
примеры есть в истории коммитов / скриптах тестирования.

---

## ☁️ Деплой

- **Бот**: [Railway](https://railway.app) — `railway.toml` уже в корне.
  `startCommand` предварительно убивает зависший `python run.py`, потом
  запускает новый экземпляр. `numReplicas = 1` — обязательное условие,
  иначе два экземпляра будут конкурировать за Telegram updates.
- **Mini App**: любой HTTPS-хостинг (Vercel / Netlify / GitHub Pages /
  Cloudflare Pages). Перед деплоем в `mini_app/index.html` заменить
  плейсхолдеры `__SUPABASE_URL__` и `__SUPABASE_KEY__` на реальные
  anon-значения.

Полная пошаговая инструкция — в [`docs/deploy.md`](docs/deploy.md).

---

## 📖 Документация

| Документ | О чём |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | Поток данных, диаграмма последовательности, ответственности модулей, решения. |
| [`docs/localization.md`](docs/localization.md) | Принципы нормализации и переводов, как добавить новый язык или кухню. |
| [`docs/deploy.md`](docs/deploy.md) | Полный чек-лист деплоя: Supabase → Railway → Mini App → проверка. |

---

## 🛡️ Лицензия

Укажи лицензию, под которой публикуешь форк (MIT — разумный дефолт).

---

## 🙏 Благодарности

- Telegram Bot API и команде [python-telegram-bot](https://docs.python-telegram-bot.org/).
- Supabase за приличный бесплатный тариф PostgreSQL + REST.
- GitHub Models за API для GPT-4o-mini.
- Крысе-повару Реми из «Ratatouille» — за имя и вдохновение.
