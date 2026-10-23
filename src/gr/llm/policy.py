"""Data transfer policy: documents never reach an external host unless enabled (R-09)."""

import ipaddress
from urllib.parse import urlsplit

from gr.llm.base import ExternalLLMNotAllowedError, LLMNotConfiguredError
from gr.settings import Settings


def is_local_host(host: str, allowlist: list[str]) -> bool:
    """Loopback or private IP literal, or a name listed in LLM_LOCAL_HOSTS.

    Names are never resolved: DNS can change between the check and the request, and
    `127.0.0.1.attacker.example` must not pass.
    """
    host = host.strip().lower().rstrip(".")
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host in {name.strip().lower().rstrip(".") for name in allowlist}
    return address.is_loopback or address.is_private


def check_data_policy(settings: Settings) -> None:
    """Raise unless analysis may send document text to LLM_BASE_URL."""
    if settings.llm_data_policy == "disabled" or settings.llm_base_url is None:
        raise LLMNotConfiguredError()
    if settings.llm_data_policy == "external_allowed":
        return
    host = urlsplit(str(settings.llm_base_url)).hostname or ""
    if not is_local_host(host, settings.llm_local_hosts):
        raise ExternalLLMNotAllowedError(
            f"LLM_DATA_POLICY=local_only, а хост «{host}» внешний. Добавьте его в "
            "LLM_LOCAL_HOSTS или включите external_allowed."
        )
