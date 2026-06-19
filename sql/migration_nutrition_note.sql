-- =====================================================================
-- Миграция: nutrition_note — пояснение, когда КБЖУ нельзя посчитать.
-- Выполнить в Supabase SQL Editor для уже существующей таблицы recipes.
-- =====================================================================

ALTER TABLE recipes ADD COLUMN IF NOT EXISTS nutrition_note TEXT DEFAULT '';

UPDATE recipes SET nutrition_note = '' WHERE nutrition_note IS NULL;
