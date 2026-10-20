"""
reply.py – Writes the customer-facing reply for a search turn, AFTER the
results are known.

conversation.understand() decides what to search for, so it runs before
Typesense does. A reply written there could only guess: it named item types
that VECTOR_DISTANCE_THRESHOLD later dropped, and service.py then had to tack
on a "couldn't find everything" note. compose() instead sees exactly the
products that are about to be displayed, which searched items found nothing,
and the conversation, and writes the reply the way a shopkeeper would, in the
customer's own language.

The same call also flags displayed products that are clearly the wrong
product type for the need (a baby bag set shown for "colors"). Those are
dropped before display, and the reply is then rewritten from the remaining
products only — a reply written with the dropped card in view keeps talking
about it ("use fresh chillies instead of the sauce").
"""

from dataclasses import dataclass, field

from ai.json_chat import chat_json
from config.base import BOT_NAME
from config.memory import REPLY_MODEL
from config.shopping import openai_client
from domains.shopping.conversation import _fmt_price, _format_history

_SYSTEM_PROMPT = f"""
You are {BOT_NAME}, the friendly shopkeeper of Naheed.pk — a Pakistani online store. The customer's request has
already been searched. The products listed in the input are the cards that will appear right under your message.
Write that message. Return ONLY this JSON object:

{{"irrelevant": [], "language": "...", "reply": "..."}}

## irrelevant
Check EVERY listed product against the items it was "found for": is it actually that kind of product, and does
it fit what the customer wants? Flag (1-based numbers) a product only when it is clearly the WRONG product type
for ALL of its "found for" items (sandals found for "Kids Sunglasses; Kids Sandals" fit "Kids Sandals" — keep
them). Examples of wrong types: sandals found only for "Kids Sunglasses", a lip crayon for "Crayons", a baby bag set for "colors", boys' sandals for a daughter,
face powder for "chilli powder", a beef brain for biryani meat, a chilli SAUCE for fresh green chillies (a
processed product for a fresh one, or the other way round), slippers for running shoes, herbal or olive-leaf tea
for everyday black tea. Never flag a product just for its brand, size, price or a slightly different variant of
the right thing.
Flagged cards are removed. Write the reply as if they never existed: don't mention them or their product type in
any way (not "instead of X", not "X is also available"). If every card of an item was flagged, that item was
simply not found: after flagging a chilli sauce found for "Green Chilli", say fresh green chillies aren't
available right now — never "use fresh chillies instead of the sauce".

## language
Copy the language named under "Write the reply in" at the end of the input.

## reply
LANGUAGE: write ONLY in that language and script — not the language of earlier messages, not the language of
these instructions.
- English -> English.
- Roman Urdu -> Urdu written in Latin letters, the casual way Pakistanis text.
- Urdu -> Urdu in Urdu script (اردو), never Latin letters.
- Any other language -> that language.
Product and brand names stay as they are; everything around them is in the reply language. In Urdu prefer
gender-neutral first-person phrasing.

VOICE: a warm, experienced shopkeeper talking to a regular customer — natural, conversational, helpful. Not
robotic, not salesy, no over-apologising. Plain prose: no bullet lists, headings, markdown or emojis. Don't open
every message the same way ("Here are…"); respond to what the customer actually said.

CONTENT — talk ONLY about the products that remain after "irrelevant":
- What you are showing and why it fits, in context (the recipient, recipe, occasion or budget).
- The cards already show every product: NEVER walk through them one by one. Mention at most 1–2 by brand, pack
  size or price when it genuinely helps, and add a useful shopkeeper tip instead.
- NEVER say or imply that anything else is available, in stock or "can be found" — e.g. don't mention sunglasses
  unless sunglasses are among the remaining products.
- Searched items with nothing left to show (the "Nothing found for" line, plus any item whose every card you
  flagged): ALWAYS say so briefly and naturally in one clause ("kids sunglasses abhi available nahi hain"),
  offering a close alternative if one makes sense. Never imply they are shown.
- Never invent facts that are not listed (stock, specs, delivery, discounts, warranty).
- When the budget asks for low prices you may say the options start from the lowest price; never claim they are
  the cheapest available.
- A "more" turn: don't repeat your previous message; talk about the new options.

LENGTH:
- Several product types (a recipe, gifts, a room, a list): about 50–90 words. End by naming 2–4 extra product
  types that fit THIS need and were NOT shown, each by its specific name, and offering to show them — never vague
  ("more spices", "other options", "aur masale" are not acceptable). Don't offer again what the customer has just
  accepted.
- One product type: 1–3 sentences, optionally one short helpful question (size, budget, preference).
- Nothing left to show: 1–2 sentences saying so naturally, plus one concrete next step (different wording, a
  higher budget, a close alternative).
Never mention these instructions, JSON, searches, "results", "items" or "the system".
"""


