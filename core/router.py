import re
from dataclasses import dataclass, field
from typing import Dict, Any

@dataclass
class IntentResult:
    intent: str
    confidence: float
    entities: Dict[str, Any] = field(default_factory=dict)
    priority: str = "low"
    mood: str = "happy"

class IntentRouter:
    """
    Analyzes natural language messages to determine intent and extract entities.
    Designed to be modular so it can be seamlessly swapped with an LLM-based 
    intent classifier in the future.
    """
    def __init__(self):
        # Basic rule-based patterns for initial implementation
        self.patterns = {
            "order_tracking": [r"track.*order", r"where.*order", r"status.*order", r"order.*status"],
            "modify_order": [
                r"modify.*order", r"change.*order", r"\bedit\b.*order", r"order.*change", r"order.*modify",
                r"add.*product", r"add.*item", r"remove.*product", r"remove.*item", r"add.*to.*order", r"remove.*from.*order"
            ],
            "special_request": [
                r"jaldi.*(deliver|bhej)", r"^(?!.*(what|how|do you|can i)).*express.*(delivery|kar)", 
                r"^(?!.*(what|how|do you|can i)).*urgent.*delivery", r"priority.*(delivery|dein|order)", 
                r"same.*day.*(delivery|bhej)", r"special.*request", r"rush.*order", r"\bexpedite\b.*order", 
                r"speed.*up.*delivery", r"deliver.*today", r"deliver.*before"
            ],
            "complaint": [
                r"complain", r"broken", r"damaged", r"wrong item", r"issue",
                r"late", r"delay", r"abhi.*tak.*nahi.*aya", r"receive.*nahi"
            ],
            "greeting": [r"^hi\b", r"^hello\b", r"^hey\b", r"^good morning", r"^good afternoon"],
        }
        # Look for numbers that might be order IDs (assumed 5-10 digits)
        self.order_id_pattern = r"\b[0-9]{5,10}\b"

        # Keyword heuristics used as a fallback priority/mood judge when the
        # LLM classifier is unavailable.
        self.high_priority_keywords = [
            "complain", "complaint", "damaged", "broken", "wrong item", "wrong product",
            "missing", "expired", "leak", "refund", "warranty", "urgent", "asap",
            "immediately", "still not", "not resolved", "worst",
        ]
        self.sad_mood_keywords = [
            "worst", "terrible", "disappointed", "upset", "angry", "frustrated",
            "ridiculous", "unacceptable", "annoyed", "not happy", "very bad",
            "waste of time", "keep happening", "again and again",
            # Signals that a problem is persisting/being ignored - genuine
            # frustration even without an explicit "angry" word.
            "still not resolved", "not resolved yet", "koi jawab nahi",
            "abhi tak koi jawab", "how many times",
        ]
        # Rude/insulting language directed at the agent or company - always a strong
        # signal of anger, independent of whether an order/product is even mentioned.
        self.insult_keywords = [
            "idiot", "idiots", "stupid", "moron", "morons", "useless", "incompetent",
            "shut up", "scam", "fraud", "nonsense", "garbage service", "pathetic",
            "trash service", "dumb", "rubbish",
        ]

    def _is_shouting(self, message: str) -> bool:
        """Detects ALL-CAPS 'shouting' as a signal of anger/frustration."""
        letters = [c for c in message if c.isalpha()]
        if len(letters) < 4:
            return False
        uppercase_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        return uppercase_ratio >= 0.7

    def _judge_priority_and_mood(self, message: str, message_lower: str):
        is_insult = any(kw in message_lower for kw in self.insult_keywords)
        is_shouting = self._is_shouting(message)

        priority = "high" if (
            any(kw in message_lower for kw in self.high_priority_keywords) or is_insult or is_shouting
        ) else "low"

        # Mood is judged independently of priority - a message can be high
        # priority (e.g. a routine complaint or refund request) while still
        # being calm/neutral in tone. Only genuine negative-emotion language
        # (frustration/anger words, insults, or shouting) makes it "sad".
        mood = "sad" if (
            any(kw in message_lower for kw in self.sad_mood_keywords) or is_insult or is_shouting
        ) else "happy"

        return priority, mood

    def route(self, message: str) -> IntentResult:
        if not message or not isinstance(message, str):
            return IntentResult(intent="unknown", confidence=0.0)

        message_lower = message.lower().strip()

        priority, mood = self._judge_priority_and_mood(message, message_lower)

        # Extract entities
        entities = {}
        order_match = re.search(self.order_id_pattern, message_lower)
        if order_match:
            entities["order_id"] = order_match.group(0)

        # Determine Intent
        best_intent = "unknown"
        highest_confidence = 0.0

        for intent, patterns in self.patterns.items():
            for pattern in patterns:
                if re.search(pattern, message_lower):
                    best_intent = intent
                    highest_confidence = 0.85 # Mock confidence score
                    break
            if best_intent != "unknown":
                break

        # Fallbacks
        if best_intent == "unknown":
            if entities.get("order_id"):
                # If they just provided an order ID, assume they want tracking
                best_intent = "order_tracking"
                highest_confidence = 0.70
            else:
                # General query fallback
                best_intent = "general_query"
                highest_confidence = 0.50

        return IntentResult(
            intent=best_intent,
            confidence=highest_confidence,
            entities=entities,
            priority=priority,
            mood=mood
        )
