-- =====================================================================
-- Миграция: двухуровневая категоризация (dish_type, main_ingredient)
-- Выполнить в Supabase SQL Editor для уже существующей таблицы recipes.
-- =====================================================================

ALTER TABLE recipes ADD COLUMN IF NOT EXISTS dish_type TEXT DEFAULT 'main';
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS main_ingredient TEXT DEFAULT 'other';

UPDATE recipes SET dish_type = 'main' WHERE dish_type IS NULL;
UPDATE recipes SET main_ingredient = 'other' WHERE main_ingredient IS NULL;

CREATE INDEX IF NOT EXISTS idx_recipes_dish_type ON recipes (dish_type);
CREATE INDEX IF NOT EXISTS idx_recipes_main_ingredient ON recipes (main_ingredient);
CREATE INDEX IF NOT EXISTS idx_recipes_dish_ingredient ON recipes (dish_type, main_ingredient);
