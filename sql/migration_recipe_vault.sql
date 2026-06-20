-- =====================================================================
-- Recipe Vault — глобальная база успешно распарсенных URL (tiered retention).
-- Выполнить в Supabase SQL Editor после deploy с поддержкой vault.
-- =====================================================================

CREATE TABLE IF NOT EXISTS recipe_vault (
    cache_key          TEXT        PRIMARY KEY,
    source_type        TEXT        NOT NULL DEFAULT '',
    source_url         TEXT        NOT NULL DEFAULT '',
    raw_text           TEXT        NOT NULL DEFAULT '',
    recipe_json        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    image_url          TEXT        NOT NULL DEFAULT '',
    parser_version     TEXT        NOT NULL DEFAULT '',
    normalize_version  TEXT        NOT NULL DEFAULT '',
    hit_count          INTEGER     NOT NULL DEFAULT 0,
    tier               TEXT        NOT NULL DEFAULT 'draft',
    is_failure         BOOLEAN     NOT NULL DEFAULT FALSE,
    failure_reason     TEXT        NOT NULL DEFAULT '',
    first_seen_at      TIMESTAMPTZ DEFAULT NOW(),
    last_hit_at        TIMESTAMPTZ DEFAULT NOW(),
    expires_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    updated_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recipe_vault_expires_at ON recipe_vault (expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_recipe_vault_tier ON recipe_vault (tier);

CREATE INDEX IF NOT EXISTS idx_recipe_vault_last_hit ON recipe_vault (last_hit_at DESC);

ALTER TABLE recipe_vault ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Bot can manage recipe vault" ON recipe_vault;
CREATE POLICY "Bot can manage recipe vault" ON recipe_vault
    FOR ALL USING (TRUE) WITH CHECK (TRUE);
