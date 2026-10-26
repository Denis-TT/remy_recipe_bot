-- Общая кулинарная книга: гости видят и правят рецепты владельца.
-- Бот (service role) пишет строки в household_members. Mini App читает через RLS.

CREATE TABLE IF NOT EXISTS public.household_members (
    member_id         BIGINT PRIMARY KEY,
    cookbook_user_id  BIGINT NOT NULL,
    username          TEXT   NOT NULL DEFAULT ''
);

ALTER TABLE public.household_members ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.household_members FROM anon, authenticated;

CREATE OR REPLACE FUNCTION public.remy_cookbook_owner_id()
RETURNS BIGINT
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT cookbook_user_id
  FROM public.household_members
  WHERE member_id = public.remy_telegram_user_id();
$$;

REVOKE ALL ON FUNCTION public.remy_cookbook_owner_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.remy_cookbook_owner_id() TO authenticated;

DROP POLICY IF EXISTS "Telegram users select own recipes" ON recipes;
DROP POLICY IF EXISTS "Telegram users insert own recipes" ON recipes;
DROP POLICY IF EXISTS "Telegram users update own recipes" ON recipes;
DROP POLICY IF EXISTS "Telegram users delete own recipes" ON recipes;

CREATE POLICY "Telegram users select own recipes"
    ON recipes FOR SELECT TO authenticated
    USING (
        user_id = public.remy_telegram_user_id()
        OR user_id = public.remy_cookbook_owner_id()
    );

CREATE POLICY "Telegram users insert own recipes"
    ON recipes FOR INSERT TO authenticated
    WITH CHECK (
        user_id = public.remy_telegram_user_id()
        OR user_id = public.remy_cookbook_owner_id()
    );

CREATE POLICY "Telegram users update own recipes"
    ON recipes FOR UPDATE TO authenticated
    USING (
        user_id = public.remy_telegram_user_id()
        OR user_id = public.remy_cookbook_owner_id()
    )
    WITH CHECK (
        user_id = public.remy_telegram_user_id()
        OR user_id = public.remy_cookbook_owner_id()
    );

CREATE POLICY "Telegram users delete own recipes"
    ON recipes FOR DELETE TO authenticated
    USING (
        user_id = public.remy_telegram_user_id()
        OR user_id = public.remy_cookbook_owner_id()
    );

INSERT INTO public.household_members (member_id, cookbook_user_id, username)
VALUES (218510504, 218510504, '')
ON CONFLICT (member_id) DO UPDATE
SET cookbook_user_id = EXCLUDED.cookbook_user_id;
