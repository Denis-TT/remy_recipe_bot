"""
Консультации шефа Реми по сохранённому рецепту (GPT через GitHub Models).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, Mapping, Optional

import aiohttp

from .recipe_metrics import format_duration_minutes

logger = logging.getLogger(__name__)

API_TIMEOUT_SECONDS = 90.0

CHEF_SYSTEM_PROMPT = """\
Ты — шеф-повар Реми, дружелюбный кулинарный помощник в Telegram-боте.
Пользователь задаёт вопросы ТОЛЬКО по конкретному рецепту из контекста.

Правила:
1. Отвечай по-русски, кратко и по делу (до ~1200 символов), с практичными советами.
2. Опирайся на ингредиенты и шаги рецепта; предлагай замены и альтернативы техники.
3. Если вопрос не о готовке/рецепте — вежливо откажи одной фразой.
4. На грубость, оскорбления, политику, медицину, диагнозы — мягкий отказ.
5. Не выдумывай ингредиенты, которых нет в рецепте, если пользователь не просит замену.
6. Без markdown-заголовков; можно нумерованные пункты для нескольких вопросов.
"""

_PROFANITY_RE = re.compile(
    r"(?<!\w)("
    r"хуй|хуя|пизд|еба[лт]|ёба[лт]|бля[дт]|сука|мраз|пидор|пидар|"
    r"fuck|shit|bitch|asshole"
    r")(?!\w)",
    re.IGNORECASE,
)

_MIN_QUESTION_LEN = 4


def recipe_context_block(recipe: Mapping[str, Any]) -> str:
    """Сжатый контекст рецепта для промпта."""
    title = str(recipe.get("title") or "Без названия").strip()
    lines = [f"Название: {title}"]
    desc = str(recipe.get("description") or "").strip()
    if desc:
        lines.append(f"Описание: {desc[:600]}")

    prep = int(recipe.get("prep_time") or 0)
    cook = int(recipe.get("cook_time") or 0)
    total = int(recipe.get("total_time") or 0)
    time_bits = []
    if prep:
        time_bits.append(f"подготовка {format_duration_minutes(prep)}")
    if cook:
        time_bits.append(f"у плиты {format_duration_minutes(cook)}")
    if total:
        time_bits.append(f"всего {format_duration_minutes(total)}")
    if time_bits:
        lines.append("Время: " + ", ".join(time_bits))

    servings = int(recipe.get("servings") or 0)
    if servings:
        lines.append(f"Порций: {servings}")

    ingredients = recipe.get("ingredients") or []
    if ingredients:
        lines.append("Ингредиенты:")
        for ing in list(ingredients)[:30]:
            if isinstance(ing, Mapping):
                name = str(ing.get("name") or "").strip()
                amt = ing.get("amount")
                unit = str(ing.get("unit") or "").strip()
                notes = str(ing.get("notes") or "").strip()
                bit = name
                qty = []
                if amt:
                    qty.append(str(amt))
                if unit:
                    qty.append(unit)
                if qty:
                    bit = " ".join(qty) + " " + name
                if notes:
                    bit += f" ({notes})"
                lines.append(f"  - {bit}")
            else:
                lines.append(f"  - {ing}")

    steps = recipe.get("steps") or []
    if steps:
        lines.append("Шаги:")
        for step in list(steps)[:20]:
            if isinstance(step, Mapping):
                num = step.get("step_number") or "?"
                desc = str(step.get("description") or "").strip()
            else:
                num = "?"
                desc = str(step).strip()
            if desc:
                lines.append(f"  {num}. {desc[:400]}")

    tips = recipe.get("tips") or []
    if tips:
        lines.append("Советы: " + "; ".join(str(t)[:120] for t in tips[:5]))

    source = str(recipe.get("source_url") or "").strip()
    if source:
        lines.append(f"Источник: {source}")

    return "\n".join(lines)


def validate_chef_question(text: str) -> Optional[str]:
    """Проверить вопрос. None — ок, иначе текст мягкого отказа."""
    q = (text or "").strip()
    if len(q) < _MIN_QUESTION_LEN:
        return (
            "Напиши вопрос текстом — хотя бы пару слов о том, "
            "что хочешь уточнить по рецепту."
        )
    if _PROFANITY_RE.search(q):
        return (
            "Давай без грубости 🙏 Я помогаю только с кулинарными вопросами по рецепту."
        )
    if re.search(r"https?://", q, re.I) and len(q) < 80:
        return (
            "Ссылки тут не разбираю — опиши вопрос словами: замена ингредиента, "
            "техника, время, что делать без духовки и т. п."
        )
    return None


class ChefAdvisor:
    """Вопросы к GPT по контексту рецепта."""

    def __init__(
        self,
        github_token: str,
        *,
        model: str = "gpt-5-mini",
        reasoning_effort: str = "medium",
        api_url: str = "https://models.inference.ai.azure.com/chat/completions",
    ) -> None:
        self.github_token = github_token
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.api_url = api_url

    async def answer(self, recipe: Mapping[str, Any], question: str) -> str:
        rejection = validate_chef_question(question)
        if rejection:
            return rejection

        ctx = recipe_context_block(recipe)
        user_msg = (
            f"Рецепт:\n{ctx}\n\n"
            f"Вопрос пользователя:\n{question.strip()}\n\n"
            "Ответь на все пункты вопроса, если их несколько."
        )

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": CHEF_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        }
        if "gpt-5" in self.model or "o1" in self.model or "o3" in self.model:
            payload["max_completion_tokens"] = 1200
            payload["reasoning_effort"] = self.reasoning_effort
        else:
            payload["temperature"] = 0.4
            payload["max_tokens"] = 1200

        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)

        logger.info("👨‍🍳 Chef Remy: вопрос по «%s»", recipe.get("title"))

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.post(self.api_url, json=payload) as response:
                    body = await response.text(errors="replace")
                    if response.status >= 400:
                        logger.error("Chef API %s: %s", response.status, body[:300])
                        raise RuntimeError(f"API {response.status}")
                    data = json.loads(body)
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    answer = str(content or "").strip()
                    if not answer:
                        raise RuntimeError("Пустой ответ")
                    if len(answer) > 3500:
                        answer = answer[:3490].rstrip() + "…"
                    return answer
        except asyncio.TimeoutError:
            raise RuntimeError("Таймаут при обращении к шефу") from None
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"Сетевая ошибка: {exc}") from exc


if __name__ == "__main__":
    assert validate_chef_question("чем заменить сыр?") is None
    assert validate_chef_question("а") is not None
    ctx = recipe_context_block({"title": "Борщ", "ingredients": [{"name": "Свёкла"}]})
    assert "Борщ" in ctx
    print("✅ chef_advisor")
