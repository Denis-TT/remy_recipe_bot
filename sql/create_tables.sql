-- =====================================================================
-- Remy Bot — схема таблиц Supabase (Блок 5: storage).
--
-- Применить один раз при первичной настройке проекта:
--   1. Открыть Supabase Studio → SQL Editor.
--   2. Вставить содержимое этого файла и нажать «Run».
--
-- Скрипт идемпотентен: повторный запуск не приведёт к ошибкам
-- (используется IF NOT EXISTS / IF NOT EXISTS для индексов и
--  CREATE OR REPLACE для политик — см. комментарии ниже).
-- =====================================================================

-- --------------------------------------------------------------------
-- Таблица рецептов
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recipes (
    id                     UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id                BIGINT      NOT NULL,

    -- Текстовые поля
    title                  TEXT        NOT NULL,
    description            TEXT        DEFAULT '',

    -- Категоризация (хранится в КАНОНИЧЕСКОЙ ЛАТИНИЦЕ — см. src/localization.py)
    cuisine                TEXT        DEFAULT 'other',
    meal_type              TEXT        DEFAULT 'other',
    dish_type              TEXT        DEFAULT 'main',
    main_ingredient        TEXT        DEFAULT 'other',
    difficulty             TEXT        DEFAULT 'medium',

    -- Время приготовления (минуты)
    prep_time              INTEGER     DEFAULT 0,
    cook_time              INTEGER     DEFAULT 0,
    total_time             INTEGER     DEFAULT 0,

    -- Порции
    servings               INTEGER     DEFAULT 4,

    -- Структурированные данные (JSON — без строгой схемы)
    ingredients            JSONB       DEFAULT '[]'::jsonb,
    steps                  JSONB       DEFAULT '[]'::jsonb,
    nutrition              JSONB       DEFAULT '{}'::jsonb,  -- на 100 г
    nutrition_per_serving  JSONB       DEFAULT '{}'::jsonb,  -- на порцию
    nutrition_note         TEXT        DEFAULT '',             -- если КБЖУ не рассчитано
    total_nutrition        JSONB       DEFAULT '{}'::jsonb,  -- на всё блюдо

    -- Массивы коротких строк
    equipment              TEXT[]      DEFAULT '{}',
    tips                   TEXT[]      DEFAULT '{}',
    tags                   TEXT[]      DEFAULT '{}',

    storage                TEXT        DEFAULT '',

    -- Диетические флаги
    is_vegetarian          BOOLEAN     DEFAULT FALSE,
    is_vegan               BOOLEAN     DEFAULT FALSE,
    is_gluten_free         BOOLEAN     DEFAULT FALSE,
    is_lactose_free        BOOLEAN     DEFAULT FALSE,

    -- Метаданные источника
    source_url             TEXT        DEFAULT '',
    image_url              TEXT        DEFAULT '',
    created_at             TIMESTAMPTZ DEFAULT NOW(),
    updated_at             TIMESTAMPTZ DEFAULT NOW()
);


-- Существующие проекты без колонки — безопасное добавление.
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS image_url TEXT DEFAULT '';
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS nutrition_note TEXT DEFAULT '';


-- --------------------------------------------------------------------
-- Индексы
-- --------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_recipes_user_id     ON recipes (user_id);
CREATE INDEX IF NOT EXISTS idx_recipes_meal_type   ON recipes (meal_type);
CREATE INDEX IF NOT EXISTS idx_recipes_user_meal   ON recipes (user_id, meal_type);
CREATE INDEX IF NOT EXISTS idx_recipes_dish_type   ON recipes (dish_type);
CREATE INDEX IF NOT EXISTS idx_recipes_main_ingredient ON recipes (main_ingredient);
CREATE INDEX IF NOT EXISTS idx_recipes_dish_ingredient ON recipes (dish_type, main_ingredient);
CREATE INDEX IF NOT EXISTS idx_recipes_created_at  ON recipes (created_at DESC);


-- --------------------------------------------------------------------
-- Триггер: автообновление updated_at при UPDATE
-- --------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_recipes_set_updated_at ON recipes;
CREATE TRIGGER trg_recipes_set_updated_at
    BEFORE UPDATE ON recipes
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();


-- --------------------------------------------------------------------
-- Row Level Security
-- --------------------------------------------------------------------
-- На этом этапе бот аутентифицируется Supabase anon-ключом, а проверка
-- принадлежности рецепта пользователю (`user_id`) делается в коде
-- (см. `delete_recipe`, фильтр `user_id=eq.X` во всех GET-запросах).
-- Политики ниже просто включают RLS и разрешают операции из
-- клиентского слоя; при переходе на auth.uid() их нужно будет сузить.
-- --------------------------------------------------------------------
ALTER TABLE recipes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can view own recipes"    ON recipes;
DROP POLICY IF EXISTS "Users can insert own recipes"  ON recipes;
DROP POLICY IF EXISTS "Users can update own recipes"  ON recipes;
DROP POLICY IF EXISTS "Users can delete own recipes"  ON recipes;

CREATE POLICY "Users can view own recipes"   ON recipes FOR SELECT USING (TRUE);
CREATE POLICY "Users can insert own recipes" ON recipes FOR INSERT WITH CHECK (TRUE);
CREATE POLICY "Users can update own recipes" ON recipes FOR UPDATE USING (TRUE);
CREATE POLICY "Users can delete own recipes" ON recipes FOR DELETE USING (TRUE);


-- --------------------------------------------------------------------
-- Supabase Storage: публичный бакет для изображений рецептов
-- --------------------------------------------------------------------
-- Бот загружает файлы через Storage REST API:
--   POST /storage/v1/object/recipe-images/<filename>
-- Mini App читает их по публичному URL:
--   /storage/v1/object/public/recipe-images/<filename>
-- --------------------------------------------------------------------
INSERT INTO storage.buckets (id, name, public)
VALUES ('recipe-images', 'recipe-images', TRUE)
ON CONFLICT (id) DO UPDATE
SET public = TRUE;

DROP POLICY IF EXISTS "Public can read recipe images" ON storage.objects;
DROP POLICY IF EXISTS "Clients can upload recipe images" ON storage.objects;
DROP POLICY IF EXISTS "Clients can update recipe images" ON storage.objects;
DROP POLICY IF EXISTS "Clients can delete recipe images" ON storage.objects;

CREATE POLICY "Public can read recipe images"
ON storage.objects FOR SELECT
USING (bucket_id = 'recipe-images');

CREATE POLICY "Clients can upload recipe images"
ON storage.objects FOR INSERT
WITH CHECK (bucket_id = 'recipe-images');

CREATE POLICY "Clients can update recipe images"
ON storage.objects FOR UPDATE
USING (bucket_id = 'recipe-images')
WITH CHECK (bucket_id = 'recipe-images');

CREATE POLICY "Clients can delete recipe images"
ON storage.objects FOR DELETE
USING (bucket_id = 'recipe-images');
