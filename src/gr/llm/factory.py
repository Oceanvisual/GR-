"""The LLM client of the service: OpenAI-compatible, with the response cache if enabled."""

from gr.llm.base import StructuredLLM
from gr.llm.cache import CachedLLM
from gr.llm.openai_compat import OpenAICompatibleLLM
from gr.settings import Settings
from gr.storage.repositories import Repositories


def build_llm(settings: Settings, repos: Repositories) -> StructuredLLM:
    client = OpenAICompatibleLLM(settings, repos.llm_calls)
    if not settings.llm_cache_enabled:
        return client
    return CachedLLM(client, repos.llm_cache, client.model_for, client.options_for)
