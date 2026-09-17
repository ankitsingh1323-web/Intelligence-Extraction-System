"""OpenAI-compatible client for on-premise Qwen / Kimi2 deployments.

Both models are expected to be served behind an OpenAI-compatible
/v1/chat/completions endpoint (vLLM, TGI, Ollama's OpenAI shim, LM Studio,
etc.). No calls ever leave the local network — base_url points at the
on-prem host and everything works with the API keys the user already has.
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI, APIError, APITimeoutError

from app.config import get_settings

logger = logging.getLogger(__name__)

# Hard ceiling on how far complete()'s automatic budget-doubling (see
# LLMReasoningBudgetExceededError) will raise max_tokens for one call, so
# a backend that never actually finishes reasoning can't be retried into
# an enormous, slow, expensive request.
_MAX_TOKENS_CEILING = 16384


class LLMJSONParseError(Exception):
    """The model's response couldn't be parsed as JSON even after the
    markdown-fence-stripping fallback. Raised rather than silently
    returning {} so this failure mode is distinguishable from the model
    legitimately reporting "nothing found" -- callers already catch and
    log extraction/synthesis failures by step and source file; swallowing
    it here instead would erase that attribution and look identical to a
    real empty result."""


class LLMReasoningBudgetExceededError(Exception):
    """A reasoning/chain-of-thought-capable backend (seen in practice with
    Kimi-K2 variants served over vLLM) can spend its entire max_tokens
    budget on an internal reasoning trace -- a separate field from the
    actual answer -- and hit the token cap before writing any real
    content. The response still comes back as a normal 200 OK with
    finish_reason="length" and an empty message.content, which used to
    surface many calls downstream as a bare, unhelpful
    `json.JSONDecodeError: Expecting value: line 1 column 1 (char 0)` with
    no indication of the real cause. complete() below already retries
    once with a doubled budget before raising this -- reaching this error
    means even that wasn't enough."""


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