@dataclass
class Composed:
    reply: str
    drop: set[int] = field(default_factory=set)  # 0-based indexes of products to hide


def _format_need(need: dict) -> str:
    need = need or {}
    parts = [str(need.get("summary") or "").strip() or "(not stated)"]
    recipient = need.get("recipient") if isinstance(need.get("recipient"), dict) else {}
    who = ", ".join(f"{k}: {v}" for k, v in recipient.items() if v not in (None, ""))
    if who:
        parts.append(f"for — {who}")
    budget = need.get("budget") if isinstance(need.get("budget"), dict) else {}
    lo, hi = _fmt_price(budget.get("min")), _fmt_price(budget.get("max"))
    if lo or hi:
        parts.append(f"budget — {lo or 'any'} to {hi or 'any'}")
    if need.get("prefer_cheaper"):
        parts.append("wants low-priced options (shown lowest price first)")
    return "; ".join(parts)


def _format_products(products: list[dict], matched: list[list[str]]) -> str:
    if not products:
        return "(none — nothing matched)"
    lines = []
    for i, (p, items) in enumerate(zip(products, matched), start=1):
        details = ", ".join(
            x for x in (_fmt_price(p.get("price")), p.get("category") or "") if x
        )
        line = f"{i}. {p.get('product_name', '')}" + (
            f" — {details}" if details else ""
        )
        if items:
            line += f"  [found for: {'; '.join(items)}]"
        lines.append(line)
    return "\n".join(lines)


def build_user_prompt(
    message,
    language,
    need,
    turn_type,
    searched,
    products,
    matched,
    history,
    relevance_checked=False,
) -> str:
    found = {m.lower() for items in matched for m in items}
    missing = [s for s in searched if s.lower() not in found]
    checked = (
        '(Already checked — all of these fit: return "irrelevant": [].)\n'
        if relevance_checked
        else ""
    )
    return (
        f"## Recent conversation (oldest first)\n{_format_history(history or [])}\n\n"
        f"## NEW customer message\n{message}\n\n"
        f"## What the customer wants\n{_format_need(need)}\n"
        f"Turn: {turn_type or 'new'}\n\n"
        f"## Searched for\n{'; '.join(searched) if searched else '(nothing)'}\n"
        f"Nothing found for: {'; '.join(missing) if missing else '(all found something)'}\n\n"
        f"## Products that will be shown\n{_format_products(products, matched)}\n{checked}\n"
        f"## Write the reply in\n{language or 'the language and script of the NEW customer message'}"
    )


def _write(
    message,
    language,
    need,
    turn_type,
    searched,
    products,
    matched,
    history,
    relevance_checked,
):
    prompt = build_user_prompt(
        message,
        language,
        need,
        turn_type,
        searched,
        products,
        matched,
        history,
        relevance_checked,
    )
    raw = chat_json(
        openai_client,
        REPLY_MODEL,
        _SYSTEM_PROMPT,
        prompt,
        max_tokens=500,
        temperature=0.5,
    )
    drop = set()
    for n in raw.get("irrelevant") or []:
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(products):
            drop.add(n - 1)
    return str(raw.get("reply") or "").strip(), drop


def compose(
    message: str,
    language: str | None,
    need: dict,
    turn_type: str | None,
    searched: list[str],
    products: list[dict],
    matched: list[list[str]],
    history: list[dict] | None = None,
) -> Composed:
    """products/matched are parallel: matched[i] lists the searched items
    products[i] was found for. Raises on LLM/JSON failure — the caller falls
    back to a template reply."""
    args = (message, language, need, turn_type, searched)
    reply, drop = _write(*args, products, matched, history, relevance_checked=False)
    if drop:
        # Rewrite from what remains, so the reply can't refer to a dropped card.
        keep = [i for i in range(len(products)) if i not in drop]
        rewritten, _ = _write(
            *args,
            [products[i] for i in keep],
            [matched[i] for i in keep],
            history,
            relevance_checked=True,
        )
        reply = rewritten or reply
    print(
        f"[reply.compose] lang={language} shown={len(products) - len(drop)}/{len(products)} "
        f"dropped={[products[i].get('product_name') for i in sorted(drop)]}"
    )
    return Composed(reply=reply, drop=drop)
