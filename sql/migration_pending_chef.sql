-- =====================================================================
-- Очередь «Спросить у шефа» из Mini App (Menu Button не поддерживает sendData).
-- Выполнить в Supabase SQL Editor после migration_rls_user_isolation.sql.
-- =====================================================================

CREATE TABLE IF NOT EXISTS pending_chef (
    user_id    BIGINT PRIMARY KEY,
    recipe_id  UUID        NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pending_chef_created_at ON pending_chef (created_at DESC);

ALTER TABLE pending_chef ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Clients can manage pending chef" ON pending_chef;
DROP POLICY IF EXISTS "Telegram users select own pending chef" ON pending_chef;
DROP POLICY IF EXISTS "Telegram users insert own pending chef" ON pending_chef;
DROP POLICY IF EXISTS "Telegram users update own pending chef" ON pending_chef;
DROP POLICY IF EXISTS "Telegram users delete own pending chef" ON pending_chef;

CREATE POLICY "Telegram users select own pending chef"
    ON pending_chef FOR SELECT
    TO authenticated
    USING (user_id = remy_telegram_user_id());

CREATE POLICY "Telegram users insert own pending chef"
    ON pending_chef FOR INSERT
    TO authenticated
    WITH CHECK (user_id = remy_telegram_user_id());

CREATE POLICY "Telegram users update own pending chef"
    ON pending_chef FOR UPDATE
    TO authenticated
    USING (user_id = remy_telegram_user_id())
    WITH CHECK (user_id = remy_telegram_user_id());

CREATE POLICY "Telegram users delete own pending chef"
    ON pending_chef FOR DELETE
    TO authenticated
    USING (user_id = remy_telegram_user_id());

REVOKE ALL ON pending_chef FROM anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON pending_chef TO authenticated;