class LLMClient:
    """Thin wrapper that dispatches to Qwen or Kimi2 by logical role."""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._clients: dict[str, AsyncOpenAI] = {
            "qwen": AsyncOpenAI(
                base_url=settings.qwen_base_url,
                api_key=settings.qwen_api_key,
                timeout=settings.llm_request_timeout_s,
            ),
            "kimi": AsyncOpenAI(
                base_url=settings.kimi_base_url,
                api_key=settings.kimi_api_key,
                timeout=settings.llm_request_timeout_s,
            ),
        }
        self._model_names = {
            "qwen": settings.qwen_model_name,
            "kimi": settings.kimi_model_name,
        }
        self._role_backend = {
            "extraction": settings.role_extraction,
            "synthesis": settings.role_synthesis,
            "chat": settings.role_chat,
            "translation": settings.role_translation,
        }

    def backend_for_role(self, role: str) -> str:
        return self._role_backend.get(role, "qwen")

    def roles_using(self, backend: str) -> list[str]:
        return [role for role, b in self._role_backend.items() if b == backend]

    async def check_reachable(self, backend: str, timeout_s: float = 8.0) -> tuple[bool, str | None]:
        """Fast connectivity probe -- a minimal chat completion (max_tokens=1),
        not GET /v1/models. Some on-prem OpenAI-compatible servers (seen in
        practice with Kimi2) implement /v1/chat/completions but not the
        /v1/models listing endpoint, returning a clean 404 for it -- that
        used to make this check report the backend as unreachable and skip
        synthesis/chat even though completions worked fine. A tiny real
        completion tests the actual capability every such server implements,
        so it can't be fooled by an unrelated endpoint being absent.

        Used as a preflight so a dead endpoint is reported in seconds instead
        of discovered only after several minutes of doomed retries deep
        inside file processing.
        """
        client = self._clients[backend]
        model = self._model_names[backend]
        base_url = self._settings.qwen_base_url if backend == "qwen" else self._settings.kimi_base_url
        try:
            await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                ),
                timeout=timeout_s,
            )
            return True, None
        except Exception as exc:  # noqa: BLE001 - any failure means "unreachable" for our purposes
            detail = f"Cannot reach {backend} model endpoint at {base_url} ({exc.__class__.__name__}: {exc})"
            logger.warning(detail)
            return False, detail

    async def check_all_backends(self, timeout_s: float = 8.0) -> dict[str, tuple[bool, str | None]]:
        backends = ("qwen", "kimi")
        results = await asyncio.gather(*(self.check_reachable(b, timeout_s) for b in backends))
        return dict(zip(backends, results))

    async def complete(
        self,
        role: str,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        backend_override: str | None = None,
    ) -> LLMResponse:
        """backend_override: bypass the role -> backend mapping and force a
        specific physical backend for this one call -- used by chat's
        automatic failover (see graphrag.py) to answer from Qwen when the
        chat role's configured backend (normally Kimi2) is unreachable,
        without needing a config change + restart to recover from an
        outage."""
        backend = backend_override or self.backend_for_role(role)
        client = self._clients[backend]
        model = self._model_names[backend]

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        for attempt in range(1, self._settings.llm_max_retries + 1):
            start = asyncio.get_event_loop().time()
            try:
                resp = await client.chat.completions.create(**kwargs)
                elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)
                choice_data = resp.choices[0]
                choice = choice_data.message.content or ""

                # See LLMReasoningBudgetExceededError -- a reasoning-capable
                # backend can spend the whole max_tokens budget on its own
                # chain-of-thought and hit the cap (finish_reason="length")
                # before writing any real content, which is NOT the same
                # thing as the model legitimately having nothing to say
                # (that comes back as finish_reason="stop" with real, if
                # short, content). Self-heal by doubling the budget and
                # retrying immediately -- reuses this same attempt loop --
                # instead of making every deployment hand-tune max_tokens
                # per model's reasoning verbosity.
                if not choice.strip() and choice_data.finish_reason == "length":
                    if kwargs["max_tokens"] < _MAX_TOKENS_CEILING:
                        new_budget = min(kwargs["max_tokens"] * 2, _MAX_TOKENS_CEILING)
                        logger.warning(
                            "%s returned empty content with finish_reason=length at max_tokens=%s "
                            "(likely spent on internal reasoning) -- retrying once with max_tokens=%s",
                            backend, kwargs["max_tokens"], new_budget,
                        )
                        kwargs["max_tokens"] = new_budget
                        continue
                    raise LLMReasoningBudgetExceededError(
                        f"{backend} (model '{model}') returned no content after exhausting "
                        f"max_tokens={kwargs['max_tokens']} (finish_reason=length), even after doubling "
                        f"the budget once -- this backend appears to spend an unusually large token "
                        f"budget on internal reasoning before answering."
                    )

                usage = resp.usage
                return LLMResponse(
                    text=choice,
                    model=model,
                    prompt_tokens=usage.prompt_tokens if usage else 0,
                    completion_tokens=usage.completion_tokens if usage else 0,
                    latency_ms=elapsed_ms,
                )
            except (APIError, APITimeoutError) as exc:
                last_err = exc
                logger.warning("LLM call failed (attempt %s/%s) on %s: %s",
                                attempt, self._settings.llm_max_retries, backend, exc)
                await asyncio.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"LLM backend '{backend}' failed after retries: {last_err}")

    async def complete_json(self, role: str, system: str, user: str, **kwargs) -> dict:
        """Convenience wrapper: force JSON mode and parse the result robustly."""
        resp = await self.complete(role, system, user, json_mode=True, **kwargs)
        return _safe_json_parse(resp.text)


def _safe_json_parse(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Some local models wrap JSON in markdown fences despite json_mode.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise LLMJSONParseError(text[:500])


_client_singleton: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = LLMClient()
    return _client_singleton
