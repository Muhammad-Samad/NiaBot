import os
import time
import threading
from typing import Dict, Any, Tuple
from utils.logger import get_logger

logger = get_logger(__name__)

class RateLimiterService:
    def __init__(self):
        self.enabled = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
        try:
            self.capacity = float(os.getenv("RATE_LIMIT_CAPACITY", "5"))
            self.refill_rate = float(os.getenv("RATE_LIMIT_REFILL_RATE", "0.1")) # tokens per second
        except ValueError:
            logger.error("Invalid RATE_LIMIT config. Using defaults capacity=5, refill=0.1")
            self.capacity = 5.0
            self.refill_rate = 0.1
            
        self.rejection_message = os.getenv(
            "RATE_LIMIT_MESSAGE", 
            "You are sending messages too quickly. Please wait a moment before sending another message."
        )
        
        # Structure: { session_id: {"tokens": float, "last_refill": float} }
        self._buckets: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, session_id: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Check if the request is allowed according to the Token Bucket algorithm.
        Returns a tuple: (is_allowed, metadata_dict)
        Fail-safe: If this method throws an exception, it should be caught by the caller.
        """
        base_metadata = {
            "bucket_capacity": self.capacity,
            "refill_rate": self.refill_rate,
            "limiter_key_type": "session_id",
            "limiter_key": session_id,
            "limiter_store": "memory",
            "execution_stage": "rate_limit_check"
        }
        
        if not self.enabled:
            base_metadata["remaining_tokens"] = self.capacity
            base_metadata["retry_after_seconds"] = 0.0
            return True, base_metadata
            
        if not session_id:
            base_metadata["remaining_tokens"] = self.capacity
            base_metadata["retry_after_seconds"] = 0.0
            return True, base_metadata
            
        now = time.time()
        
        with self._lock:
            bucket = self._buckets.get(session_id)
            
            if bucket is None:
                # First time seeing this session, create a full bucket
                self._buckets[session_id] = {
                    "tokens": self.capacity - 1.0, # consume 1 token immediately
                    "last_refill": now
                }
                base_metadata["remaining_tokens"] = self.capacity - 1.0
                base_metadata["retry_after_seconds"] = 0.0
                return True, base_metadata
                
            # Refill tokens based on time elapsed
            time_passed = now - bucket["last_refill"]
            tokens_to_add = time_passed * self.refill_rate
            
            new_token_count = min(self.capacity, bucket["tokens"] + tokens_to_add)
            
            if new_token_count >= 1.0:
                # Allowed, consume 1 token
                bucket["tokens"] = new_token_count - 1.0
                bucket["last_refill"] = now
                base_metadata["remaining_tokens"] = bucket["tokens"]
                base_metadata["retry_after_seconds"] = 0.0
                return True, base_metadata
            else:
                # Rate limited, do not consume token, but update last_refill to current time
                bucket["tokens"] = new_token_count
                bucket["last_refill"] = now
                
                # Calculate retry_after_seconds
                tokens_needed = 1.0 - new_token_count
                retry_after = tokens_needed / self.refill_rate if self.refill_rate > 0 else 0.0
                
                base_metadata["remaining_tokens"] = new_token_count
                base_metadata["retry_after_seconds"] = round(retry_after, 2)
                return False, base_metadata
