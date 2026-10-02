import os
import time
from groq import Groq
import groq
from ai.base_client import BaseLLMClient, LLMAPIError, llm_timeout_seconds, llm_max_retries
from utils.logger import get_logger
from dotenv import load_dotenv

load_dotenv()
logger = get_logger(__name__)

class GroqClient(BaseLLMClient):
    @property
    def provider_name(self) -> str:
        return "groq"

    def __init__(self):
        self.api_key = os.getenv("GROQ_API_KEY")
        self.default_model = os.getenv("GROQ_MODEL", "llama3-8b-8192")
        
        if not self.api_key:
            logger.warning("GROQ_API_KEY is missing from environment variables.")
            raise ValueError("GROQ_API_KEY must be set in .env")
            
        # Initialize official Groq client
        try:
            self.client = Groq(api_key=self.api_key, timeout=llm_timeout_seconds(), max_retries=llm_max_retries())
        except Exception as e:
            logger.error(f"Failed to initialize Groq SDK: {e}")
            raise ValueError(f"Groq SDK Initialization failed: {e}")
        
    def health_check(self) -> bool:
        if not self.api_key or not getattr(self, "client", None):
            return False
        return True

    def generate_content(self, prompt: str, model: str = None, message_id: str = "unknown") -> str:
        model_name = model or self.default_model
        start_time = time.time()
        
        try:
            logger.debug(f"Sending request to Groq API (Model: {model_name})")
            
            chat_completion = self.client.chat.completions.create(
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                model=model_name,
                temperature=0.0
            )
            
            latency = time.time() - start_time
            logger.debug(f"Groq API request successful. Latency: {latency:.4f}s")
            
            return chat_completion.choices[0].message.content
            
        except Exception as e:
            latency = time.time() - start_time
            error_msg = str(e).upper()
            logger.error(f"Groq API request failed after {latency:.4f}s. Error: {error_msg}")
            
            from ai.base_client import RecoverableLLMError, UnrecoverableLLMError
            
            # Provider-Specific (Recoverable)
            auth_markers = ["401", "403", "UNAUTHORIZED", "FORBIDDEN", "INVALID API KEY", "PERMISSION DENIED", "MISSING CREDENTIALS", "INVALID CREDENTIALS", "AUTHENTICATION ERROR"]
            if any(marker in error_msg for marker in auth_markers):
                raise RecoverableLLMError("Authentication failed", self.provider_name, None, e)
                
            model_markers = ["MODEL NOT FOUND", "MODEL REMOVED", "MODEL UNAVAILABLE"]
            if any(marker in error_msg for marker in model_markers) or ("404" in error_msg and "MODEL" in error_msg):
                raise RecoverableLLMError("Model unavailable", self.provider_name, 404, e)
                
            rate_markers = ["429", "RATE LIMIT", "RESOURCE EXHAUSTED", "TOO MANY REQUESTS"]
            if any(marker in error_msg for marker in rate_markers):
                raise RecoverableLLMError("Rate limited", self.provider_name, 429, e)
                
            timeout_markers = ["TIMEOUT", "READ TIMEOUT", "DEADLINE EXCEEDED"]
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
