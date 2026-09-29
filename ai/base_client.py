import os
from abc import ABC, abstractmethod
from typing import Optional

def llm_timeout_seconds() -> float:
    """Per-request LLM timeout. Kept short so a slow provider fails over to the
    next one in the chain instead of stalling the conversation (SDK defaults are
    up to 10 minutes)."""
    return float(os.getenv("LLM_TIMEOUT_SECONDS", "10"))

def llm_max_retries() -> int:
    """SDK-level retries per provider. Defaults to 0 because LLMFactory already
    fails over to the next provider on error."""
    return int(os.getenv("LLM_MAX_RETRIES", "0"))

class LLMAPIError(Exception):
    def __init__(
        self, 
        message: str, 
        provider: str, 
        status_code: Optional[int], 
        recoverable: bool, 
        original_exception: Optional[Exception] = None
    ):
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.recoverable = recoverable
        self.original_exception = original_exception

    def __str__(self):
        return f"[{self.provider.upper()}] Status: {self.status_code} | Recoverable: {self.recoverable} | {self.message}"

class RecoverableLLMError(LLMAPIError):
    def __init__(self, message: str, provider: str = "unknown", status_code: Optional[int] = None, original_exception: Optional[Exception] = None):
        super().__init__(message, provider, status_code, recoverable=True, original_exception=original_exception)

class UnrecoverableLLMError(LLMAPIError):
    def __init__(self, message: str, provider: str = "unknown", status_code: Optional[int] = None, original_exception: Optional[Exception] = None):
        super().__init__(message, provider, status_code, recoverable=False, original_exception=original_exception)


class BaseLLMClient(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        pass

    @abstractmethod
    def generate_content(self, prompt: str, model: str = None, message_id: str = "unknown") -> str:
        """
        Sends a prompt to the LLM and returns the text response.
        Must explicitly raise LLMAPIError on failure.
        """
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """
        Returns True if the provider is configured correctly and reachable.
        """
        pass
