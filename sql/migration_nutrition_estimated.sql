-- =====================================================================
-- nutrition_estimated: КБЖУ приблизительное (отображается с префиксом ~).
-- =====================================================================

ALTER TABLE recipes ADD COLUMN IF NOT EXISTS nutrition_estimated BOOLEAN DEFAULT FALSE;

UPDATE recipes SET nutrition_estimated = FALSE WHERE nutrition_estimated IS NULL;
