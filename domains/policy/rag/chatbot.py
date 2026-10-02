"""
PolicyChatbot - the single entry point the API, CLI (and later NiaBot) call.

    bot = PolicyChatbot()
    reply = bot.ask("Can I return perfume?", session_id="abc")

Flow:  validate -> condense follow-up into standalone question -> retrieve
       -> answer with grounded prompt -> store turn in memory
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict, dataclass, field

from config.policy import settings
from domains.policy.rag.llm import LLMError, OpenAIChat
from domains.policy.rag.memory import ConversationMemory
from domains.policy.rag.prompts import CONDENSE_PROMPT, SYSTEM_PROMPT, build_user_turn
from domains.policy.rag.retriever import PolicyRetriever

logger = logging.getLogger(__name__)

# Words that signal a message leans on earlier turns ("and for Lahore?",
# "what about that?", "uska kya?"). Only such messages are rewritten.
_FOLLOW_UP_RE = re.compile(
    r"\b(it|its|that|this|those|these|they|them|there|same|also|else|then|instead|above|"
    r"previous|what about|how about|and for|and in|and if|what if|"
    r"ye|yeh|wo|woh|is ka|iska|uska|unka|wahan|yahan|aur)\b",
    re.I,
)
_MAX_SHORT_FOLLOW_UP_WORDS = 2  # "Islamabad?", "and Lahore" - not "Can I return perfume?"


@dataclass
class Source:
    section: str
    subsection: str
    question: str
    score: float


@dataclass
class ChatResponse:
    answer: str
    session_id: str
    standalone_question: str
    sources: list[Source] = field(default_factory=list)
    grounded: bool = True  # False when no relevant chunk was found

    def to_dict(self) -> dict:
        return asdict(self)


class PolicyChatbot:
    def __init__(
        self,
        retriever: PolicyRetriever | None = None,
        llm: OpenAIChat | None = None,
        memory: ConversationMemory | None = None,
    ):
        self.retriever = retriever or PolicyRetriever()
        self.llm = llm or OpenAIChat()
        self.memory = memory or ConversationMemory()

    # ─── public ──────────────────────────────────────────────────────────
    def ask(self, message: str, session_id: str | None = None) -> ChatResponse:
        session_id = session_id or uuid.uuid4().hex
        message = (message or "").strip()

        if not message:
            return ChatResponse("Please type your question about Naheed.pk.", session_id, "", grounded=False)
        if len(message) > settings.max_input_chars:
            return ChatResponse(
                f"Your message is too long. Please keep it under {settings.max_input_chars} characters.",
                session_id, "", grounded=False,
            )

        history = self.memory.get(session_id)
        try:
            standalone = self._condense(message, history)
            results = self._retrieve(message, standalone)
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": build_user_turn(message, standalone, results)},
            ]
            answer = self.llm.complete(messages)
        except LLMError:
            logger.exception("LLM failure")
            return ChatResponse(
                "Sorry, I'm having trouble answering right now. "
                f"Please try again shortly or contact Naheed support at {settings.support_phone}.",
                session_id, message, grounded=False,
            )

        self.memory.append(session_id, message, answer)
        return ChatResponse(
            answer=answer,
            session_id=session_id,
            standalone_question=standalone,
            sources=[
                Source(
                    section=f"{r.metadata.get('section_number')}. {r.metadata.get('section_title')}",
                    subsection=r.metadata.get("subsection", ""),
                    question=r.metadata.get("question", ""),
                    score=round(r.score, 3),
                )
                for r in results
            ],
            grounded=bool(results),
        )

    def reset(self, session_id: str) -> None:
        self.memory.clear(session_id)

    # ─── internals ───────────────────────────────────────────────────────
    @staticmethod
    def _looks_like_follow_up(message: str) -> bool:
        return len(message.split()) <= _MAX_SHORT_FOLLOW_UP_WORDS or bool(_FOLLOW_UP_RE.search(message))

    def _retrieve(self, message: str, standalone: str):
        """Search with the customer's own words, and also with the rewritten
        question when there is one, so a bad rewrite can never hide the
        section the customer actually asked about."""
        if standalone == message:
            return self.retriever.retrieve(message)
        merged = {}
        for r in self.retriever.retrieve(message) + self.retriever.retrieve(standalone):
            if r.id not in merged or r.distance < merged[r.id].distance:
                merged[r.id] = r
        return sorted(merged.values(), key=lambda r: r.distance)[: settings.top_k]

    def _condense(self, message: str, history: list[dict]) -> str:
        """Turn a follow-up ("and for Islamabad?") into a standalone question so
        retrieval works. Skipped on the first turn and for messages that are
        already self-contained, which also saves an LLM call."""
        if not history or not self._looks_like_follow_up(message):
            return message
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in history[-4:])
        try:
            rewritten = self.llm.complete(
                [
                    {"role": "system", "content": CONDENSE_PROMPT},
                    {"role": "user", "content": f"Conversation:\n{convo}\n\nLatest message: {message}"},
                ],
                temperature=0,
                max_tokens=100,
            )
            return rewritten or message
        except LLMError:
            return message
