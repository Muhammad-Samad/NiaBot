"""eval_conversations.py - Regression check for multi-turn shopping
understanding (domains/shopping/conversation.py).

Runs scripted conversations through conversation.understand() only — no DB,
no Typesense — simulating each assistant reply and the product cards it
would have shown, and checks every turn against expectations. Re-run after
any prompt or model change:

    python -m scripts.eval_conversations            # all cases
    python -m scripts.eval_conversations walls      # cases whose name contains "walls"

Add real customer conversations that went wrong as new cases.
"""

import sys

from domains.shopping import conversation


def has(*words):
    """Search text contains every word (case-insensitive)."""
    return ("has " + "+".join(words), lambda t: all(w.lower() in t.search_text.lower() for w in words))


def lacks(*words):
    """Search text contains none of the words."""
    return ("lacks " + "+".join(words), lambda t: not any(w.lower() in t.search_text.lower() for w in words))


def action(name):
    return (f"action={name}", lambda t: t.action == name)


def action_in(*names):
    return (f"action in {names}", lambda t: t.action in names)


def turn_type(*names):
    return (f"turn_type in {names}", lambda t: t.turn_type in names)


def remembers(word):
    return (f"remembers {word}", lambda t: any(word.lower() in f.lower() for f in t.remember))


def shows(n):
    return (f"shows product {n}", lambda t: n in t.show_products)


def reply_words(lo, hi):
    return (f"reply {lo}-{hi} words", lambda t: lo <= len(t.reply.split()) <= hi)


def prefers_cheaper(expected=True):
    return (f"prefer_cheaper={expected}", lambda t: bool((t.need or {}).get("prefer_cheaper")) == expected)


def no_price_limit():
    """A list must never get a 'cheaper than shown' price limit."""
    return ("no price_direction", lambda t: t.price_direction is None)


# Earlier non-shopping messages a case can start from (role, message).
ORDER_CHAT = [
    ("user", "mujhy mera order track krna hai"),
    ("assistant", "Main aapki madad kar sakti hoon aapka order track karne mein. Barae meherbani apna Order ID bata dein."),
    ("user", "2000098442"),
    ("assistant", "Security ke liye please woh phone number bata dein jo is order ke saath register hai."),
    ("user", "03112972475"),
    ("assistant", "Aapka order 2000098442 abhi partial picked stage mein hai, matlab kuch items pick ho chuke hain."),
    ("user", "mujhy nahi chahiye ab cancel krdo isy"),
    ("assistant", "Aapka order ab fulfillment stage mein hai is liye ise automatically cancel nahi kiya ja sakta. "
                  "Barah-e-karam humari Customer Support team se (021) 111-624-333 par raabta karein."),
    ("user", "nahi krna rabta"),
    ("assistant", "Agar aapko aur madad chahiye ho to barah-e-karam hamari Customer Support team se (021) 111-624-333 par raabta karein."),
]

