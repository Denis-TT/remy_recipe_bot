# Remy — Telegram-бот для парсинга и сохранения рецептов

**Remy** (`remy_recipe_bot`) — Telegram-бот, который превращает ссылку на рецепт
или кулинарное видео в аккуратную карточку с ингредиентами, шагами и КБЖУ,
складывает её в облачную базу и показывает через Mini App «Книга рецептов».

- 🔗 Парсит рецепты с веб-сайтов, **YouTube Shorts**, **Instagram Reels**, **TikTok** и **VK**.
- 🤖 Нормализует текст через AI (**GPT-5-mini** / GPT-4o-mini через GitHub Models).
- 🔥 Рассчитывает КБЖУ на порцию и на всё блюдо; оценивает порции по USDA RACC.
- ⏱ Разделяет активное время «у плиты» и общее календарное (расстойка, маринад).
- 🗄️ Сохраняет рецепты в **Supabase** (PostgreSQL + RLS + Edge Functions).
- 📖 Показывает «Книгу рецептов» как **Telegram Mini App** (тёмная тема, JWT-авторизация).
- 🔒 Изоляция данных по Telegram user_id через RLS и Edge Function `telegram-auth`.
- 🌍 Все тексты и логи — на русском (локализация с нормализацией синонимов → канонические ключи).

---

## 🛠 Технологии

