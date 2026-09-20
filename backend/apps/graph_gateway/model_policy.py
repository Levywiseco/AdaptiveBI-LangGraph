"""Provider-specific request policy for the experimental graph gateway."""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from apps.ai_model.model_factory import LLMConfig


ProviderFamily = Literal["openai-compatible", "qwen-model-studio"]


@dataclass(frozen=True)
class GatewayModelPolicy:
    family: ProviderFamily
    request_params: dict


def _provider_family(config: LLMConfig) -> ProviderFamily:
    """Recognize only combinations for which the gateway has explicit behavior."""
    model_name = config.model_name.casefold()
    hostname = (urlsplit(config.api_base_url or "").hostname or "").casefold()
    if model_name.startswith("qwen") and "dashscope" in hostname:
        return "qwen-model-studio"
    return "openai-compatible"


def gateway_model_config(config: LLMConfig) -> tuple[LLMConfig, GatewayModelPolicy]:
    """Apply bounded generation settings without accepting arbitrary provider options."""
    family = _provider_family(config)
    request_params = {
        "timeout": 25.0,
        "max_retries": 0,
        "max_tokens": 2048,
        "streaming": False,
    }
    if family == "qwen-model-studio":
        # The graph expects SQL in message.content. Disable Qwen thinking so
        # non-streaming models do not require a reasoning stream or expose it.
        request_params["extra_body"] = {"enable_thinking": False}
    policy = GatewayModelPolicy(family=family, request_params=request_params)
    return config.model_copy(update={"additional_params": request_params}), policy
