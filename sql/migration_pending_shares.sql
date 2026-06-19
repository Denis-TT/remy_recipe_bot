-- =====================================================================
-- Очередь «поделиться рецептом» из Mini App (Menu Button не поддерживает sendData).
-- Выполнить в Supabase SQL Editor.
-- =====================================================================

CREATE TABLE IF NOT EXISTS pending_shares (
    user_id    BIGINT PRIMARY KEY,
    recipe_id  UUID        NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pending_shares_created_at ON pending_shares (created_at DESC);

ALTER TABLE pending_shares ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Clients can manage pending shares" ON pending_shares;
CREATE POLICY "Clients can manage pending shares" ON pending_shares
    FOR ALL USING (TRUE) WITH CHECK (TRUE);
