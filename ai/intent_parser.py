import json
import re
from typing import Optional

from pydantic import ValidationError

from ai.base_client import LLMAPIError
from ai.llm_factory import LLMFactory
from ai.schemas import IntentResult
from ai.prompts import INTENT_EXTRACTION_PROMPT, STATE_CONTEXT_INJECTION
from core.state_manager import ConversationState
from utils.logger import get_logger

logger = get_logger(__name__)

class IntentParserError(Exception):
    pass

class IntentParser:
    def __init__(self, factory: Optional[LLMFactory] = None):
        # Allow dependency injection for testing
        self.factory = factory or LLMFactory()

    def clean_json_response(self, raw_text: str) -> str:
        """
        Safely extracts JSON from Markdown or raw text.
        Handles cases where the LLM might wrap output in ```json ... ```
        """
        text = raw_text.strip()
        
        if text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline != -1:
                text = text[first_newline:].strip()
            if text.endswith("```"):
                text = text[:-3].strip()
                
        # Attempt to find the outermost JSON braces just in case
        json_match = re.search(r'(\{.*\})', text, re.DOTALL)
        if json_match:
            return json_match.group(1).strip()
            
        return text

    def parse_intent(self, user_message: str, state: Optional[ConversationState] = None, message_id: str = "unknown") -> IntentResult:
        """
        Sends the message to Gemini and returns a validated IntentResult.
        Optionally uses conversation state to provide contextual history.
        """
        context_block = ""
        if state and state.current_flow:
            context_block = STATE_CONTEXT_INJECTION.format(
                current_flow=state.current_flow,
                current_stage=state.current_stage or "None",
                waiting_for_order_id=state.waiting_for_order_id,
                known_entities=state.entities,
                last_assistant_message=state.last_assistant_message or "None"
            )

        prompt = f"{INTENT_EXTRACTION_PROMPT}\n{context_block}\nUser Message: \"{user_message}\""
        
        try:
            logger.debug(f"Requesting intent classification for message: '{user_message}' (Context: {'Yes' if context_block else 'No'})")
            logger.info(f"IntentParser -> LLMFactory (message_id={message_id})")
            raw_response = self.factory.generate_content_with_failover(prompt, message_id=message_id)
            
            cleaned_json = self.clean_json_response(raw_response)
            parsed_data = json.loads(cleaned_json)
            
            # Pydantic validation
            result = IntentResult(**parsed_data)
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Gemini response as JSON: {e}")
            raise IntentParserError("Malformed JSON response from LLM") from e
        except ValidationError as e:
            logger.error(f"LLM response failed schema validation: {e}")
            raise IntentParserError("Schema validation failed") from e
        except LLMAPIError as e:
            logger.error(f"LLM API failure during intent parsing: {e}")
            raise IntentParserError("API Error") from e
        except Exception as e:
            logger.error(f"Unexpected error during intent parsing: {e}")
            raise IntentParserError("Unexpected Error") from e


