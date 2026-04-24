# Деплой Remy Bot

Полная пошаговая инструкция: от пустого Supabase-проекта до живого
бота, который сохраняет рецепты и открывает «Книгу рецептов» как
Telegram Mini App.

В боевом варианте сервисов три:

1. **Supabase** — база данных + REST.
2. **Railway** — хост для бота (Python-процесс 24/7).
3. **Vercel / Netlify / GitHub Pages / Cloudflare Pages** — HTTPS-хост
   для статического `mini_app/index.html`.

Все три — бесплатны на MVP-тарифах.

---

## 0. Что подготовить заранее

- Телеграм-бот создан через [@BotFather](https://t.me/BotFather),
  на руках `TELEGRAM_BOT_TOKEN`.
- GitHub personal access token с областью **`models:read`** (GitHub
  Models → GPT-4o-mini). Классический `ghp_...` тоже подходит.
- Аккаунты на [supabase.com](https://supabase.com) и
  [railway.app](https://railway.app).
- Аккаунт на любом HTTPS-хостинге статики (в примерах — Vercel).

---

## 1. Supabase — база данных

### 1.1. Создание проекта

1. [supabase.com](https://supabase.com) → **New Project**.
2. Регион — ближайший к Railway (у Railway дефолт `us-west` / `us-east`).
3. Подождать ~2 минуты, пока поднимется Postgres.

### 1.2. Накатить схему

1. **SQL Editor → New query**.
2. Вставить содержимое `sql/create_tables.sql`.
3. **Run**. Скрипт идемпотентный — повторный запуск ничего не сломает.

После этого в **Table Editor** должна появиться таблица `recipes` с
полями из [`docs/architecture.md`](architecture.md#5-хранение-данных).

### 1.3. Забрать ключи

**Project Settings → API**:

- `Project URL` → в переменные как `SUPABASE_URL`.
- `Project API keys → anon / public` → `SUPABASE_KEY`.

> ⚠️ `service_role` ключ **не берём** — он полнодоступный и не должен
> попасть в Mini App. Всё MVP работает на `anon`-ключе.

### 1.4. Проверка

В Supabase Studio **SQL Editor**:

```sql
SELECT count(*) FROM recipes;        -- 0
INSERT INTO recipes (user_id, title) VALUES (1, 'test');
SELECT count(*) FROM recipes;        -- 1
DELETE FROM recipes WHERE title='test';
```

Если каждая команда отработала — всё готово.

---

## 2. Railway — сам бот

### 2.1. Создать сервис

1. [railway.app](https://railway.app) → **New Project → Deploy from
   GitHub repo** → выбрать `remy_recipe_bot`.
2. Railway сам увидит `railway.toml` и применит:
   - `builder = "NIXPACKS"` + `pythonVersion = "3.11"`;
   - `startCommand = "pkill -9 -f 'python.*run.py' 2>/dev/null; sleep 2; python run.py"`;
   - `numReplicas = 1` — **критично**, два экземпляра будут драться за
     Telegram getUpdates и ловить конфликты;
   - `restartPolicyType = "ON_FAILURE"`.

### 2.2. Переменные окружения

**Service → Variables**, добавить:

| Key | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | токен из @BotFather |
| `GITHUB_TOKEN` | PAT с `models:read` |
| `SUPABASE_URL` | из п. 1.3 |
| `SUPABASE_KEY` | `anon` ключ из п. 1.3 |
| `LOG_LEVEL` | `INFO` (на первых порах можно `DEBUG`) |
| `ENVIRONMENT` | `production` |
| `WEBAPP_URL` | пока оставить пустым — заполним в п. 3 |

Нажать **Deploy**. Railway подтянет зависимости из `requirements.txt`
и запустит `python run.py`. В логах должно появиться:

```
🚀 Remy Bot starting...
🤖 Инициализация RemyBot...
✅ Хендлеры зарегистрированы
▶️ Запуск polling...
```

### 2.3. Проверка бота

Открыть бота в Telegram и написать `/start`. Если пришёл ответ
«👨‍🍳 Привет! Я Remy…» — бот живой.

---

## 3. Mini App — «Книга рецептов»

Mini App — это **статический** одностраничный `mini_app/index.html`.
Ему нужен любой HTTPS-хост (Telegram не откроет HTTP).

### 3.1. Подстановка ключей

В `mini_app/index.html` есть два плейсхолдера:

```js
const DEFAULT_SUPABASE_URL = "__SUPABASE_URL__";
const DEFAULT_SUPABASE_KEY = "__SUPABASE_KEY__";
```

Их нужно **заменить на реальные значения** из п. 1.3 перед деплоем.
Можно руками, можно одной командой:

```bash
# macOS / BSD sed
sed -i '' \
  -e "s#__SUPABASE_URL__#https://xxx.supabase.co#g" \
  -e "s#__SUPABASE_KEY__#eyJhbGciOi...anon-key#g" \
  mini_app/index.html

# Linux / GNU sed
sed -i \
  -e "s#__SUPABASE_URL__#https://xxx.supabase.co#g" \
  -e "s#__SUPABASE_KEY__#eyJhbGciOi...anon-key#g" \
  mini_app/index.html
```

> Это **anon** ключ Supabase — он публичный по дизайну. Чувствительные
> ключи (service_role) в Mini App никогда не попадают.

### 3.2. Деплой статики

Выбери любой хост, ниже — примеры.

**Vercel** (проще всего):

```bash
npm i -g vercel
cd mini_app
vercel --prod      # подтвердить создание проекта
```

Vercel выдаст URL вида `https://remy-recipes.vercel.app`.

**Netlify**:

```bash
npm i -g netlify-cli
cd mini_app
netlify deploy --prod --dir .
```

**GitHub Pages** — закинуть папку `mini_app/` в ветку `gh-pages` и
включить Pages в настройках репозитория.

**Cloudflare Pages** — подключить репозиторий, указать build
directory `mini_app`, build command оставить пустым.

### 3.3. Регистрация Mini App в BotFather

1. В Telegram открыть [@BotFather](https://t.me/BotFather).
2. `/newapp` → выбрать своего бота.
3. Название: «Книга рецептов», короткое описание, иконка 640×360.
4. Web App URL: `https://<твой-домен>/` (тот самый из п. 3.2, должен
   указывать на `index.html`).

### 3.4. Прокинуть URL в бота

В Railway **Variables** заполнить:

```
WEBAPP_URL = https://<твой-домен>/
```

Railway перезапустит сервис; `RemyBot` автоматически покажет кнопку
«📖 Книга рецептов» в Reply-клавиатуре и в `/menu`.

Проверка:

- `/start` — в клавиатуре рядом с «📋 Меню» появилась «📖 Книга
  рецептов»;
- нажатие открывает Mini App прямо поверх чата;
- категории подгружаются, детальный рецепт показывается.

---

## 4. Финальная проверка (smoke-тест на проде)

| Шаг | Ожидаемый результат |
| --- | --- |
| `/start` | Приветствие + клавиатура. |
| `/help` | Инструкция. |
| `/menu` | Inline-меню с 4 кнопками. |
| Отправить ссылку на любой рецепт (например [eda.ru](https://eda.ru/)) | «🔍 Читаю страницу...» → «🤖 Анализирую рецепт...» → карточка с бейджами, КБЖУ, «Ингредиенты», «Приготовление» и кнопками `✅ Сохранить` / `❌ Не сохранять`. |
| Нажать `✅ Сохранить` | «✅ Сохранено: «<название>»». В Supabase в таблице `recipes` появилась строка. |
| «📚 Сохранённые рецепты» | Сетка категорий с количеством. |
| Выбор категории → рецепт | Полный рецепт с кнопкой `🗑 Удалить`. |
| `🗑 Удалить` → подтверждение | «✅ Рецепт удалён». Строки в Supabase больше нет. |
| Нажать «📖 Книга рецептов» | Открывается Mini App, показывает те же категории. |
| В Mini App: категория → рецепт → кнопка «Назад» | Стек экранов работает, Telegram BackButton тоже. |

Если какой-то шаг не проходит — первым делом смотрим:

- **Railway → Deployments → View Logs** — ошибки Python-процесса.
- **Supabase → Logs → API** — ошибки REST (401/404 = не тот ключ или
  не накачен DDL).
- **Mini App → DevTools в Telegram Desktop** (кликнуть правой
  клавишей на Mini App → «Inspect»).

---

## 5. Обновление

1. **Бот**: любой `git push` в ветку, на которую смотрит Railway,
   триггерит новый деплой. `pkill` в `startCommand` заранее убьёт
   предыдущий экземпляр, поэтому конфликтов за polling не будет.
2. **Mini App**:
   - Vercel / Netlify пересобирают автоматически при пуше.
   - GitHub Pages — push в `gh-pages`.
   - Если правили плейсхолдеры — не забудь повторить `sed` перед
     деплоем (или вынеси ключи в build-step своего хостинга).
3. **БД**: любые изменения схемы клади новыми миграциями в `sql/`
   (например `sql/2026_XX_YY_add_foo.sql`). Скрипты должны быть
   идемпотентными — повторный запуск безопасен.

---

## 6. Траблшутинг

| Симптом | Причина / что делать |
| --- | --- |
| `ValueError: Не задан TELEGRAM_BOT_TOKEN` | Не прокинута env-переменная, Railway → Variables. |
| Бот молчит на `/start`, в логах `Conflict: terminated by other getUpdates` | Запущено два экземпляра. Проверь `numReplicas = 1`, остановки процессов в `startCommand`, и что нет локального `python run.py`. |
| `❌ Ошибка нормализации` на нормальной ссылке | Проверь `GITHUB_TOKEN` и лимит GitHub Models. В логах будет `RecipeNormalizer: … status=401/429`. |
| `⚠️ Не удалось сохранить рецепт` | В логах `SupabaseStorage.save_recipe … status=…` — чаще всего не накачен DDL (404) или не тот `SUPABASE_KEY`. |
| Mini App показывает `USER_ID = 0` и пустые категории | Открыт **не через Telegram WebApp** (а напрямую в браузере). Для отладки подставь `?user_id=<твой>` в URL. |
| В `/menu` нет кнопки Mini App | `WEBAPP_URL` пуст **или** не начинается с `https://` (ключевая проверка в `keyboards.py → _is_valid_webapp_url`). |
| «message is not modified» в логах | Безвредно — PTB не даёт редактировать сообщение на то же содержимое, мы просто логируем и идём дальше. |

---

## 7. Чеклист перед релизом

- [ ] `sql/create_tables.sql` накачен, `recipes` существует, RLS
      включён.
- [ ] `.env.example` ↔ фактические Variables в Railway — имена совпадают.
- [ ] `requirements.txt` не содержит лишнего и собран на Railway без
      ошибок.
- [ ] `railway.toml`: `numReplicas = 1`, `pythonVersion = "3.11"`.
- [ ] Плейсхолдеры в `mini_app/index.html` заменены.
- [ ] Mini App зарегистрирован в BotFather, `WEBAPP_URL` прокинут в
      Railway.
- [ ] Пройден smoke-тест из §4.
- [ ] Логи Railway чистые: нет `ERROR`/`Traceback` в течение первых
      5 минут работы.