# (name, memory, [(customer message, [checks]), ...], optional prior history)
CASES = [
    ("gift_son_age", None, [
        ("suggest some gift options for my son", [action_in("search", "clarify")]),
        ("he is 25 year old", [action("search"), turn_type("refine"), has("men"), lacks("toy", "25 year"), remembers("son")]),
    ]),
    ("room_decor_walls", None, [
        ("i need decorating material for my room", [action("search")]),
        ("for walls", [action("search"), turn_type("refine"), has("wall"), lacks("rug", "cushion", "for walls")]),
    ]),
    ("spouse_outfits_male", None, [
        ("i need some outfits for my spouse", [action_in("clarify", "search")]),
        ("he is male not female", [action("search"), has("men"), lacks("women", "female")]),
    ]),
    ("wife_then_husband_correction", None, [
        ("show me some outfits for my wife", [action("search"), has("women")]),
        ("sorry i meant my husband", [action("search"), turn_type("correct", "refine"), has("men"), lacks("women")]),
    ]),
    ("shoes_refinements", None, [
        ("running shoes", [action("search"), has("running shoe")]),
        ("what about Nike?", [action("search"), has("nike", "running")]),
        ("for women", [action("search"), has("nike", "women")]),
        ("show more", [action("search"), turn_type("more")]),
    ]),
    ("brand_followup_samsung", None, [
        ("mobile phones", [action("search")]),
        ("any Samsung?", [action("search"), has("samsung")]),
    ]),
    ("color_size_followups", None, [
        ("sneakers for men", [action("search")]),
        ("in white", [action("search"), has("white", "sneaker")]),
        ("only large", [action("search"), has("sneaker")]),
    ]),
    ("audience_followup", None, [
        ("perfumes", [action("clarify")]),
        ("for men's", [action("search"), has("men", "perfume")]),
    ]),
    ("topic_switch_resets", None, [
        ("laptops under 150k", [action("search"), has("laptop")]),
        ("now i need cooking oil", [action("search"), turn_type("new"), has("oil"), lacks("laptop")]),
    ]),
    ("reference_price", None, [
        ("running shoes", [action("search")]),
        ("how much is the second one?", [action("answer"), shows(2)]),
    ]),
    ("roman_urdu_sister_gift", None, [
        ("mujhe apni behan ke liye gift chahiye", [action_in("search", "clarify")]),
        ("wo 15 saal ki hai", [action("search"), lacks("15 saal")]),
    ]),
    ("breakfast_for_kids", None, [
        ("breakfast items", [action("search")]),
        ("for kids", [action("search"), turn_type("refine")]),
    ]),
    ("grocery_budget", None, [
        ("i want monthly grocery items", [action("search"), reply_words(60, 140)]),
        ("can you suggest only cheap items as you give very expensive cooking oil. 2 to 3 Liters is enough for me",
         [action("search"), has("oil"), lacks("cheap", "affordable", "budget", "economical", "2 to 3"), no_price_limit(),
          prefers_cheaper()]),
        ("add some snacks too", [action("search"), prefers_cheaper()]),
        ("now show me premium perfumes", [action("search"), turn_type("new"), prefers_cheaper(False)]),
    ]),
    ("cheap_single_product", None, [
        ("sasta shampoo dikhao", [action("search"), has("shampoo"), lacks("sasta"), prefers_cheaper()]),
        ("for men", [action("search"), has("men", "shampoo"), prefers_cheaper()]),
    ]),
    ("biryani_descriptive_then_extras", None, [
        ("chicken biryani ingredients", [action("search"), has("rice", "chicken"), reply_words(60, 140)]),
        ("yes show those too", [action("search"), turn_type("more"), lacks("basmati", "chicken meat")]),
    ]),
    ("single_product_short_reply", None, [
        ("face wash", [action("search"), reply_words(3, 55)]),
        ("cheaper ones", [action("search"), has("face wash")]),
    ]),
    ("off_topic", None, [
        ("who won the cricket match yesterday?", [action("off_topic")]),
    ]),
    ("memory_recall", {
        "facts": ["Has a son, about 25 years old"],
        "past": [{"date": "2026-10-02", "summary": "Looked at gift ideas for 25-year-old son: watches, perfumes, wallets."}],
    }, [
        ("what was i looking for last time?", [action("answer"), turn_type("recall")]),
        ("yes show me some again", [action("search"), has("men")]),
    ]),
    ("memory_not_forced", {
        "facts": ["Has a son, about 25 years old"],
        "past": [{"date": "2026-10-02", "summary": "Looked at gift ideas for 25-year-old son."}],
    }, [
        ("basmati rice", [action("search"), has("rice"), lacks("gift", "son")]),
    ]),
    # Real chat: vague requests after an order-tracking exchange were searched
    # instead of clarified.
    ("clarify_after_order_chat", None, [
        ("acha mujhy perfumes dikhao", [action("clarify"), turn_type("new")]),
        ("mujhy gift items chahiyen", [action("clarify"), turn_type("new")]),
    ], ORDER_CHAT),
    ("gift_memory_not_assumed", {"facts": ["Has a son, about 25 years old"], "past": []}, [
        ("mujhy gift items chahiyen", [action("clarify")]),
    ], ORDER_CHAT),
]


def _fake_products(turn):
    names = turn.items[:5] if turn.kind == "list" else [f"{turn.query} {n}" for n in ("Model A", "Model B", "Model C")]
    return [{"product_name": n, "price": 1500 * (i + 1), "category": ""} for i, n in enumerate(names)]


def run_case(name, memory, turns, prior=()):
    print(f"\n=== {name} ===")
    state, shown, failures = {}, [], 0
    history = [{"role": r, "message": m, "products": [], "age_minutes": 0} for r, m in prior]
    for msg, checks in turns:
        turn = conversation.understand(msg, state, history, shown, memory)
        print(f"  > {msg}")
        print(f"    {turn.turn_type}/{turn.action} kind={turn.kind} search={turn.search_text!r}")
        print(f"    reply: {turn.reply}")
        if turn.remember:
            print(f"    remember: {turn.remember}")
        for label, check in checks:
            if not check(turn):
                failures += 1
                print(f"    FAIL: {label}")

        history.append({"role": "user", "message": msg, "products": [], "age_minutes": 0})
        products = _fake_products(turn) if turn.action == "search" else []
        history.append({"role": "assistant", "message": turn.reply, "products": [p["product_name"] for p in products], "age_minutes": 0})
        if turn.action == "search":
            shown = products
            searched = state.get("searched", []) if turn.turn_type == "more" else []
            state = {"need": turn.need, "searched": searched + (turn.items or [turn.query])}
        elif turn.need:
            state = {**state, "need": turn.need}
    return failures


def main():
    selected = [c for c in CASES if len(sys.argv) < 2 or sys.argv[1] in c[0]]
    total = sum(run_case(*c) for c in selected)
    print(f"\n{len(selected)} conversations, {total} failed checks")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
