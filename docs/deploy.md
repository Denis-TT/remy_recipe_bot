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

### 1.2. Накатить схему и миграции

В **SQL Editor → New query** выполнить **по порядку** (каждый файл — Run):

| # | Файл | Зачем |
|---|------|-------|
| 1 | `sql/create_tables.sql` | Таблица `recipes`, Storage, базовый RLS |
| 2 | `sql/migration_pending_shares.sql` | Шаринг из Mini App |
| 3 | `sql/migration_recipe_vault.sql` | Глобальный кэш URL |
| 4 | `sql/migration_rls_user_isolation.sql` | **RLS по Telegram user_id** |
| 5 | `sql/migration_rls_hardening.sql` | Vault только для service role |
| 6 | `sql/migration_recipe_views.sql` | Просмотренные рецепты Mini App (между устройствами) |
| 7 | `sql/migration_pending_chef.sql` | «Спросить у шефа» из Mini App без `/start` |

Опционально (если БД старая и колонок ещё нет):  
`migration_nutrition_note.sql`, `migration_nutrition_estimated.sql`, `migration_dish_categorization.sql`.

> ⚠️ **Порядок важен:** сначала задеплой Edge Function (п. 1.4), **потом**
> п. 4–6, **потом** обнови Mini App (п. 3). Иначе «Книга рецептов» перестанет
> грузить данные до выката JWT-авторизации.

После п. 1 в **Table Editor** должна появиться таблица `recipes`.

### 1.3. Забрать ключи

**Project Settings → API**:

| Ключ | Куда |
|------|------|
| `Project URL` | `SUPABASE_URL` (Railway + Mini App) |
| **`service_role`** (secret) | `SUPABASE_KEY` на **Railway** (бот) |
| **`anon` / publishable** | `__SUPABASE_ANON_KEY__` в **Mini App** |

> ⚠️ **service_role** — только на Railway, никогда в Mini App или git.  
> Mini App использует **anon** + JWT от Edge Function `telegram-auth`.

### 1.4. Edge Function `telegram-auth` (обязательно для RLS)

Mini App не ходит в БД напрямую с anon-ключом — сначала получает JWT.

