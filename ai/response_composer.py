"""response_composer.py - Turns an operations reply into a natural, human-sounding
message in the customer's own language.

Flows and services (order tracking, cancellation, complaints, complaint tracking)
build their replies as plain English text - labels, menus, DB fields. That text
is treated here as a fact sheet: one LLM call rewrites it the way a real support
agent would say it, in the language the customer is using (English, Roman Urdu or
Urdu script), and also reports that language so the whole session stays in it.

Guarantees, so the rewrite can never break a flow:
  - Numbered menus are returned as a separate list of labels and rebuilt as
    "1. label" lines in the original order, so number replies keep resolving
    exactly as before. An `OPTIONS::` marker line tells the frontend to render
    them as buttons (its own detection only knows English prompt words).
  - Frontend marker lines (ITEM_PICKER::, IMAGE_UPLOAD::, SKIP_OPTION::) and the
    item-picker's numbered list are never sent to the LLM; they are re-attached
    verbatim.
  - Every order/ticket/phone number and URL in the original must appear in the
    rewrite, otherwise the original text is used.
  - Any error, timeout or invalid output falls back to the original text.
"""

import json
import os
import re
from typing import List, Optional, Tuple

from ai.base_client import llm_max_retries
from ai.json_chat import chat_json
from ai.prompts import RESPONSE_COMPOSER_PROMPT
from utils.logger import get_logger

logger = get_logger(__name__)

LANGUAGES = {"english", "roman_urdu", "urdu"}
DEFAULT_LANGUAGE = "english"

_MARKER_PREFIXES = ("ITEM_PICKER::", "IMAGE_UPLOAD::", "SKIP_OPTION::", "OPTIONS::")
# Same rules as templates/index.html's option-list detection.
_OPTION_LINE_RE = re.compile(r"^\s*(\d{1,2})[.)]\s+(.+?)\s*$")
_OPTION_PROMPT_RE = re.compile(r"\b(choose|select|pick)\b|reply with a number", re.IGNORECASE)
_MULTI_PROMPT_RE = re.compile(r"\bone or more\b", re.IGNORECASE)
_URDU_SCRIPT_RE = re.compile(r"[؀-ۿ]")
_HAS_LETTER_RE = re.compile(r"[A-Za-z؀-ۿ]")
_URL_RE = re.compile(r"https?://\S+")
_DIGIT_RUN_RE = re.compile(r"\d{3,}")
_YEAR_RE = re.compile(r"(19|20)\d{2}")
# Common Roman Urdu words - only used to pick the language when the LLM call
# fails, so the fallback reply's session language is still right next turn.
_ROMAN_URDU_WORDS = {
    "mera", "meri", "mere", "mujhe", "mujhy", "mujhko", "hai", "hain", "ho", "hoga", "hogi", "kab", "kya", "kyun",
    "kahan", "kidhar", "nahi", "nhi", "karna", "krna", "karni", "krni", "kar", "kr", "do", "dein", "den", "ka",
    "ki", "ke", "ko", "se", "aap", "ap", "bhai", "yaar", "abhi", "tak", "chahiye", "chahye", "kaise", "kesay",
    "batao", "bataen", "wapas", "aya", "aaya", "mila", "hua", "huwa", "gaya", "raha", "rahi", "shukriya",
}

ENABLED = os.getenv("RESPONSE_COMPOSER_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
MODEL = os.getenv("RESPONSE_COMPOSER_MODEL") or "gpt-4.1-mini"
# Writing a reply takes longer than classifying an intent, so this has its own
# (longer) timeout than LLM_TIMEOUT_SECONDS.
TIMEOUT_SECONDS = float(os.getenv("RESPONSE_COMPOSER_TIMEOUT_SECONDS", "15"))

_client = None


def _get_client():
    global _client
    if _client is None:
        import openai
        _client = openai.OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            timeout=TIMEOUT_SECONDS,
            max_retries=llm_max_retries(),
        )
    return _client


# Urdu replies sometimes come back with Eastern Arabic / Urdu digits; IDs and
# phone numbers must stay in 0-9 so the customer can copy them.
_EASTERN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _western_digits(text) -> str:
    return text.translate(_EASTERN_DIGITS) if isinstance(text, str) else ""


def _guess_language(message: str) -> Optional[str]:
    words = re.findall(r"[a-z]+", (message or "").lower())
    if not words:
        return None
    hits = sum(w in _ROMAN_URDU_WORDS for w in words)
    return "roman_urdu" if hits >= 2 or hits / len(words) >= 0.3 else "english"


def detect_language_hint(message: str, previous: Optional[str]) -> Optional[str]:
    """Cheap, deterministic part of language detection. Returns "urdu" for
    Urdu script, the previous language for a message with no words (an order
    ID, a phone number, an image upload), else None - the LLM decides between
    English and Roman Urdu."""
    text = message or ""
    if text.startswith(("[Image Uploaded:", "[Images Uploaded:", "[Image Skipped]")):
        return previous or DEFAULT_LANGUAGE
    if _URDU_SCRIPT_RE.search(text):
        return "urdu"
    if not _HAS_LETTER_RE.search(text):
        return previous or DEFAULT_LANGUAGE
    return None


