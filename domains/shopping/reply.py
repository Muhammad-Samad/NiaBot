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

It also returns a short, recognisable name for each card ("SJ Kids Suit,
Blue White Short, A-41" -> "SJ Kids Suit – Blue/White"), shown instead of the
full catalog name; shorten_name() is the no-LLM fallback.
"""

import html
import re
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

{{"irrelevant": [], "language": "...", "short_names": {{"1": "..."}}, "reply": "..."}}

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

## short_names
For EVERY listed product (keyed by its number), a short name for its card, max ~30 characters, that a customer
still recognises: brand + product type + the one detail that tells it apart (pack size, color, shade, model).
Drop SKU/style codes, repeated words and marketing filler. Keep the catalog's English wording.
"SJ Kids Suit, Blue White Short, A-41" -> "SJ Kids Suit – Blue/White"; "Nestle Milkpak UHT Full Cream Milk 1 Litre"
-> "Nestle Milkpak Milk 1L"; "L'Oreal Paris Color Riche Satin Lipstick 302 Bois De Rose" -> "L'Oreal Color Riche
Lipstick 302".

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

CONTENT — short and to the point; talk ONLY about the products that remain after "irrelevant":
- What you are showing, in a few words, tied to the need (recipient, recipe, occasion or budget).
- The cards already show every product's name and price: NEVER walk through or describe them one by one. Mention
  at most 1–2 products, by their short name or a general name ("the Nivea one", "the 1L pack"), never the full
  catalog name, and only when it genuinely helps. No long descriptions; a tip only if it is short and truly useful.
- NEVER say or imply that anything else is available, in stock or "can be found" — e.g. don't mention sunglasses
  unless sunglasses are among the remaining products.
- Searched items with nothing left to show (the "Nothing found for" line, plus any item whose every card you
  flagged): ALWAYS say so briefly and naturally in one clause ("kids sunglasses abhi available nahi hain"),
  offering a close alternative if one makes sense. Never imply they are shown.
- Never invent facts that are not listed (stock, specs, delivery, discounts, warranty).
- When the budget asks for low prices you may say the options start from the lowest price; never claim they are
  the cheapest available.
- A "more" turn: don't repeat your previous message; talk about the new options.
- "Brand not found" in the input: say in a few words that this brand isn't available right now and the cards show
  similar options from other brands — never present them as that brand.

LENGTH — keep it brief:
- One product type: ONE short sentence (max ~20 words), optionally followed by one very short question (size,
  budget, preference). E.g. "Here are L'Oreal lipsticks in matte and satin shades — any colour in mind?"
- Several product types (a recipe, gifts, a room, a list): 2 short sentences (max ~40 words). End by naming 2–3
  extra product types that fit THIS need and were NOT shown, by their specific names, and offering to show them —
  never vague ("more spices", "other options", "aur masale" are not acceptable). Don't offer again what the
  customer has just accepted.
- Nothing left to show: 1–2 short sentences saying so, plus one concrete next step (different wording, a higher
  budget, a close alternative).
Never mention these instructions, JSON, searches, "results", "items" or "the system".
"""


@dataclass
class Composed:
    reply: str
    drop: set[int] = field(default_factory=set)  # 0-based indexes of products to hide
    short_names: dict[int, str] = field(default_factory=dict)  # 0-based index -> card name


SHORT_NAME_MAX = 40

# Trailing style/SKU codes ("A-41", "WS-1234", "MTP-1302D-7A1VDF") carry
# nothing a customer reads.
_CODE_TOKEN = re.compile(r"^(?:[A-Z]{1,4}-?\d+[A-Z\d-]*|#?\d{4,})$")


def shorten_name(name: str) -> str:
    """No-LLM short card name: the part before the first comma / pipe / dash
    separator, minus trailing codes. Never cuts mid-name — that loses pack
    sizes ("Milk 1 Litre" -> "Milk 1"); the card clips long names itself.
    Falls back to the full name if nothing sensible is left."""
    full = " ".join(html.unescape(str(name or "")).split())
    words = re.split(r",|\s\|\s|\s[-–]\s", full, maxsplit=1)[0].split()
    while len(words) > 2 and _CODE_TOKEN.match(words[-1]):
        words.pop()
    short = " ".join(words)
    return short if len(short) >= 3 else full


def _valid_short_name(short, full: str) -> str | None:
    short = " ".join(str(short or "").split())
    if len(short) < 3 or len(short) > SHORT_NAME_MAX + 10 or len(short) > len(full) + 5:
        return None
    return short


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
    brand_not_found=None,
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
        f"## Products that will be shown\n{_format_products(products, matched)}\n{checked}"
        + (f"Brand not found: {brand_not_found}\n" if brand_not_found else "")
        + "\n"
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
    brand_not_found=None,
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
        brand_not_found,
    )
    raw = chat_json(
        openai_client,
        REPLY_MODEL,
        _SYSTEM_PROMPT,
        prompt,
        max_tokens=900,
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
    short_names = {}
    raw_names = raw.get("short_names")
    if isinstance(raw_names, dict):
        for n, name in raw_names.items():
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(products):
                short = _valid_short_name(name, str(products[n - 1].get("product_name") or ""))
                if short:
                    short_names[n - 1] = short
    return str(raw.get("reply") or "").strip(), drop, short_names


def compose(
    message: str,
    language: str | None,
    need: dict,
    turn_type: str | None,
    searched: list[str],
    products: list[dict],
    matched: list[list[str]],
    history: list[dict] | None = None,
    brand_not_found: str | None = None,
) -> Composed:
    """products/matched are parallel: matched[i] lists the searched items
    products[i] was found for. brand_not_found names a brand the customer
    asked for that none of the products are from. Raises on LLM/JSON
    failure — the caller falls back to a template reply."""
    args = (message, language, need, turn_type, searched)
    reply, drop, short_names = _write(
        *args, products, matched, history, relevance_checked=False,
        brand_not_found=brand_not_found,
    )
    if drop:
        # Rewrite from what remains, so the reply can't refer to a dropped card.
        keep = [i for i in range(len(products)) if i not in drop]
        rewritten, _, _ = _write(
            *args,
            [products[i] for i in keep],
            [matched[i] for i in keep],
            history,
            relevance_checked=True,
            brand_not_found=brand_not_found,
        )
        reply = rewritten or reply
    print(
        f"[reply.compose] lang={language} shown={len(products) - len(drop)}/{len(products)} "
        f"dropped={[products[i].get('product_name') for i in sorted(drop)]}"
    )
    return Composed(reply=reply, drop=drop, short_names=short_names)