1. Установи [Supabase CLI](https://supabase.com/docs/guides/cli) и залогинься.
2. В корне репозитория:

```bash
supabase link --project-ref <YOUR_PROJECT_REF>
supabase secrets set TELEGRAM_BOT_TOKEN=<токен_из_BotFather>
# опционально для dev в браузере:
supabase secrets set REMY_DEV_AUTH_SECRET=<случайная_строка>
supabase functions deploy telegram-auth --no-verify-jwt
```

3. Проверка:

```bash
curl -s -X POST "https://<project>.supabase.co/functions/v1/telegram-auth" \
  -H "apikey: <anon_key>" \
  -H "Authorization: Bearer <anon_key>" \
  -H "Content-Type: application/json" \
  -d '{"dev_user_id":12345,"dev_secret":"<REMY_DEV_AUTH_SECRET>"}'
```

Ответ: `{"access_token":"eyJ...","user_id":12345,...}`.

### 1.4.1. Edge Function `chef-notify` (кнопка «Спросить у шефа» в Mini App)

Menu Button **не поддерживает** `WebApp.sendData`. Функция ставит рецепт в
`pending_chef` и **сама шлёт приглашение** в чат с ботом — без `/start`.

> ⚠️ Секреты с префиксом `SUPABASE_` (**URL**, **service_role**, **anon**) Edge
> Functions получают **автоматически** — через `supabase secrets set` их задать
> нельзя (CLI: «Env name cannot start with SUPABASE_»). Вручную нужен только
> `TELEGRAM_BOT_TOKEN` (тот же, что для `telegram-auth`).

```bash
cd /path/to/remy_recipe_bot
supabase secrets list          # должен быть TELEGRAM_BOT_TOKEN
supabase functions deploy chef-notify --no-verify-jwt
```

Таблица `pending_chef` — миграция `sql/migration_pending_chef.sql` (п. 1.2).

### 1.5. Проверка БД

В Supabase Studio **SQL Editor**:

```sql
SELECT count(*) FROM recipes;        -- 0
INSERT INTO recipes (user_id, title) VALUES (1, 'test');
SELECT count(*) FROM recipes;        -- 1
DELETE FROM recipes WHERE title='test';
```

Если каждая команда отработала — всё готово.

### 1.6. Keep-alive на Free Plan (чтобы проект не ушёл в pause)

На бесплатном тарифе Supabase **ставит проект на паузу после ~7 дней без
API-активности**. Если ботом и Mini App никто не пользуется — БД «засыпает».

В репозитории есть workflow `.github/workflows/supabase-keepalive.yml`:
**2 раза в неделю** (пн и чт, 09:00 UTC) он вызывает Edge Function
`telegram-auth`. Ответ будет 4xx без `initData` — это ожидаемо; для Supabase
важен сам факт запроса.

**Один раз после пуша в GitHub:**

1. Репозиторий → **Settings → Secrets and variables → Actions → New repository secret**:
   | Secret | Значение |
   | --- | --- |
   | `SUPABASE_URL` | Project URL из п. 1.3 |
   | `SUPABASE_ANON_KEY` | **anon** ключ из п. 1.3 (не service_role) |
2. **Actions** → workflow **Supabase keep-alive** → **Run workflow** (проверка вручную).
3. Зелёный run с `OK (HTTP 4xx)` — всё настроено.

> Edge Function `telegram-auth` должна быть задеплоена (п. 1.4).  
> Расписание можно поменять в `cron` внутри yaml; главное — чаще одного раза в 7 дней.

---

## 2. Railway — сам бот

### 2.1. Создать сервис

1. [railway.app](https://railway.app) → **New Project → Deploy from
   GitHub repo** → выбрать `remy_recipe_bot`.
2. Railway сам увидит `railway.toml` и применит:
   - `builder = "NIXPACKS"` + `pythonVersion = "3.11"`;
   - `startCommand = "python run.py"`;
   - `healthcheckPath = "/health"`;
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
| `SUPABASE_KEY` | **service_role** ключ из п. 1.3 (не anon) |
| `YOUTUBE_API_KEY` | для YouTube Shorts (опционально, но рекомендуется) |
| `APIFY_API_TOKEN` | для субтитров Instagram / YouTube / VK (опционально) |
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
const DEFAULT_SUPABASE_KEY = "__SUPABASE_ANON_KEY__";
```

Их нужно **заменить на реальные значения** из п. 1.3 (**anon/publishable**, не service_role) перед деплоем.
Можно руками, можно одной командой:

```bash
# macOS / BSD sed
sed -i '' \
  -e "s#__SUPABASE_URL__#https://xxx.supabase.co#g" \
  -e "s#__SUPABASE_ANON_KEY__#eyJhbGciOi...anon-key#g" \
  mini_app/index.html

# Linux / GNU sed
sed -i \
  -e "s#__SUPABASE_URL__#https://xxx.supabase.co#g" \
  -e "s#__SUPABASE_ANON_KEY__#eyJhbGciOi...anon-key#g" \
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
| Отправить ссылку на рецепт (сайт или [Instagram Reels](https://www.instagram.com/reel/DZw5dtOtmxO/)) | «🔍 Читаю…» → «🤖 Анализирую…» → карточка с бейджами, КБЖУ, ингредиентами, шагами и кнопками `✅ Сохранить` / `❌ Не сохранять`. |
| Нажать `✅ Сохранить` | «✅ Сохранено: «<название>»». В Supabase в таблице `recipes` появилась строка. |
| «📚 Сохранённые рецепты» | Сетка категорий с количеством. |
| Выбор категории → рецепт | Полный рецепт с кнопкой `🗑 Удалить`. |
| `🗑 Удалить` → подтверждение | «✅ Рецепт удалён». Строки в Supabase больше нет. |
| Нажать «📖 Книга рецептов» | Открывается Mini App, категории загружаются (JWT + RLS). |
| В Mini App: тёмная тема, навигация «Назад» | Переключатель темы работает; BackButton и стек экранов согласованы. |
| В Mini App: категория → рецепт → «Назад» | Просмотр помечается в `recipe_views` (синхронизация между устройствами). |

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
| Mini App: «Авторизация не удалась» / HTTP 401 | Edge Function `telegram-auth` не задеплоена или неверный `TELEGRAM_BOT_TOKEN` в secrets. |
| Mini App: пустые категории после RLS | Миграция `migration_rls_user_isolation.sql` накатана, но Mini App старый (без JWT) или нет `access_token`. |
| Mini App: `USER_ID = 0` | Открыт не через Telegram. Dev: `?user_id=123&dev_secret=<REMY_DEV_AUTH_SECRET>`. |
| Supabase: проект на pause / «Resume project» | Free Plan: 7 дней без API. Настрой keep-alive (§1.6) или нажми Resume в Dashboard. |
| GitHub Actions keep-alive красный | Нет secrets `SUPABASE_URL` / `SUPABASE_ANON_KEY` или не задеплоена `telegram-auth`. |
| В `/menu` нет кнопки Mini App | `WEBAPP_URL` пуст **или** не начинается с `https://` (ключевая проверка в `keyboards.py → _is_valid_webapp_url`). |
| Mini App: отступ сверху слишком большой / малый | Режим fullscreen vs fullsize: `applyTelegramInsets()` в `mini_app/index.html`. Обнови Mini App после правок. |
| Отправить Instagram Reels (пример из `/start`) | Парсинг видео: нужны `APIFY_API_TOKEN` и/или Whisper; смотри логи Railway. |
| «message is not modified» в логах | Безвредно — PTB не даёт редактировать сообщение на то же содержимое. |

---

## 7. Чеклист перед релизом

- [ ] Все миграции из §1.2 накатаны (включая `migration_recipe_views.sql`).
- [ ] Edge Function `telegram-auth` задеплоена, secrets заданы.
- [ ] GitHub Actions: secrets `SUPABASE_URL` + `SUPABASE_ANON_KEY`, keep-alive workflow зелёный (§1.6).
- [ ] Railway: `SUPABASE_KEY` = **service_role**; Mini App: **anon** key.
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