| Слой | Технология |
| --- | --- |
| Runtime | Python 3.11+ |
| Telegram | [`python-telegram-bot`](https://docs.python-telegram-bot.org/) 22.x |
| HTTP | `aiohttp` 3.x |
| HTML-парсинг | `beautifulsoup4` + `lxml` + `readability-lxml` |
| Видео | `yt-dlp`, Whisper, Apify Actors (YouTube / Instagram / VK) |
| AI | **GitHub Models** → GPT-5-mini (Chat Completions API) |
| Изображения | Hugging Face FLUX.1-dev (опционально), Supabase Storage |
| База данных | Supabase (PostgreSQL, JSONB, RLS, REST, Edge Functions) |
| Mini App | Vanilla HTML/CSS/JS + Telegram WebApp SDK |
| Деплой | Railway (Nixpacks) + Vercel / любой HTTPS-хост для Mini App |

Подробные обоснования решений — в [`docs/architecture.md`](docs/architecture.md).

---

## 🚀 Быстрый старт

```bash
# 1. Клонировать репозиторий
git clone https://github.com/Denis-TT/remy_recipe_bot.git
cd remy_recipe_bot

# 2. Создать виртуальное окружение и поставить зависимости
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Заполнить переменные окружения
cp .env.example .env
# открой .env и впиши свои токены (см. раздел «Переменные окружения»)

# 4. Создать таблицы в Supabase
#    Supabase Studio → SQL Editor → выполнить миграции по порядку (см. docs/deploy.md)

# 5. Запустить бота локально
python run.py
```

Бот стартует с единичным экземпляром, пишет логи в stdout и в
`logs/remy.log`, поднимает healthcheck на `http://localhost:$PORT/health`
(по умолчанию `8081`).

---

## 🔑 Переменные окружения

### Обязательные

| Переменная | Назначение |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Токен бота от [@BotFather](https://t.me/BotFather). |
| `GITHUB_TOKEN` | Personal access token с правом `models:read` (GitHub Models). |
| `SUPABASE_URL` | URL Supabase-проекта, например `https://xxx.supabase.co`. |
| `SUPABASE_KEY` | **service_role** ключ на Railway (бот); **anon** — только в Mini App. |

### Основные опциональные

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `GITHUB_MODEL` | `gpt-5-mini` | Модель нормализации рецептов. |
| `GITHUB_REASONING_EFFORT` | `medium` | Усилие рассуждения для reasoning-моделей. |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / … |
| `WEBAPP_URL` | — | HTTPS-URL Mini App. Без него кнопка «Книга рецептов» не появится. |
| `ENVIRONMENT` | `production` | `development` или `production`. |
| `EXAMPLE_TEST_URL` | Instagram Reels | Ссылка для кнопки «Протестировать пример» в `/start`. |

### Видео и медиа

| Переменная | Назначение |
| --- | --- |
| `YOUTUBE_API_KEY` | YouTube Data API v3 (метаданные). |
| `APIFY_API_TOKEN` | Apify Actors для субтитров YouTube / Instagram / VK. |
| `INSTAGRAM_SESSION_ID` | Опционально — sessionid для Instagram Actor. |
| `HF_API_KEY` | Hugging Face — генерация изображений блюд (FLUX.1-dev). |
| `IMAGES_DIR` | `/images` | Каталог изображений (том на Railway). |

### Лимиты и Vault

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `URL_RATE_LIMIT_SECONDS` | `180` | Минимальный интервал между ссылками на пользователя. |
| `VAULT_ENABLED` | `true` | Глобальный кэш URL → рецепт (Recipe Vault). |
| `APIFY_MAX_RUNS_PER_DAY` | `80` | Лимит запусков Apify Actor в сутки. |

Полный список — в [`.env.example`](./.env.example).

---

## 📁 Структура проекта

```
remy_recipe_bot/
├── config.py                    # загрузка и валидация .env → dataclass Config
├── run.py                       # точка входа: single-instance, логи, healthcheck
├── railway.toml                 # деплой Railway (Nixpacks, numReplicas=1)
├── nixpacks.toml                # ffmpeg и системные пакеты
├── requirements.txt
├── .env.example
│
├── src/
│   ├── bot.py                   # RemyBot — сборка модулей + polling
│   ├── keyboards.py             # Reply/Inline-клавиатуры и WebApp-кнопки
│   ├── localization.py          # нормализация ключей + RU-переводы
│   ├── parser.py                # WebParser, YouTube, Instagram, TikTok, VK
│   ├── normalizer.py            # RecipeNormalizer: GPT + постобработка
│   ├── recipe_metrics.py        # время, порции (USDA RACC), форматирование КБЖУ
│   ├── recipe_vault/            # глобальный кэш URL → рецепт
│   ├── handlers/
│   │   ├── commands.py          # /start, /menu, /help, deep link share_*
│   │   ├── messages.py          # URL/фото/текст-пайплайн
│   │   └── callbacks.py         # inline-кнопки (save, share, delete, …)
│   └── storage/
│       ├── base.py
│       └── supabase_storage.py  # Supabase REST (service role)
│
├── mini_app/
│   └── index.html               # Telegram Mini App (JWT, тёмная тема, safe area)
│
├── supabase/
│   └── functions/
│       └── telegram-auth/       # Edge Function: initData → JWT для RLS
│
├── sql/
│   ├── create_tables.sql
│   └── migration_*.sql          # RLS, vault, shares, recipe_views, …
│
└── docs/
    ├── architecture.md
    ├── localization.md
    └── deploy.md
```

---

## 💬 Что умеет бот

### Команды

| Команда | Что делает |
| --- | --- |
| `/start` | Приветствие + кнопка «Протестировать пример» (Instagram Reels). |
| `/menu` | Inline-меню: сохранённые рецепты, помощь, обратная связь, Mini App. |
| `/help` | Инструкция по использованию. |

### Источники рецептов

| Тип | Как обрабатывается |
| --- | --- |
| Веб-сайт | `aiohttp` + `readability-lxml` + BeautifulSoup |
| YouTube Shorts | yt-dlp + Whisper / Apify субтитры |
| Instagram Reels | yt-dlp + Whisper / Apify transcripts |
| TikTok | Аналогичный видео-пайплайн |
| VK Клипы | yt-dlp + Apify VK scraper |
| Фото блюда | GPT по изображению (GitHub Models) |

### Пайплайн сохранения

1. Пользователь отправляет URL, фото или текст → статус «Читаю…» / «Анализирую…».
2. Парсер извлекает сырой контент; при включённом Vault — проверяет глобальный кэш URL.
3. `RecipeNormalizer` вызывает GPT по строгой JSON-схеме, постобрабатывает время, порции, КБЖУ.
4. Бот показывает карточку с бейджами и кнопками **✅ Сохранить** / **❌ Не сохранять**.
5. По «Сохранить» — запись в Supabase через `SupabaseStorage` (service role).

### Просмотр и шаринг

- В боте — «📚 Сохранённые рецепты» → категории → детальный вид → удаление / шаринг.
- В Mini App — «Книга рецептов»: категории, подкатегории по ингредиентам, детальный рецепт.
- «Поделиться» из Mini App → deep link `/start share_<id>` или очередь `pending_shares`.

---

## 📖 Mini App

Одностраничное приложение (`mini_app/index.html`) с:

- JWT-авторизацией через Edge Function `telegram-auth` (RLS в Supabase).
- Тёмной темой (переключатель, `localStorage`).
- Синхронизацией просмотренных рецептов между устройствами (`recipe_views`).
- Адаптивными отступами под fullscreen / fullsize режимы Telegram WebApp.

Перед деплоем заменить плейсхолдеры `__SUPABASE_URL__` и `__SUPABASE_ANON_KEY__`
на **anon**-ключ из Supabase (не service_role).

---

## 🧪 Разработка и тесты

Многие модули содержат встроенный `if __name__ == "__main__":` блок с self-тестами:

```bash
python -m src.localization
python -m src.parser
python -m src.recipe_metrics
python -m src.normalizer            # требует GITHUB_TOKEN
python -m src.storage.supabase_storage  # требует SUPABASE + DDL
python -m src.bot                   # smoke-тесты без сети
```

Для зелёного прогона `src.bot` достаточно фиктивных переменных:

```bash
TELEGRAM_BOT_TOKEN=test GITHUB_TOKEN=test \
SUPABASE_URL=https://example.supabase.co SUPABASE_KEY=test \
python -m src.bot
```

---

## ☁️ Деплой

| Компонент | Где | Что нужно |
| --- | --- | --- |
| База + RLS + Edge Function | Supabase | SQL-миграции + `telegram-auth` |
| Бот 24/7 | Railway | `railway.toml`, `SUPABASE_KEY` = service_role |
| Mini App | Vercel / Netlify / Pages | anon-ключ, HTTPS |

Полная пошаговая инструкция — в [`docs/deploy.md`](docs/deploy.md).

---

## 📖 Документация

| Документ | О чём |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | Поток данных, RLS/JWT, модули, Recipe Vault. |
| [`docs/localization.md`](docs/localization.md) | Нормализация и переводы, добавление языка. |
| [`docs/deploy.md`](docs/deploy.md) | Чек-лист деплоя: Supabase → Railway → Mini App. |

---

## 🛡️ Лицензия

MIT (или укажи свою при публикации форка).

---

## 🙏 Благодарности

- Telegram Bot API и [python-telegram-bot](https://docs.python-telegram-bot.org/).
- Supabase за PostgreSQL + REST + Edge Functions.
- GitHub Models за API для GPT.
- Крысе-повару Реми из «Ratatouille» — за имя и вдохновение.
