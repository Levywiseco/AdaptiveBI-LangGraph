from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, ValidationError

from app.contracts import ModelCallError, ModelUsage
from app.planning import MetricCandidate, MetricQueryPlan


class CompiledMetric(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metric_id: int = Field(gt=0)
    metric_code: str = Field(min_length=1, max_length=128)
    metric_name: str = Field(min_length=1, max_length=255)
    metric_version_id: int = Field(gt=0)
    metric_version: int = Field(gt=0)
    dimensions: list[str] = Field(max_length=20)
    time_range: dict[str, str] | None = None
    sql_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler: str = Field(min_length=1, max_length=64)


class BusinessGateway(BaseModel):
    """Narrow client for metric planning; credentials and request data never serialize."""

    gateway_url: str
    service_token: SecretStr = Field(exclude=True)
    delegation: SecretStr = Field(exclude=True)
    request_body: dict = Field(exclude=True)
    _usage: ModelUsage = PrivateAttr(default_factory=ModelUsage)
    _calls: int | None = PrivateAttr(default=0)

    @property
    def usage(self) -> ModelUsage:
        return self._usage

    @property
    def calls(self) -> int | None:
        return self._calls

    def _request(self, endpoint: str, extra: dict[str, Any] | None = None) -> dict:
        body = {**self.request_body, **(extra or {})}
        try:
            with httpx.Client(
                timeout=3 if endpoint.endswith(("/authorize", "/candidates")) else 35,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.post(
                    self.gateway_url.rstrip("/") + endpoint,
                    json=body,
                    headers={
                        "X-Graph-Service": self.service_token.get_secret_value(),
                        "X-Graph-Delegation": self.delegation.get_secret_value(),
                    },
                )
            if response.status_code in (401, 403, 404):
                raise ModelCallError("gateway_rejected")
            if response.status_code == 422 and endpoint.endswith("/compile"):
                raise ModelCallError("metric_compile_failed")
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and {"code", "data", "msg"} <= payload.keys():
                if payload["code"] != 0:
                    raise ModelCallError("gateway_rejected")
                payload = payload["data"]
            if not isinstance(payload, dict):
                raise ModelCallError("gateway_unavailable")
            return payload
        except ModelCallError:
            raise
        except httpx.TimeoutException:
            code = "model_timeout" if endpoint.endswith("/model") else "gateway_unavailable"
            raise ModelCallError(code) from None
        except Exception:
            raise ModelCallError("gateway_unavailable") from None

    def authorize(self) -> None:
        if self._request("/internal/graph/metrics/authorize").get("authorized") is not True:
            raise ModelCallError("gateway_rejected")

    def candidates(self) -> list[MetricCandidate]:
        payload = self._request("/internal/graph/metrics/candidates")
        raw = payload.get("candidates")
        if not isinstance(raw, list) or len(raw) > 20:
            raise ModelCallError("gateway_unavailable")
        try:
            return [MetricCandidate.model_validate(item) for item in raw]
        except ValidationError:
            raise ModelCallError("gateway_unavailable") from None

    def plan(self, candidates: list[MetricCandidate]) -> str:
        refs = [
            {"metric_id": item.metric_id, "metric_version_id": item.metric_version_id}
            for item in candidates
        ]
        self._calls = None
        payload = self._request("/internal/graph/metrics/model", {"candidates": refs})
        try:
            self._usage = ModelUsage.model_validate(payload.get("usage") or {})
            calls = payload.get("model_calls")
            if calls is not None and (type(calls) is not int or calls < 0):
                raise ValueError("invalid_calls")
            self._calls = calls
        except (ValidationError, ValueError):
            raise ModelCallError("model_output_invalid") from None
        if payload.get("error"):
            code = payload["error"]
            allowed = {"model_timeout", "model_call_failed", "model_output_invalid"}
            raise ModelCallError(code if code in allowed else "model_call_failed")
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 16000:
            raise ModelCallError("model_output_invalid")
        return content

    def compile(self, plan: MetricQueryPlan) -> dict:
        payload = self._request(
            "/internal/graph/metrics/compile",
            {"plan": plan.model_dump(mode="json")},
        )
        try:
            compiled = CompiledMetric.model_validate(payload)
        except ValidationError:
            raise ModelCallError("metric_compile_failed") from None
        if (
            compiled.metric_id != plan.metric_id
            or compiled.metric_version_id != plan.metric_version_id
            or compiled.dimensions != plan.dimensions
        ):
            raise ModelCallError("metric_compile_failed")
        return compiled.model_dump(mode="json")
