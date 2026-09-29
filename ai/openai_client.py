import os
import time
from typing import Optional
import openai
from utils.logger import get_logger
from ai.base_client import BaseLLMClient, LLMAPIError, RecoverableLLMError, UnrecoverableLLMError, llm_timeout_seconds, llm_max_retries

logger = get_logger(__name__)

class OpenAIClient(BaseLLMClient):
    @property
    def provider_name(self) -> str:
        return "openai"

    def __init__(self):
        # We don't do network checks here. Only config checks.
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            logger.warning("OPENAI_API_KEY is missing from environment variables.")
            raise ValueError("OPENAI_API_KEY is missing from environment variables.")
            
        self.default_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        
        try:
            self.client = openai.OpenAI(api_key=self.api_key, timeout=llm_timeout_seconds(), max_retries=llm_max_retries())
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI SDK: {e}")
            raise ValueError(f"OpenAI SDK Initialization failed: {e}")

    def health_check(self) -> bool:
        """
        Offline health check. Only validates config, no network calls.
        """
        if not self.api_key or not self.client:
            return False
        return True

    def _classify_error(self, e: Exception) -> LLMAPIError:
        error_msg = str(e).upper()
        
        # Provider-Specific (Recoverable)
        auth_markers = ["401", "403", "UNAUTHORIZED", "FORBIDDEN", "INVALID API KEY", "PERMISSION DENIED", "MISSING CREDENTIALS", "INVALID CREDENTIALS", "AUTHENTICATION ERROR"]
        if any(marker in error_msg for marker in auth_markers):
            return RecoverableLLMError("Authentication failed", self.provider_name, None, e)
            
        model_markers = ["MODEL NOT FOUND", "MODEL REMOVED", "MODEL UNAVAILABLE"]
        if any(marker in error_msg for marker in model_markers):
            return RecoverableLLMError("Model unavailable", self.provider_name, None, e)
            
        if isinstance(e, openai.APIError) and getattr(e, "status_code", None) == 404:
            if "MODEL" in error_msg or "NOT FOUND" in error_msg:
                return RecoverableLLMError("Model unavailable", self.provider_name, 404, e)
                
        rate_markers = ["429", "RATE LIMIT", "RESOURCE EXHAUSTED", "TOO MANY REQUESTS"]
        if any(marker in error_msg for marker in rate_markers):
            return RecoverableLLMError("Rate limited", self.provider_name, 429, e)
            
        timeout_markers = ["TIMEOUT", "READ TIMEOUT", "DEADLINE EXCEEDED"]
        if any(marker in error_msg for marker in timeout_markers):
            return RecoverableLLMError("Network timeout", self.provider_name, None, e)
            
        service_markers = ["500", "502", "503", "504", "DNS", "SSL", "CONNECTION", "DISCONNECT"]
        if any(marker in error_msg for marker in service_markers):
            return RecoverableLLMError("Service unavailable", self.provider_name, None, e)
            
        # System-Wide (Unrecoverable)
        invalid_markers = ["INVALID ENDPOINT", "MALFORMED PAYLOAD", "INVALID JSON", "MISSING REQUIRED PARAMETERS", "INVALID TOOL SCHEMA", "SERIALIZATION", "DESERIALIZATION", "SDK MISUSE"]
        if any(marker in error_msg for marker in invalid_markers):
            return UnrecoverableLLMError("System-wide validation failed", self.provider_name, None, e)
            
        if isinstance(e, openai.APIError) and getattr(e, "status_code", None) == 400:
            return UnrecoverableLLMError("Malformed payload", self.provider_name, 400, e)
            
        if isinstance(e, openai.APIError) and getattr(e, "status_code", None) == 404:
            return UnrecoverableLLMError("Invalid endpoint", self.provider_name, 404, e)
            
        # Default to recoverable to guarantee chain continuation unless strictly caught
        return RecoverableLLMError("Service unavailable", self.provider_name, None, e)


    def generate_content(self, prompt: str, model: str = None, message_id: str = "unknown") -> str:
        model_name = model or self.default_model
        
        try:
            logger.debug(f"Sending request to OpenAI API (Model: {model_name})")
            logger.info(f"OpenAIClient -> OpenAI API (message_id={message_id})")
            
            response = self.client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=200,
                temperature=0.1
            )
            
            if not response or not response.choices or not response.choices[0].message.content:
                raise ValueError("Received empty response from OpenAI.")

            # The static intent prompt is sent first, so OpenAI's automatic prompt
            # caching should cover most of it - log it to confirm.
            usage = getattr(response, "usage", None)
            details = getattr(usage, "prompt_tokens_details", None) if usage else None
            if usage:
                logger.info(f"OpenAI prompt tokens: {usage.prompt_tokens} (cached: {getattr(details, 'cached_tokens', 0) or 0})")

            return response.choices[0].message.content
            
        except Exception as e:
            error_msg = str(e).upper()
            logger.error(f"OpenAI API request failed. Error: {error_msg}")
            raise self._classify_error(e) from e
