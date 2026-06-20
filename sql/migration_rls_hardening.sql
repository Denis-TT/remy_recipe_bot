-- =====================================================================
-- Ужесточение recipe_vault (только service role).
-- Если уже выполняли migration_rls_user_isolation.sql — шаги ниже дублируются,
-- повторный запуск безопасен (идемпотентно).
-- =====================================================================

-- Отозвать широкую политику Vault, если была создана миграцией recipe_vault.
DROP POLICY IF EXISTS "Bot can manage recipe vault" ON recipe_vault;

-- Без политик для anon/authenticated PostgREST вернёт пусто/403 для клиентов.
-- Service role bypass RLS — бот с service key продолжит работать.

COMMENT ON TABLE recipe_vault IS
    'Глобальный кэш URL→рецепт. Доступ только через service role (бот).';

-- Опционально: явно запретить anon (если политики снова добавят по ошибке).
REVOKE ALL ON recipe_vault FROM anon, authenticated;
