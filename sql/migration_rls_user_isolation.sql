-- =====================================================================
-- RLS: изоляция данных по Telegram user_id (JWT от Edge Function telegram-auth).
--
-- Порядок деплоя:
--   1. Задеплоить supabase/functions/telegram-auth (см. docs/deploy.md)
--   2. Выполнить этот SQL
--   3. Обновить Mini App (index.html с JWT-авторизацией)
--
-- Бот на Railway использует service role — RLS не применяется.
-- Mini App: anon key + access_token (role=authenticated, claim telegram_user_id).
-- =====================================================================

-- --------------------------------------------------------------------
-- Хелпер: Telegram user id из JWT (Edge Function telegram-auth)
-- --------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.remy_telegram_user_id()
RETURNS BIGINT
LANGUAGE sql
STABLE
SET search_path = public
AS $$
  SELECT NULLIF(
    trim(
      coalesce(
        auth.jwt() ->> 'telegram_user_id',
        auth.jwt() ->> 'sub'
      )
    ),
    ''
  )::bigint;
$$;

COMMENT ON FUNCTION public.remy_telegram_user_id() IS
    'Telegram user id из JWT (Edge Function telegram-auth). NULL без валидного токена.';

-- --------------------------------------------------------------------
-- recipes — только свои строки (role authenticated + JWT)
-- --------------------------------------------------------------------
ALTER TABLE recipes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can view own recipes"    ON recipes;
DROP POLICY IF EXISTS "Users can insert own recipes"  ON recipes;
DROP POLICY IF EXISTS "Users can update own recipes"  ON recipes;
DROP POLICY IF EXISTS "Users can delete own recipes"  ON recipes;
DROP POLICY IF EXISTS "Telegram users select own recipes" ON recipes;
DROP POLICY IF EXISTS "Telegram users insert own recipes" ON recipes;
DROP POLICY IF EXISTS "Telegram users update own recipes" ON recipes;
DROP POLICY IF EXISTS "Telegram users delete own recipes" ON recipes;

CREATE POLICY "Telegram users select own recipes"
    ON recipes FOR SELECT
    TO authenticated
    USING (user_id = remy_telegram_user_id());

CREATE POLICY "Telegram users insert own recipes"
    ON recipes FOR INSERT
    TO authenticated
    WITH CHECK (user_id = remy_telegram_user_id());

CREATE POLICY "Telegram users update own recipes"
    ON recipes FOR UPDATE
    TO authenticated
    USING (user_id = remy_telegram_user_id())
    WITH CHECK (user_id = remy_telegram_user_id());

CREATE POLICY "Telegram users delete own recipes"
    ON recipes FOR DELETE
    TO authenticated
    USING (user_id = remy_telegram_user_id());

REVOKE ALL ON recipes FROM anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON recipes TO authenticated;

-- --------------------------------------------------------------------
-- pending_shares — только своя очередь «Поделиться»
-- --------------------------------------------------------------------
ALTER TABLE pending_shares ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Clients can manage pending shares" ON pending_shares;
DROP POLICY IF EXISTS "Telegram users select own pending shares" ON pending_shares;
DROP POLICY IF EXISTS "Telegram users insert own pending shares" ON pending_shares;
DROP POLICY IF EXISTS "Telegram users update own pending shares" ON pending_shares;
DROP POLICY IF EXISTS "Telegram users delete own pending shares" ON pending_shares;

CREATE POLICY "Telegram users select own pending shares"
    ON pending_shares FOR SELECT
    TO authenticated
    USING (user_id = remy_telegram_user_id());

CREATE POLICY "Telegram users insert own pending shares"
    ON pending_shares FOR INSERT
    TO authenticated
    WITH CHECK (user_id = remy_telegram_user_id());

CREATE POLICY "Telegram users update own pending shares"
    ON pending_shares FOR UPDATE
    TO authenticated
    USING (user_id = remy_telegram_user_id())
    WITH CHECK (user_id = remy_telegram_user_id());

CREATE POLICY "Telegram users delete own pending shares"
    ON pending_shares FOR DELETE
    TO authenticated
    USING (user_id = remy_telegram_user_id());

REVOKE ALL ON pending_shares FROM anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON pending_shares TO authenticated;

-- --------------------------------------------------------------------
-- recipe_vault — только backend (service role), см. migration_rls_hardening.sql
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS "Bot can manage recipe vault" ON recipe_vault;
REVOKE ALL ON recipe_vault FROM anon, authenticated;

-- --------------------------------------------------------------------
-- Storage recipe-images: публичное чтение; запись только service role (бот)
-- --------------------------------------------------------------------
DROP POLICY IF EXISTS "Clients can upload recipe images" ON storage.objects;
DROP POLICY IF EXISTS "Clients can update recipe images" ON storage.objects;
DROP POLICY IF EXISTS "Clients can delete recipe images" ON storage.objects;

-- SELECT «Public can read recipe images» оставляем из create_tables.sql
