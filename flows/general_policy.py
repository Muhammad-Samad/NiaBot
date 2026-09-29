from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState
from flows.knowledge_base import GENERAL_POLICY_KB
from utils.logger import get_logger

logger = get_logger(__name__)

class GeneralPolicyFlow(BaseFlow):
    def __init__(self):
        super().__init__()

    def _format_policy(self, topic_data: dict) -> str:
        lines = [f"**{topic_data.get('title', 'Policy Information')}**\n"]
        for section in topic_data.get('sections', []):
            if 'subtitle' in section:
                lines.append(f"*{section['subtitle']}*")
            for point in section.get('points', []):
                lines.append(f"• {point}")
            lines.append("")  # Empty line between sections
        return "\n".join(lines).strip()

    def _extract_specific_points(self, topic_data: dict, point_substring: str) -> str:
        """Return only the bullet point(s) whose text contains point_substring,
        keeping their section subtitle for context (e.g. Road-Side Pickup and
        Express Shipping are Karachi-only, so that must stay attached).
        Returns None if nothing matches.
        """
        substring_lower = point_substring.lower()
        matches = []
        for section in topic_data.get('sections', []):
            subtitle = section.get('subtitle')
            for point in section.get('points', []):
                if substring_lower in point.lower():
                    if subtitle:
                        matches.append(f"*{subtitle}*\n• {point}")
                    else:
                        matches.append(f"• {point}")
        return "\n\n".join(matches) if matches else None

    def _match_specific_topic(self, message: str):
        """Check the customer's raw wording against each topic's declared
        `specific_topics` phrase -> bullet-point-substring map (e.g.
        "roadside pickup" -> "road-side pickup"). The longest matching phrase
        wins, so more precise wording takes priority. Returns
        (topic_key, point_substring) or (None, None) if nothing matches.
        """
        if not message:
            return None, None
        msg_lower = message.lower()
        best = (None, None)
        best_len = 0
        for topic_key, topic_data in GENERAL_POLICY_KB.items():
            for phrase, point_substring in topic_data.get("specific_topics", {}).items():
                if phrase in msg_lower and len(phrase) > best_len:
                    best = (topic_key, point_substring)
                    best_len = len(phrase)
        return best

    def _match_topic_by_keyword(self, message: str) -> str:
        """Fallback keyword matcher: if the LLM didn't return a usable
        policy_topic (or returned one the KB doesn't recognize), check the
        customer's raw wording against each topic's declared `keywords`
        (e.g. "roadside pickup", "express delivery" both alias to "delivery").
        Returns the matched topic key, or None if nothing matches.
        """
        if not message:
            return None
        msg_lower = message.lower()
        for topic_key, topic_data in GENERAL_POLICY_KB.items():
            for keyword in topic_data.get("keywords", []):
                if keyword in msg_lower:
                    return topic_key
        return None

    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        entities = intent_result.entities if isinstance(intent_result.entities, dict) else intent_result.entities.model_dump()
        topic = entities.get("policy_topic")

        logger.info(f"Intent detected: {intent_result.intent}")
        logger.info(f"Policy topic extracted: {topic}")

        user_msg = ""
        if state.conversation_history:
            user_msg = state.conversation_history[-1].get("content", "")

        # Regardless of how the topic was determined (LLM or keyword fallback),
        # check first whether the customer asked about ONE specific shipping
        # method (e.g. "roadside pickup", "express delivery"). If so, answer
        # with just that bullet point instead of the entire policy dump.
        specific_topic, point_substring = self._match_specific_topic(user_msg)
        if specific_topic and specific_topic in GENERAL_POLICY_KB:
            snippet = self._extract_specific_points(GENERAL_POLICY_KB[specific_topic], point_substring)
            if snippet:
                logger.info(f"Knowledge base mapped: Yes (specific topic -> {specific_topic}: {point_substring})")
                return FlowResponse(status="completed", response=snippet)

        if not topic or topic not in GENERAL_POLICY_KB:
            # The LLM either didn't extract a topic or extracted one we don't
            # recognize (e.g. "unknown_policy") - before giving up, check the
            # customer's own wording against each topic's keyword aliases.
            keyword_topic = self._match_topic_by_keyword(user_msg)
            if keyword_topic:
                logger.info(f"Knowledge base mapped: Yes (keyword fallback -> {keyword_topic})")
                topic_data = GENERAL_POLICY_KB[keyword_topic]
                return FlowResponse(status="completed", response=self._format_policy(topic_data))

            logger.info("Knowledge base mapped: Fallback Triggered")
            fallback_msg = "I'm sorry, I don't have information on that specific policy right now. Please call us at (021) 111-624-333 for further assistance."
            return FlowResponse(status="completed", response=fallback_msg)

        logger.info("Knowledge base mapped: Yes")

        # Zero LLM usage for standard responses.
        # Format the response from the structured KB.
        topic_data = GENERAL_POLICY_KB[topic]
        formatted_response = self._format_policy(topic_data)

        return FlowResponse(status="completed", response=formatted_response)
