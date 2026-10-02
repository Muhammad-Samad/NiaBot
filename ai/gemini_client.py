import os
import time
from google import genai
from google.genai import types
from dotenv import load_dotenv
from typing import Optional
from utils.logger import get_logger
from ai.base_client import BaseLLMClient, LLMAPIError, llm_timeout_seconds

load_dotenv()
logger = get_logger(__name__)

class GeminiClient(BaseLLMClient):
    @property
    def provider_name(self) -> str:
        return "gemini"

    def __init__(self):
        # Validate the key exists before startup
        self.api_key = os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            logger.critical("GEMINI_API_KEY is missing from environment variables.")
            raise ValueError("GEMINI_API_KEY is missing from environment variables.")
        
        # Initialize official client
        try:
            # HttpOptions.timeout is in milliseconds
            self.client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=int(llm_timeout_seconds() * 1000))
            )
        except Exception as e:
            logger.error(f"Failed to initialize Gemini SDK: {e}")
            raise ValueError(f"Gemini SDK Initialization failed: {e}")
            
        self.default_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
        
    def health_check(self) -> bool:
        """
        Offline health check. Only validates config, no network calls.
        """
        if not self.api_key or not self.client:
            return False
        return True

    def generate_content(self, prompt: str, model: str = None, message_id: str = "unknown") -> str:
        model_name = model or self.default_model
        start_time = time.time()
        
        try:
            logger.debug(f"Sending request to Gemini API (Model: {model_name})")
            response = self.client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            latency = time.time() - start_time
            logger.debug(f"Gemini API request successful. Latency: {latency:.4f}s")
            
            if not response or not response.text:
                raise LLMAPIError("Received empty or invalid response from Gemini API.", "gemini", 500, True)
                
            return response.text
            
        except Exception as e:
            latency = time.time() - start_time
            error_msg = str(e).upper()
            
            from ai.base_client import RecoverableLLMError, UnrecoverableLLMError
            
            model_markers = ["MODEL NOT FOUND", "MODEL REMOVED", "MODEL UNAVAILABLE", "NO LONGER AVAILABLE TO NEW USERS"]
            is_model_unavailable = any(marker in error_msg for marker in model_markers) or ("404" in error_msg and "MODEL" in error_msg)
            
            if is_model_unavailable:
                logger.error(
                    f"Gemini model '{model_name}' is unavailable for this project.\n"
                    "Please update the GEMINI_MODEL environment variable."
                )
            else:
                logger.error(f"Gemini API request failed after {latency:.4f}s. Error: {error_msg}")
            
            # Provider-Specific (Recoverable)
            auth_markers = ["401", "403", "UNAUTHORIZED", "FORBIDDEN", "INVALID API KEY", "PERMISSION DENIED", "MISSING CREDENTIALS", "INVALID CREDENTIALS", "AUTHENTICATION ERROR"]
            if any(marker in error_msg for marker in auth_markers):
                raise RecoverableLLMError("Authentication failed", self.provider_name, None, e)
                
            if is_model_unavailable:
                raise RecoverableLLMError("Model unavailable", self.provider_name, 404, e)
                
            rate_markers = ["429", "RATE LIMIT", "RESOURCE_EXHAUSTED", "TOO MANY REQUESTS"]
            if any(marker in error_msg for marker in rate_markers):
                raise RecoverableLLMError("Rate limited", self.provider_name, 429, e)
                
            timeout_markers = ["TIMEOUT", "READ TIMEOUT", "DEADLINE EXCEEDED", "DEADLINE_EXCEEDED"]
            if any(marker in error_msg for marker in timeout_markers):
                raise RecoverableLLMError("Network timeout", self.provider_name, None, e)
                
            service_markers = ["500", "502", "503", "504", "DNS", "SSL", "CONNECTION", "DISCONNECT"]
            if any(marker in error_msg for marker in service_markers):
                raise RecoverableLLMError("Service unavailable", self.provider_name, None, e)
                
            # System-Wide (Unrecoverable)
            invalid_markers = ["INVALID ENDPOINT", "MALFORMED PAYLOAD", "INVALID JSON", "MISSING REQUIRED PARAMETERS", "INVALID TOOL SCHEMA", "SERIALIZATION", "DESERIALIZATION", "SDK MISUSE"]
            if any(marker in error_msg for marker in invalid_markers):
                raise UnrecoverableLLMError("System-wide validation failed", self.provider_name, None, e)
                
            if "400" in error_msg or "INVALID ARGUMENT" in error_msg:
                raise UnrecoverableLLMError("Malformed payload", self.provider_name, 400, e)
                
            if "404" in error_msg or "NOT_FOUND" in error_msg:
                raise UnrecoverableLLMError("Invalid endpoint", self.provider_name, 404, e)
                
            # Default to recoverable
            raise RecoverableLLMError("Service unavailable", self.provider_name, None, e)

class GeminiAPIError(LLMAPIError):
    def __init__(self, message: str, status_code: Optional[int] = None, recoverable: bool = True, original_exception: Optional[Exception] = None):
        super().__init__(message, provider="gemini", status_code=status_code, recoverable=recoverable, original_exception=original_exception)
