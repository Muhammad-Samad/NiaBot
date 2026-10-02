"""
Structure-aware chunker for the Naheed policy knowledge base.

Why not fixed-size chunks?  The knowledge base is a numbered list of policy
sections ("17. Return Policy", "23. Refund Timelines", ...). Cutting it every
N characters would split a policy in half or glue two unrelated policies
together, and the retriever would then return half-answers. Instead we chunk
along the document's own structure:

  1. Section split   - top-level "N. Title" headings. Numbered list items
                       ("1. Customer First") are told apart from headings by
                       requiring headings to be sequential and not sit inside
                       a run of consecutive numbered lines.
  2. Section filter  - internal sections written for the bot builders (RAG
                       architecture, metadata, freshness, etc.) are skipped so
                       customers never get them as answers.
  3. Strategy per section
       - FAQ sections   -> one chunk per question/answer pair (precise hits)
       - Subsections    -> "7.1 Groceries & Pets", "Rule 3 - ..." each become
                           their own unit with a breadcrumb title
       - Everything else-> the section is one unit
  4. Size control    - a unit longer than CHUNK_MAX_CHARS is split on
                       paragraph -> line -> sentence boundaries, with
                       CHUNK_OVERLAP_CHARS of trailing context carried over.
                       Tiny tail fragments are merged back.
  5. Context header  - every chunk starts with its breadcrumb
                       ("Naheed policy > 23. Refund Timelines") so that the
                       embedding and the LLM both know what the text is about
                       even when the body is just "1-2 business days".

Nested bullet lists (category trees) are collapsed into
"Parent: child, child, child" lines, which keeps a whole category on a few
dense lines instead of one word per line.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# Sections whose title matches any of these are bot-builder notes, not
# customer-facing information. Their useful rules live in the system prompt.
EXCLUDED_SECTION_PATTERNS = [
    r"\bRAG\b",
    r"\bArchitecture\b",
    r"^Document Purpose$",
    r"^Source Documents",
]

# Section title keyword -> topic (first match wins). Stored as metadata.
TOPIC_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("faq", ("faq",)),
    ("key_facts", ("critical facts",)),
    ("refunds", ("refund", "store-credit", "store credit", "reversal")),
    ("returns", ("return",)),
    ("account", ("account", "otp", "mobile number")),
    ("loyalty", ("loyalty", "points")),
    ("delivery", ("delivery", "deliver", "shipping", "ship", "pickup", "cities")),
    ("payment", ("payment", "pay ", "cash", "card", "unionpay", "qr", "bnpl")),
    ("orders", ("order",)),
    ("warranty", ("warranty",)),
    ("privacy", ("privacy", "cookie", "personal information", "customer information")),
    ("terms", ("terms", "law", "third-party")),
    ("support", ("support", "contact")),
    ("categories", ("categor",)),
    ("company", ("about", "naheed", "vision", "mission", "location", "history")),
]

_SECTION_RE = re.compile(r"^(\d{1,3})\.\s+(\S.*)$")
_NUMBERED_ITEM_RE = re.compile(r"^(\d{1,3})\.\s+\S")
_SUBSECTION_RES = [
    re.compile(r"^(\d{1,3}\.\d{1,3})\s+(\S.*)$"),          # 7.1 Groceries & Pets
    re.compile(r"^(Rule\s+\d+)\s*[—–-]\s*(\S.*)$", re.I),  # Rule 3 — Do not promise
]
_QUESTION_START_RE = re.compile(
    r"^(what|when|where|which|who|why|how|does|do|did|can|could|is|are|was|will|should|may)\b", re.I
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_TERMINAL_PUNCT = (".", ":", "?", "!", ";", ",")


@dataclass
class Section:
    number: str
    title: str
    lines: list[str] = field(default_factory=list)

    @property
    def heading(self) -> str:
        return f"{self.number}. {self.title}" if self.number else self.title


@dataclass
class Chunk:
    id: str
    text: str          # what gets embedded and shown to the LLM
    metadata: dict


# ─────────────────────────────────────────────────────────────────────────────
# Line-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _join_wrapped_lines(lines: list[str]) -> list[str]:
    """Re-join PDF soft line wraps: a line starting in lowercase continues the
    previous line ("...contact Naheed within 7" + "days.")."""
    out: list[str] = []
    for line in lines:
        prev = out[-1] if out else ""
        if (
            line
            and prev
            and line[0].islower()
            and not line.startswith(("- ", "  - "))
            and not prev.endswith((".", "?", "!", ":"))
        ):
            out[-1] = f"{prev} {line}"
        else:
            out.append(line)
    return out


def _collapse_nested_bullets(lines: list[str]) -> list[str]:
    """'- Fresh Products' + '  - Fruits' + '  - Vegetables'
    -> '- Fresh Products: Fruits, Vegetables'"""
    out: list[str] = []
    for line in lines:
        if line.startswith("  - ") and out and out[-1].startswith("- "):
            child = line[4:].strip()
            parent = out[-1]
            out[-1] = f"{parent}, {child}" if ":" in parent else f"{parent}: {child}"
        elif line.startswith("  - "):
            out.append("- " + line[4:].strip())
        else:
            out.append(line)
    return out


def _prev_nonblank(lines: list[str], i: int) -> str:
    for j in range(i - 1, -1, -1):
        if lines[j].strip():
            return lines[j]
    return ""


def _next_nonblank(lines: list[str], i: int) -> tuple[int, str]:
    for j in range(i + 1, len(lines)):
        if lines[j].strip():
            return j, lines[j]
    return -1, ""


def _item_number(line: str) -> int | None:
    m = _NUMBERED_ITEM_RE.match(line)
    return int(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# 1. Section split
# ─────────────────────────────────────────────────────────────────────────────

def split_sections(lines: list[str]) -> list[Section]:
    preamble = Section(number="", title="Introduction")
    sections: list[Section] = []
    expected = 1
    skip_next = False

    for i, line in enumerate(lines):
        if skip_next:  # wrapped heading tail already consumed
            skip_next = False
            continue

        m = _SECTION_RE.match(line)
        if m:
            num = int(m.group(1))
            in_list = (
                _item_number(_prev_nonblank(lines, i)) == num - 1
                or _item_number(_next_nonblank(lines, i)[1]) == num + 1
            )
            if num in (expected, expected + 1) and not in_list:
                title = m.group(2).strip()
                # Heading wrapped onto a second line, e.g.
                # "31. Adding Naheed Loyalty Card to Google" + "Wallet".
                # Headings use a large font and wrap at ~40 chars, so only a
                # single word that would push the heading past that width is
                # treated as a wrap (not a body label like "Bahadurabad").
                j, nxt = _next_nonblank(lines, i)
                if (
                    j == i + 1
                    and len(line) + 1 + len(nxt) >= 40
                    and nxt[:1].isupper()
                    and len(nxt.split()) == 1
                    and not nxt.endswith(_TERMINAL_PUNCT)
                ):
                    title = f"{title} {nxt.strip()}"
                    skip_next = True
                sections.append(Section(number=str(num), title=title))
                expected = num + 1
                continue

        (sections[-1] if sections else preamble).lines.append(line)

    if not sections:  # unstructured document: treat it as one big section
        preamble.title = "Document"
        return [preamble]
    if len("".join(preamble.lines)) > 300:
        sections.insert(0, preamble)
    return sections


def is_excluded(section: Section) -> bool:
    return any(re.search(p, section.title, re.I) for p in EXCLUDED_SECTION_PATTERNS)


def detect_topic(title: str) -> str:
    low = title.lower()
    for topic, keywords in TOPIC_KEYWORDS:
        if any(k in low for k in keywords):
            return topic
    return "general"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Per-section strategies -> list of (subtitle, body) units
# ─────────────────────────────────────────────────────────────────────────────

def _is_faq(section: Section) -> bool:
    questions = [l for l in section.lines if l.endswith("?") and _QUESTION_START_RE.match(l)]
    return "faq" in section.title.lower() or len(questions) >= 5


def _split_faq(lines: list[str]) -> list[tuple[str, str]]:
    content = [l for l in lines if l.strip()]
    pairs: list[tuple[str, str]] = []
    question: list[str] = []
    answer: list[str] = []
    open_question = False

    for i, line in enumerate(content):
        if open_question:
            question.append(line)
            open_question = not line.endswith("?")
            continue
        nxt = content[i + 1] if i + 1 < len(content) else ""
        starts_question = _QUESTION_START_RE.match(line) and (line.endswith("?") or nxt.endswith("?"))
        if starts_question and (answer or not question):
            if question:
                pairs.append((" ".join(question), "\n".join(answer)))
            question, answer = [line], []
            open_question = not line.endswith("?")
        else:
            answer.append(line)

    if question:
        pairs.append((" ".join(question), "\n".join(answer)))
    return [(q, f"Q: {q}\nA: {a}".strip()) for q, a in pairs]


def _split_subsections(lines: list[str]) -> list[tuple[str, str]] | None:
    units: list[tuple[str, list[str]]] = [("", [])]
    found = False
    for line in lines:
        for rx in _SUBSECTION_RES:
            m = rx.match(line)
            if m:
                units.append((f"{m.group(1)} {m.group(2).strip()}", []))
                found = True
                break
        else:
            units[-1][1].append(line)
    if not found:
        return None
    result = [(sub, "\n".join(body).strip()) for sub, body in units]
    # The text before the first subsection becomes an overview that also lists
    # every subsection, so broad questions ("what do you sell?") have a target.
    intro = result[0][1]
    toc = "\n".join(f"- {sub}" for sub, _ in result[1:])
    result[0] = ("Overview", f"{intro}\n{toc}".strip())
    return [(s, b) for s, b in result if b]


# ─────────────────────────────────────────────────────────────────────────────
# 3. Size control
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_pieces(text: str, max_chars: int) -> list[str]:
    """Break text into pieces no larger than max_chars, preferring paragraph,
    then line, then sentence boundaries."""
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        for line in para.split("\n"):
            if len(line) <= max_chars:
                pieces.append(line)
                continue
            buf = ""
            for sent in _SENTENCE_SPLIT_RE.split(line):
                if len(buf) + len(sent) + 1 > max_chars and buf:
                    pieces.append(buf)
                    buf = ""
                while len(sent) > max_chars:  # pathological: no punctuation at all
                    pieces.append(sent[:max_chars])
                    sent = sent[max_chars:]
                buf = f"{buf} {sent}".strip()
            if buf:
                pieces.append(buf)
    return pieces


def split_by_size(text: str, max_chars: int, overlap_chars: int, min_chars: int) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []

    pieces = _atomic_pieces(text, max_chars)
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0

    for piece in pieces:
        if current and size + len(piece) + 1 > max_chars:
            chunks.append(current)
            # carry trailing pieces as overlap, but never the whole chunk
            carry: list[str] = []
            carry_size = 0
            for p in reversed(current[1:]):
                if carry_size + len(p) > overlap_chars:
                    break
                carry.insert(0, p)
                carry_size += len(p) + 1
            current, size = carry, carry_size
        current.append(piece)
        size += len(piece) + 1
    if current:
        chunks.append(current)

    texts = ["\n".join(c) for c in chunks]
    # merge a tiny tail into its predecessor
    if len(texts) > 1 and len(texts[-1]) < min_chars:
        tail = texts.pop()
        texts[-1] = f"{texts[-1]}\n{tail}"
    return texts


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def _chunk_id(source: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join((source, *parts)).encode("utf-8")).hexdigest()[:16]
    return f"{source.rsplit('.', 1)[0][:40].replace(' ', '_')}-{digest}"


def chunk_document(
    text: str,
    source: str,
    max_chars: int = 1200,
    overlap_chars: int = 200,
    min_chars: int = 150,
) -> list[Chunk]:
    lines = _collapse_nested_bullets(_join_wrapped_lines(text.split("\n")))
    sections = split_sections(lines)

    chunks: list[Chunk] = []
    for section in sections:
        if is_excluded(section):
            continue

        if _is_faq(section):
            units, kind = _split_faq(section.lines), "faq"
        else:
            subs = _split_subsections(section.lines)
            if subs:
                units, kind = subs, "subsection"
            else:
                units, kind = [("", "\n".join(section.lines).strip())], "section"

        section_topic = detect_topic(section.title)
        for sub_idx, (subtitle, body) in enumerate(units):
            if not body:
                continue
            # FAQ pairs get the topic of their own question ("Can perfumes be returned?" -> returns)
            topic = detect_topic(subtitle) if kind == "faq" else section_topic
            if topic == "general":
                topic = section_topic
            breadcrumb = section.heading if not subtitle or kind == "faq" else f"{section.heading} > {subtitle}"
            # FAQ question is already in the body; keep the breadcrumb short
            parts = split_by_size(body, max_chars, overlap_chars, min_chars)
            for part_idx, part in enumerate(parts):
                chunk_text = f"Naheed policy > {breadcrumb}\n{part}"
                chunks.append(
                    Chunk(
                        id=_chunk_id(source, section.number, section.title, str(sub_idx), str(part_idx)),
                        text=chunk_text,
                        metadata={
                            "source": source,
                            "section_number": section.number,
                            "section_title": section.title,
                            "subsection": subtitle if kind != "faq" else "",
                            "question": subtitle if kind == "faq" else "",
                            "chunk_type": kind,
                            "topic": topic,
                            "part": part_idx,
                            "parts_total": len(parts),
                            "char_count": len(chunk_text),
                        },
                    )
                )
    return chunks
