"""M2-4 model layer: multi-provider LLM client, encrypted per-user keys, shared translation cache,
processing profiles, optional AI re-ranking and semantic dedup."""
from .client import PROVIDERS, LLMClient, LLMError, ModelConfig

__all__ = ["PROVIDERS", "LLMClient", "LLMError", "ModelConfig"]
