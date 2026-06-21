-- =====================================================================
-- Просмотренные рецепты Mini App (синхронизация между устройствами).
-- Выполнить в Supabase SQL Editor после migration_rls_user_isolation.sql.
-- =====================================================================

CREATE TABLE IF NOT EXISTS recipe_views (
    user_id    BIGINT      NOT NULL,
    recipe_id  UUID        NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    viewed_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, recipe_id)
);

CREATE INDEX IF NOT EXISTS idx_recipe_views_user ON recipe_views (user_id);
CREATE INDEX IF NOT EXISTS idx_recipe_views_viewed_at ON recipe_views (viewed_at DESC);

ALTER TABLE recipe_views ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Telegram users select own recipe views" ON recipe_views;
DROP POLICY IF EXISTS "Telegram users insert own recipe views" ON recipe_views;
DROP POLICY IF EXISTS "Telegram users update own recipe views" ON recipe_views;
DROP POLICY IF EXISTS "Telegram users delete own recipe views" ON recipe_views;

CREATE POLICY "Telegram users select own recipe views"
    ON recipe_views FOR SELECT TO authenticated
    USING (user_id = remy_telegram_user_id());

CREATE POLICY "Telegram users insert own recipe views"
    ON recipe_views FOR INSERT TO authenticated
    WITH CHECK (user_id = remy_telegram_user_id());

CREATE POLICY "Telegram users update own recipe views"
    ON recipe_views FOR UPDATE TO authenticated
    USING (user_id = remy_telegram_user_id())
    WITH CHECK (user_id = remy_telegram_user_id());

CREATE POLICY "Telegram users delete own recipe views"
    ON recipe_views FOR DELETE TO authenticated
    USING (user_id = remy_telegram_user_id());

REVOKE ALL ON recipe_views FROM anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON recipe_views TO authenticated;
