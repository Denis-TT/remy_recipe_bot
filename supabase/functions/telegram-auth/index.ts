/**
 * Telegram Mini App → Supabase JWT.
 *
 * Проверяет initData (HMAC по документации Telegram WebApp) и выдаёт
 * короткоживущий JWT с claim `telegram_user_id` для RLS в Postgres.
 *
 * Secrets (Supabase Dashboard → Edge Functions → Secrets):
 *   TELEGRAM_BOT_TOKEN  — токен бота
 *   REMY_DEV_AUTH_SECRET — опционально, для dev (?user_id=&dev_secret=)
 *
 * Автоматически доступен: SUPABASE_JWT_SECRET (или JWT_SECRET).
 */

import { SignJWT } from "npm:jose@5";

const corsHeaders: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const TOKEN_TTL_SEC = 3600;
const MAX_AUTH_AGE_SEC = 86400;

type AuthRequest =
  | { init_data: string }
  | { dev_user_id: number; dev_secret: string };

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

function bytesToHex(bytes: Uint8Array): string {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmacSha256(
  key: Uint8Array,
  data: string,
): Promise<Uint8Array> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    key,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign(
    "HMAC",
    cryptoKey,
    new TextEncoder().encode(data),
  );
  return new Uint8Array(sig);
}

/** https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app */
async function validateInitData(
  initData: string,
  botToken: string,
): Promise<number | null> {
  const params = new URLSearchParams(initData);
  const hash = params.get("hash");
  if (!hash) return null;

  params.delete("hash");
  const dataCheckString = [...params.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");

  const secretKey = await hmacSha256(
    new TextEncoder().encode("WebAppData"),
    botToken,
  );
  const computed = bytesToHex(
    await hmacSha256(secretKey, dataCheckString),
  );

  if (computed !== hash.toLowerCase()) return null;

  const authDate = Number(params.get("auth_date") || "0");
  if (authDate > 0) {
    const age = Math.floor(Date.now() / 1000) - authDate;
    if (age > MAX_AUTH_AGE_SEC) return null;
  }

  const userRaw = params.get("user");
  if (!userRaw) return null;
  try {
    const user = JSON.parse(userRaw) as { id?: number };
    if (user?.id && user.id > 0) return user.id;
  } catch {
    return null;
  }
  return null;
}

async function mintAccessToken(
  userId: number,
  jwtSecret: string,
): Promise<string> {
  const sub = String(userId);
  return await new SignJWT({
    role: "authenticated",
    telegram_user_id: sub,
  })
    .setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setSubject(sub)
    .setAudience("authenticated")
    .setIssuedAt()
    .setExpirationTime(`${TOKEN_TTL_SEC}s`)
    .sign(new TextEncoder().encode(jwtSecret));
}

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }
  if (req.method !== "POST") {
    return jsonResponse({ error: "Method not allowed" }, 405);
  }

  const botToken = Deno.env.get("TELEGRAM_BOT_TOKEN")?.trim();
  const jwtSecret =
    Deno.env.get("SUPABASE_JWT_SECRET")?.trim() ||
    Deno.env.get("JWT_SECRET")?.trim();
  const devSecret = Deno.env.get("REMY_DEV_AUTH_SECRET")?.trim();

  if (!botToken || !jwtSecret) {
    console.error("Missing TELEGRAM_BOT_TOKEN or JWT secret");
    return jsonResponse({ error: "Server misconfigured" }, 500);
  }

  let payload: AuthRequest;
  try {
    payload = await req.json();
  } catch {
    return jsonResponse({ error: "Invalid JSON body" }, 400);
  }

  let userId: number | null = null;

  if ("init_data" in payload && typeof payload.init_data === "string") {
    userId = await validateInitData(payload.init_data.trim(), botToken);
    if (!userId) {
      return jsonResponse({ error: "Invalid init_data" }, 401);
    }
  } else if (
    "dev_user_id" in payload &&
    typeof payload.dev_user_id === "number" &&
    typeof payload.dev_secret === "string"
  ) {
    if (!devSecret || payload.dev_secret !== devSecret) {
      return jsonResponse({ error: "Dev auth denied" }, 403);
    }
    if (payload.dev_user_id > 0) {
      userId = Math.floor(payload.dev_user_id);
    }
  } else {
    return jsonResponse(
      { error: "Provide init_data or dev_user_id+dev_secret" },
      400,
    );
  }

  if (!userId) {
    return jsonResponse({ error: "user_id not resolved" }, 401);
  }

  try {
    const access_token = await mintAccessToken(userId, jwtSecret);
    return jsonResponse({
      access_token,
      token_type: "bearer",
      expires_in: TOKEN_TTL_SEC,
      user_id: userId,
    });
  } catch (err) {
    console.error("JWT mint failed:", err);
    return jsonResponse({ error: "Token issue failed" }, 500);
  }
});
