import os
import time
from typing import Dict, Type, List
from ai.base_client import BaseLLMClient, LLMAPIError
from utils.logger import get_logger

logger = get_logger(__name__)

class ProviderState:
    CONFIGURED = "CONFIGURED"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    AUTH_FAILED = "AUTH_FAILED"
    DISABLED = "DISABLED"

class ProviderHealth:
    def __init__(self, name: str, max_failures: int):
        self.name = name
        self.max_failures = max_failures
        self.state = ProviderState.CONFIGURED
        
        # Consecutive recoverable failures
        self.consecutive_failures = 0
        
        # Metrics
        self.request_count = 0
        self.success_count = 0
        self.failure_count = 0
        self.failover_count = 0
        self.total_latency = 0.0

    def record_success(self, latency: float):
        self.request_count += 1
        self.success_count += 1
        self.total_latency += latency
        self.consecutive_failures = 0
        
        if self.state in [ProviderState.CONFIGURED, ProviderState.DEGRADED, ProviderState.AUTH_FAILED]:
            if self.state == ProviderState.DEGRADED:
                logger.info(f"Provider {self.name} self-healed. State: DEGRADED -> HEALTHY")
            elif self.state == ProviderState.AUTH_FAILED:
                logger.info(f"Provider {self.name} self-healed auth. State: AUTH_FAILED -> HEALTHY")
            else:
                logger.info(f"Provider {self.name} passed first request. State: CONFIGURED -> HEALTHY")
            self.state = ProviderState.HEALTHY

    def record_failure(self, latency: float, message: str, recoverable: bool):
        self.request_count += 1
        self.failure_count += 1
        self.total_latency += latency
        
        if recoverable:
            self.failover_count += 1
            if "Authentication failed" in message:
                self.state = ProviderState.AUTH_FAILED
                logger.warning(f"Provider {self.name} authentication failed. State: -> AUTH_FAILED")
            else:
                self.consecutive_failures += 1
                if self.consecutive_failures >= self.max_failures and self.state in [ProviderState.HEALTHY, ProviderState.CONFIGURED]:
                    self.state = ProviderState.DEGRADED
                    logger.warning(f"Provider {self.name} exceeded failure threshold. State: -> DEGRADED")
        else:
            # Unrecoverable error doesn't automatically degrade, but we log it
            pass

    def mark_disabled(self):
        self.state = ProviderState.DISABLED

    def print_metrics(self):
        avg_latency = self.total_latency / self.request_count if self.request_count > 0 else 0
        logger.info(f"Metrics for [{self.name}] (State: {self.state}): Requests={self.request_count}, Success={self.success_count}, Failures={self.failure_count}, Failovers={self.failover_count}, AvgLatency={avg_latency:.4f}s")


class LLMFactory:
    def __init__(self):
        self._providers: Dict[str, BaseLLMClient] = {}
        self.health_stats: Dict[str, ProviderHealth] = {}
        
        chain_env = os.getenv("LLM_PROVIDER_CHAIN", "openai,groq,gemini")
        self.provider_chain: List[str] = [p.strip().lower() for p in chain_env.split(",") if p.strip()]
        
        self.max_failures = int(os.getenv("LLM_FAILURE_THRESHOLD", "3"))
        
        # Load default providers dynamically
        self._load_default_providers()
        self._initialize_chain()

    def _load_default_providers(self):
        # Dynamically import to prevent circular dependencies or forced SDK requirements
        try:
            from ai.openai_client import OpenAIClient
            self.register_provider("openai", OpenAIClient())
        except Exception as e:
            logger.warning(f"Could not load OpenAI default provider: {e}")
            
        try:
            from ai.gemini_client import GeminiClient
            self.register_provider("gemini", GeminiClient())
        except Exception as e:
            logger.warning(f"Could not load Gemini default provider: {e}")
            
        try:
            from ai.groq_client import GroqClient
            self.register_provider("groq", GroqClient())
        except Exception as e:
            logger.warning(f"Could not load Groq default provider: {e}")

    def register_provider(self, name: str, client_instance: BaseLLMClient):
        self._providers[name] = client_instance

    def _initialize_chain(self):
        for name in self.provider_chain:
            health = ProviderHealth(name, self.max_failures)
            self.health_stats[name] = health
            
            provider = self._providers.get(name)
            if not provider:
                logger.warning(f"Provider '{name}' in chain is not registered. Marking DISABLED.")
                health.mark_disabled()
                continue
                
            # Offline health check
            if not provider.health_check():
                logger.warning(f"Provider '{name}' failed offline health check (missing config/key). Marking DISABLED.")
                health.mark_disabled()
            else:
                logger.info(f"Provider '{name}' passed offline config check. State: CONFIGURED.")
                    
    def _execute_with_provider(self, name: str, prompt: str, message_id: str) -> str:
        provider = self._providers.get(name)
        stats = self.health_stats.get(name)
        
        if not provider or not stats:
            raise LLMAPIError(f"Provider {name} is not initialized.", name, None, False)
            
        if stats.state == ProviderState.DISABLED:
            raise LLMAPIError(f"Provider {name} is DISABLED (failed startup config).", name, None, True)
            
        start_time = time.time()
        try:
            logger.info(f"Using {name.capitalize()} provider...")
            response = provider.generate_content(prompt, message_id=message_id)
            latency = time.time() - start_time
            stats.record_success(latency)
            logger.info(f"{name.capitalize()} request successful.")
            stats.print_metrics()
            return response
            
        except LLMAPIError as e:
            latency = time.time() - start_time
            logger.warning(f"{name.capitalize()} failed ({e.message}). Provider unavailable for this request.")
            stats.record_failure(latency, e.message, recoverable=e.recoverable)
            stats.print_metrics()
            raise
            
    def generate_content_with_failover(self, prompt: str, message_id: str = "unknown") -> str:
        last_error = None
        
        # Always traverse the chain in order for every request
        for i, provider_name in enumerate(self.provider_chain):
            try:
                return self._execute_with_provider(provider_name, prompt, message_id)
            except LLMAPIError as e:
                last_error = e
                # Check if error is unrecoverable (configuration error, invalid prompt, etc.)
                if not e.recoverable:
                    logger.error(f"Unrecoverable error from {provider_name}. Bypassing failover.")
                    raise
                
                # If there is another provider in the chain, log failover intent
                if i < len(self.provider_chain) - 1:
                    next_provider = self.provider_chain[i + 1]
                    logger.warning(f"Switching to {next_provider.capitalize()}...")
        
        # If we exhausted the chain
        logger.error("All providers in the chain failed.")
        if last_error:
            raise last_error
        else:
            raise LLMAPIError("No valid providers configured in chain.", "factory", None, False)
