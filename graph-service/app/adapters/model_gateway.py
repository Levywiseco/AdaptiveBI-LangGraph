from typing import Any

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr, SecretStr

from app.contracts import ModelCallError, ModelUsage


class GatewayChatModel(BaseChatModel):
    gateway_url: str
    service_token: SecretStr = Field(exclude=True)
    delegation: SecretStr = Field(exclude=True)
    request_body: dict = Field(exclude=True)
    _usage: ModelUsage = PrivateAttr(default_factory=ModelUsage)
    _calls: int | None = PrivateAttr(default=0)

    @property
    def _llm_type(self):
        return "adaptive-gateway"

    @property
    def usage(self):
        return self._usage

    @property
    def calls(self):
        return self._calls

    def request(self, endpoint):
        try:
            with httpx.Client(timeout=3 if endpoint.endswith("/authorize") else 35,
                              follow_redirects=False, trust_env=False) as client:
                response = client.post(self.gateway_url.rstrip("/") + endpoint,
                                       json=self.request_body,
                                       headers={"X-Graph-Service": self.service_token.get_secret_value(),
                                                "X-Graph-Delegation": self.delegation.get_secret_value()})
            if response.status_code in (401, 403, 404):
                raise ModelCallError("gateway_rejected")
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and {"code", "data", "msg"} <= payload.keys():
                if payload["code"] != 0:
                    raise ModelCallError("gateway_rejected")
                payload = payload["data"]
            if not isinstance(payload, dict):
                raise ModelCallError("model_output_invalid")
            return payload
        except ModelCallError:
            raise
        except httpx.TimeoutException:
            raise ModelCallError("model_timeout") from None
        except Exception:
            raise ModelCallError("gateway_unavailable") from None

    def authorize(self):
        if self.request("/internal/graph/authorize").get("authorized") is not True:
            raise ModelCallError("gateway_rejected")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        # The gateway reconstructs its trusted prompt; no arbitrary role/messages
        # or model settings are forwarded to the credential-owning service.
        self._calls = None  # Request outcome is unknown until a reply arrives.
        payload = self.request("/internal/graph/model")
        try:
            self._usage = ModelUsage.model_validate(payload.get("usage") or {})
            calls = payload.get("model_calls")
            if calls is not None and (type(calls) is not int or calls < 0):
                raise ValueError("invalid_calls")
            self._calls = calls
        except ValueError:
            raise ModelCallError("model_output_invalid") from None
        if payload.get("error"):
            code = payload["error"]
            raise ModelCallError(code if code in {"model_timeout", "model_call_failed", "model_output_invalid"}
                                 else "model_call_failed")
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 16000:
            raise ModelCallError("model_output_invalid")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])