class _Parsed:
    """The original reply split into the parts the LLM may rewrite and the
    parts that must be kept as-is."""

    def __init__(self, text: str):
        self.markers: List[str] = []
        body: List[str] = []
        for line in text.split("\n"):
            if line.startswith(_MARKER_PREFIXES):
                self.markers.append(line)
            else:
                body.append(line)

        self.item_picker = any(m.startswith("ITEM_PICKER::") for m in self.markers)
        self.before: List[str] = []
        self.options: List[str] = []
        self.after: List[str] = []
        self.frozen: List[str] = []  # item-picker list + hint, kept verbatim
        self.multi = bool(_MULTI_PROMPT_RE.search(text))

        if self.item_picker:
            # The frontend hides these lines and shows a product table instead.
            for line in body:
                if _OPTION_LINE_RE.match(line) or line.strip().lower().startswith("reply with the item numbers"):
                    self.frozen.append(line)
                else:
                    self.before.append(line)
            return

        if _OPTION_PROMPT_RE.search(text):
            start, run = self._find_option_run(body)
            if run:
                self.before = body[:start]
                self.options = [_OPTION_LINE_RE.match(l).group(2) for l in run]
                self.after = body[start + len(run):]
                return
        self.before = body

    @staticmethod
    def _find_option_run(lines: List[str]):
        for i in range(len(lines)):
            run = []
            while i + len(run) < len(lines):
                m = _OPTION_LINE_RE.match(lines[i + len(run)])
                if not m or int(m.group(1)) != len(run) + 1:
                    break
                run.append(lines[i + len(run)])
            if len(run) >= 2:
                return i, run
        return 0, []

    @staticmethod
    def _join(lines: List[str]) -> str:
        return "\n".join(lines).strip()

    @property
    def message(self) -> str:
        return self._join(self.before)

    @property
    def after_options(self) -> str:
        return self._join(self.after)


def _must_keep(text: str) -> List[str]:
    """Identifiers the rewrite is not allowed to drop or alter: URLs and
    order/ticket/phone/tracking numbers. Years are left out - a date may be
    phrased differently ("12 Oct 2026" -> "12 October")."""
    keep = set(_URL_RE.findall(text))
    without_urls = _URL_RE.sub(" ", text)
    keep.update(n for n in _DIGIT_RUN_RE.findall(without_urls) if not _YEAR_RE.fullmatch(n))
    return sorted(keep)


def _build(parsed: _Parsed, message: str, options: List[str], after: str, mark_options: bool) -> str:
    parts = []
    if message:
        parts.append(message)
    if parsed.frozen:
        parts.append("\n".join(parsed.frozen))
    if options:
        parts.append("\n".join(f"{i}. {label}" for i, label in enumerate(options, start=1)))
    if after:
        parts.append(after)
    text = "\n\n".join(parts)
    markers = list(parsed.markers)
    if options and mark_options:
        markers.append("OPTIONS::" + json.dumps({"multi": parsed.multi}))
    if markers:
        text = text + "\n" + "\n".join(markers)
    return text


def compose(
    draft: str,
    user_message: str,
    previous_language: Optional[str] = None,
    previous_bot_message: Optional[str] = None,
    context: Optional[str] = None,
) -> Tuple[str, str]:
    """Returns (reply, language). On any problem the reply is `draft`
    unchanged and the language is the best guess available."""
    hint = detect_language_hint(user_message, previous_language)
    fallback_language = hint or _guess_language(user_message) or previous_language or DEFAULT_LANGUAGE
    if not ENABLED or not draft or not draft.strip():
        return draft, fallback_language

    parsed = _Parsed(draft)
    if not parsed.message and not parsed.options and not parsed.after_options:
        return draft, fallback_language

    payload = {
        "customer_message": user_message,
        "session_language": previous_language or "unknown",
        "language_hint": hint,
        "previous_assistant_message": previous_bot_message,
        "context": context,
        "draft": {
            "message": parsed.message,
            "options": parsed.options,
            "after_options": parsed.after_options,
        },
    }
    try:
        result = chat_json(
            _get_client(),
            MODEL,
            RESPONSE_COMPOSER_PROMPT,
            json.dumps(payload, ensure_ascii=False, default=str),
            max_tokens=700,
            temperature=0.4,
        )
    except Exception as e:
        logger.warning(f"Response composer unavailable, sending the original reply: {e}")
        return draft, fallback_language

    language = result.get("language")
    if hint:
        language = hint
    if language not in LANGUAGES:
        language = fallback_language

    message = _western_digits(result.get("message") or "").strip()
    after = _western_digits(result.get("after_options") or "").strip()
    options = result.get("options") or []
    if not isinstance(options, list) or len(options) != len(parsed.options) \
            or not all(isinstance(o, str) and o.strip() for o in options):
        options = parsed.options
    options = [_western_digits(o).strip() for o in options]

    if not message and parsed.message:
        return draft, language

    original = "\n".join([parsed.message, *parsed.options, parsed.after_options])
    rewritten = "\n".join([message, *options, after])
    missing = [item for item in _must_keep(original) if item not in rewritten]
    if missing:
        logger.warning(f"Response composer dropped {missing}; sending the original reply.")
        return draft, language

    return _build(parsed, message, options, after, mark_options=True), language
