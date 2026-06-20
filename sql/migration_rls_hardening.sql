-- =====================================================================
-- Ужесточение доступа: recipe_vault только для backend (service role).
-- Mini App по-прежнему использует anon + фильтр user_id в запросах.
--
-- ВАЖНО: на Railway для бота лучше SUPABASE_SERVICE_ROLE_KEY,
-- а в Mini App — только anon key (см. docs ниже в README).
-- =====================================================================

-- Отозвать широкую политику Vault, если была создана миграцией recipe_vault.
DROP POLICY IF EXISTS "Bot can manage recipe vault" ON recipe_vault;

-- Без политик для anon/authenticated PostgREST вернёт пусто/403 для клиентов.
-- Service role bypass RLS — бот с service key продолжит работать.

COMMENT ON TABLE recipe_vault IS
    'Глобальный кэш URL→рецепт. Доступ только через service role (бот).';

-- Опционально: явно запретить anon (если политики снова добавят по ошибке).
REVOKE ALL ON recipe_vault FROM anon, authenticated;
