/**
 * Mini App «Спросить у шефа» без /start в чате.
 *
 * 1. Проверяет JWT (тот же, что выдаёт telegram-auth).
 * 2. Проверяет, что рецепт принадлежит пользователю.
 * 3. Ставит recipe_id в pending_chef (бот подхватит сессию на первом вопросе).
 * 4. Шлёт приглашение в Telegram через Bot API.
 *
 * Secrets (вручную): TELEGRAM_BOT_TOKEN.
 * Автоматически в Edge Runtime: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
 *   SUPABASE_JWT_SECRET (или JWT_SECRET).
 */

import { jwtVerify } from "npm:jose@5";

const corsHeaders: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

async function verifyAccessToken(
  token: string,
  jwtSecret: string,
): Promise<number | null> {
  try {
    const { payload } = await jwtVerify(
      token,
      new TextEncoder().encode(jwtSecret),
      { audience: "authenticated" },
    );
    const raw =
      payload.telegram_user_id ?? payload.sub ?? null;
    const userId = Number(raw);
    return userId > 0 ? Math.floor(userId) : null;
  } catch {
    return null;
  }
}

function chefPromptHtml(title: string): string {
  const safe = title
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return (
    `👨‍🍳 <b>Задай вопрос шефу Реми</b> по рецепту «${safe}».\n\n` +
    "Можно несколько вопросов в одном сообщении, например:\n" +
    "• чем заменить баклажан?\n" +
    "• что делать, если нет духовки?\n\n" +
    "<i>Отвечаю только по этому рецепту. " +
    "Один запрос раз в 3 минуты. Файлы и фото здесь не разбираю. " +
    "Выйти — кнопка «Нет, спасибо» после ответа или 📋 Меню.</i>"
  );
}

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }
  if (req.method !== "POST") {
    return jsonResponse({ error: "Method not allowed" }, 405);
  }

  const botToken = Deno.env.get("TELEGRAM_BOT_TOKEN")?.trim();
  const supabaseUrl = Deno.env.get("SUPABASE_URL")?.trim();
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")?.trim();
  const jwtSecret =
    Deno.env.get("SUPABASE_JWT_SECRET")?.trim() ||
    Deno.env.get("JWT_SECRET")?.trim();

  if (!botToken || !supabaseUrl || !serviceKey || !jwtSecret) {
    console.error("chef-notify: missing secrets");
    return jsonResponse({ error: "Server misconfigured" }, 500);
  }

  const authHeader = req.headers.get("Authorization") || "";
  const bearer = authHeader.startsWith("Bearer ")
    ? authHeader.slice(7).trim()
    : "";
  if (!bearer) {
    return jsonResponse({ error: "Missing Authorization Bearer token" }, 401);
  }

  const userId = await verifyAccessToken(bearer, jwtSecret);
  if (!userId) {
    return jsonResponse({ error: "Invalid or expired token" }, 401);
  }

  let body: { recipe_id?: string };
  try {
    body = await req.json();
  } catch {
    return jsonResponse({ error: "Invalid JSON body" }, 400);
  }

  const recipeId = String(body.recipe_id || "").trim();
  if (!recipeId) {
    return jsonResponse({ error: "recipe_id required" }, 400);
  }

  const restBase = supabaseUrl.replace(/\/+$/, "") + "/rest/v1";
  const restHeaders = {
    apikey: serviceKey,
    Authorization: "Bearer " + serviceKey,
    Accept: "application/json",
  };

  const recipeRes = await fetch(
    `${restBase}/recipes?id=eq.${encodeURIComponent(recipeId)}` +
      `&user_id=eq.${userId}&select=id,title&limit=1`,
    { headers: restHeaders },
  );
  if (!recipeRes.ok) {
    const errText = await recipeRes.text();
    console.error("chef-notify: recipe fetch", recipeRes.status, errText);
    return jsonResponse({ error: "Recipe lookup failed" }, 502);
  }
  const recipes = await recipeRes.json();
  if (!Array.isArray(recipes) || recipes.length === 0) {
    return jsonResponse({ error: "Recipe not found" }, 404);
  }

  const title = String(recipes[0].title || "рецепт").trim();

  const upsertRes = await fetch(
    `${restBase}/pending_chef?on_conflict=user_id`,
    {
      method: "POST",
      headers: {
        ...restHeaders,
        "Content-Type": "application/json",
        Prefer: "resolution=merge-duplicates,return=minimal",
      },
      body: JSON.stringify({
        user_id: userId,
        recipe_id: recipeId,
      }),
    },
  );
  if (!upsertRes.ok) {
    const errText = await upsertRes.text();
    console.error("chef-notify: pending_chef upsert", upsertRes.status, errText);
    return jsonResponse({ error: "Pending queue failed" }, 502);
  }

  const tgRes = await fetch(
    `https://api.telegram.org/bot${botToken}/sendMessage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: userId,
        text: chefPromptHtml(title),
        parse_mode: "HTML",
        disable_web_page_preview: true,
      }),
    },
  );
  if (!tgRes.ok) {
    const errText = await tgRes.text();
    console.error("chef-notify: sendMessage", tgRes.status, errText);
    return jsonResponse({ error: "Telegram send failed" }, 502);
  }

  return jsonResponse({ ok: true, user_id: userId, recipe_id: recipeId });
});
