"""Multi-provider LLM interface with streaming support."""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

logger = logging.getLogger("crow_agent.providers")

import httpx



class PermanentProviderError(Exception):
    """Provider error that should NOT be retried (auth, invalid request, etc.)."""


@dataclass
class ChatMessage:
    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_content: str | None = None


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str
    usage: dict[str, int]
    reasoning_content: str | None = None


@dataclass
class ProviderConfig:
    name: str
    api_key: str
    base_url: str
    model: str
    api_type: str = "openai_compat"
    reasoning_variance: str = ""


class BaseProvider(ABC):
    """Abstract provider. Subclass for auth/format quirks."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @abstractmethod
    def _headers(self) -> dict[str, str]:
        ...

    @abstractmethod
    def _payload(self, messages: list[ChatMessage], tools: list[dict] | None, max_tokens: int) -> dict[str, Any]:
        ...

    @abstractmethod
    def _parse(self, raw: dict[str, Any]) -> ChatResponse:
        ...

    def chat(self, messages: list[ChatMessage], tools: list[dict] | None = None, max_tokens: int = 4096) -> ChatResponse:
        resp = httpx.post(
            f"{self.config.base_url}/chat/completions",
            headers=self._headers(),
            json=self._payload(messages, tools, max_tokens),
            timeout=120,
        )
        if resp.status_code in (401, 403):
            raise PermanentProviderError(
                f"{self.config.name}: auth failed ({resp.status_code}) — check API key"
            )
        resp.raise_for_status()
        parsed = resp.json()
        if "choices" not in parsed:
            logger.warning("API response missing 'choices' — body=%s", resp.text[:500])
        return self._parse(parsed)

    async def chat_stream(
        self, messages: list[ChatMessage], tools: list[dict] | None = None, max_tokens: int = 4096
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a chat completion. Yields events:

        - {"type": "content", "text": "..."}       — token
        - {"type": "done", "tool_calls": [...],
                          "usage": {...}}           — final
        """
        payload = self._payload(messages, tools, max_tokens)
        payload["stream"] = True

        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self.config.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=120,
            ) as resp:
                if resp.status_code in (401, 403):
                    raise PermanentProviderError(
                        f"{self.config.name}: auth failed ({resp.status_code}) — check API key"
                    )
                resp.raise_for_status()
                tool_calls_acc: dict[int, dict[str, Any]] = {}
                usage: dict[str, int] = {}
                full_reasoning: str = ""  # ponytail: track reasoning but don't leak to user
                buf = ""

                async for chunk in resp.aiter_bytes():
                    buf += chunk.decode()
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line or line == "data: [DONE]":
                            continue
                        if not line.startswith("data: "):
                            continue
                        try:
                            data = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue

                        choices = data.get("choices", [])
                        if not choices:
                            if "usage" in data:
                                usage = data["usage"]
                            continue

                        delta = choices[0].get("delta", {})
                        finish = choices[0].get("finish_reason")

                        # Some APIs (opencode/deepseek) put content in reasoning_content
                        # ponytail: track reasoning separately, don't leak to user
                        if "content" in delta and delta["content"]:
                            yield {"type": "content", "text": delta["content"]}
                        if "reasoning_content" in delta and delta["reasoning_content"]:
                            full_reasoning += delta["reasoning_content"]

                        # Accumulate tool calls from stream deltas
                        for tc in delta.get("tool_calls", []):
                            idx = tc.get("index", 0)
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": tc.get("id", ""),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            acc = tool_calls_acc[idx]
                            if tc.get("id"):
                                acc["id"] = tc["id"]
                            if tc.get("function"):
                                fn = tc["function"]
                                if "name" in fn:
                                    acc["function"]["name"] += fn["name"]
                                if "arguments" in fn:
                                    acc["function"]["arguments"] += fn["arguments"]

                        if finish:
                            tcs = [tool_calls_acc[i] for i in sorted(tool_calls_acc)] if tool_calls_acc else []
                            merged_usage = usage if usage else {}
                            yield {"type": "done", "tool_calls": tcs, "usage": merged_usage, "reasoning": full_reasoning}
                            return


def _inject_reasoning(payload: dict, config: ProviderConfig) -> None:
    """Inject reasoning/thinking params based on model and reasoning_variance config."""
    model_lower = config.model.lower()
    rv = config.reasoning_variance
    if "deepseek" in model_lower:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = rv
    elif "kimi" in model_lower:
        payload["thinking"] = {"type": "enabled", "keep": "all"}
    elif "mimo" in model_lower:
        payload["chat_template_kwargs"] = {"enable_thinking": True}


class OpenAICompatibleProvider(BaseProvider):
    """Standard OpenAI-compatible /v1/chat/completions endpoint."""

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, messages: list[ChatMessage], tools: list[dict] | None, max_tokens: int) -> dict[str, Any]:
        msgs: list[dict[str, Any]] = []
        for m in messages:
            entry: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_call_id is not None:
                entry["tool_call_id"] = m.tool_call_id
            if m.tool_calls is not None:
                entry["tool_calls"] = m.tool_calls
            if m.reasoning_content is not None:
                entry["reasoning_content"] = m.reasoning_content
            msgs.append(entry)

        p: dict[str, Any] = {
            "model": self.config.model,
            "messages": msgs,
            "max_tokens": max_tokens,
        }
        if tools:
            p["tools"] = tools
        if self.config.reasoning_variance:
            _inject_reasoning(p, self.config)
        return p

    def _parse(self, raw: dict[str, Any]) -> ChatResponse:
        choice = raw["choices"][0]["message"]
        content = choice.get("content", "") or ""
        # ponytail: reasoning_content is exposed separately via ChatResponse.reasoning_content.
        # Don't merge it into content — that leaks model inner monologue to the user.
        # If both are empty, content stays empty. Downstream code (turn_finalizer) handles
        # the no-content case by retrying.
        return ChatResponse(
            content=content,
            tool_calls=choice.get("tool_calls", []),
            finish_reason=raw["choices"][0].get("finish_reason", "stop"),
            usage=raw.get("usage", {}),
            reasoning_content=choice.get("reasoning_content"),
        )



# ── failover provider (sequential chain) ──


_DSML_OPEN = "<\uff5c\uff5cDSML\uff5c\uff5c"
_DSML_CLOSE = "\uff5c\uff5cDSML\uff5c\uff5c"


def _normalize_tool_call_text(content: str) -> str:
    """Strip DeepSeek DSML wrappers so standard XML parsers can match."""
    if "DSML" not in content:
        return content
    import re as _re
    # Closing tags first — otherwise </｜｜DSML｜｜x> becomes </<x>
    content = _re.sub(r"</" + _DSML_CLOSE, "</", content)
    content = _re.sub(_DSML_OPEN, "<", content)
    return content


def text_may_contain_tool_calls(content: str | None) -> bool:
    """True when model text might embed XML/DSML tool calls instead of native tool_calls."""
    if not content:
        return False
    markers = (
        "<tool_call", "<invoke", "<function=", "<tool_name>",
        "<param name=", _DSML_OPEN, "DSML",
    )
    return any(m in content for m in markers)


def _parse_xml(content: str) -> tuple[str, list[dict[str, Any]]]:
    import re as _re
    import json as _j

    content = _normalize_tool_call_text(content)
    tcs: list[dict[str, Any]] = []

    # Format 1: <invoke name="func"><parameter name="x">val</parameter></invoke>
    for m in _re.finditer(r'<invoke\s+name="(\w+)"[^>]*>(.*?)</invoke>', content, _re.DOTALL):
        fn = m.group(1)
        params = {}
        for pm in _re.finditer(r'<parameter\s+name="(\w+)"[^>]*>(.*?)</parameter>', m.group(2), _re.DOTALL):
            val = pm.group(2).strip()
            if val in ("true", "false"):
                val = val == "true"
            params[pm.group(1)] = val
        tcs.append({"id": f"xmlcall_{len(tcs)}", "type": "function", "function": {"name": fn, "arguments": _j.dumps(params)}})

    # Format 2: <function=name> with parameters in either old or new format
    for m in _re.finditer(r'<function=(\w+)>\s*(.*?)\s*</function>', content, _re.DOTALL):
        fn = m.group(1)
        inner = m.group(2)
        params = {}
        for pm in _re.finditer(r'<parameter\s+name="(\w+)"[^>]*>(.*?)</parameter>', inner, _re.DOTALL):
            params[pm.group(1)] = pm.group(2).strip()
        if not params:
            for pm in _re.finditer(r'<parameter=(\w+)>(.*?)</parameter>', inner, _re.DOTALL):
                params[pm.group(1)] = pm.group(2).strip()
        tcs.append({"id": f"xmlcall_{len(tcs)}", "type": "function", "function": {"name": fn, "arguments": _j.dumps(params)}})

    # Format 3: <tool_call><tool_name>fn</tool_name><param name="k">v</param></tool_call>
    for m in _re.finditer(r'<tool_call>\s*(.*?)\s*</tool_call>', content, _re.DOTALL):
        inner = m.group(1)
        name_m = _re.search(r'<tool_name>\s*(.*?)\s*</tool_name>', inner, _re.DOTALL)
        if not name_m:
            continue
        fn = name_m.group(1).strip()
        params = {}
        for pm in _re.finditer(r'<param\s+name="(\w+)"[^>]*>(.*?)</param>', inner, _re.DOTALL):
            val = pm.group(2).strip()
            if val in ("true", "false"):
                val = val == "true"
            params[pm.group(1)] = val
        tcs.append({"id": f"xmlcall_{len(tcs)}", "type": "function", "function": {"name": fn, "arguments": _j.dumps(params)}})

    cleaned = content
    for pattern in (
        r'<tool_calls?>.*?</tool_calls?>',
        r'<invoke\s+name=[^>]*>.*?</invoke>',
        r'<function=\w+>.*?</function>',
        r'<tool_call>.*?</tool_call>',
    ):
        cleaned = _re.sub(pattern, "", cleaned, flags=_re.DOTALL).strip()
    if tcs:
        logger.info("Parsed %d XML tool calls from text response", len(tcs))
    return cleaned, tcs


def normalize_model_text_tools(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Parse embedded tool calls from model text and merge with native tool_calls."""
    text = content or ""
    merged = list(tool_calls or [])
    if not text_may_contain_tool_calls(text):
        return text, merged
    cleaned, parsed = _parse_xml(text)
    if parsed:
        merged.extend(parsed)
        text = cleaned
    return text, merged


# --- Concrete provider factories ---

PROVIDER_REGISTRY: dict[str, type[BaseProvider]] = {
    "openai_compat": OpenAICompatibleProvider,
}


class FallbackProvider:
    """ponytail: primary + 1 fallback. Retries on 500/502/503/timeout."""

    def __init__(self, primary: BaseProvider, fallback: BaseProvider) -> None:
        self._primary = primary
        self._fallback = fallback
        self.config = primary.config

    def chat(self, messages, tools=None, max_tokens=4096):
        try:
            return self._primary.chat(messages, tools, max_tokens)
        except PermanentProviderError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403, 400):
                raise
            logger.warning("Primary %s HTTP %s — falling back", self._primary.config.name, e.response.status_code)
        except httpx.TimeoutException:
            logger.warning("Primary %s timeout — falling back", self._primary.config.name)
        except Exception:
            logger.warning("Primary %s unexpected error — falling back", self._primary.config.name, exc_info=True)
        return self._fallback.chat(messages, tools, max_tokens)

    async def chat_stream(self, messages, tools=None, max_tokens=4096):
        try:
            async for event in self._primary.chat_stream(messages, tools, max_tokens):
                yield event
            return
        except PermanentProviderError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403, 400):
                raise
            logger.warning("Primary %s stream HTTP %s — falling back", self._primary.config.name, e.response.status_code)
        except httpx.TimeoutException:
            logger.warning("Primary %s stream timeout — falling back", self._primary.config.name)
        except Exception:
            logger.warning("Primary %s stream unexpected error — falling back", self._primary.config.name, exc_info=True)
        async for event in self._fallback.chat_stream(messages, tools, max_tokens):
            yield event


def resolve_provider(
    name: str,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    provider_manager: "ProviderManager | None" = None,
    fallback_name: str | None = None,
    fallback_model: str | None = None,
) -> BaseProvider:
    env_prefix = name.upper().replace("-", "_")

    store_entry = None
    if provider_manager is not None:
        store_entry = provider_manager.get(name)

    resolved_key = api_key or (store_entry.api_key if store_entry else None) or os.environ.get(f"{env_prefix}_API_KEY", "")
    resolved_url = base_url or (store_entry.base_url if store_entry else None) or os.environ.get(f"{env_prefix}_BASE_URL", "")
    resolved_model = model or (store_entry.model if store_entry else None) or os.environ.get(f"{env_prefix}_MODEL", "")

    if not resolved_key:
        raise ValueError(f"No API key for provider '{name}'. Add it via UI or set {env_prefix}_API_KEY.")
    if not resolved_url:
        raise ValueError(f"No base URL for provider '{name}'. Add it via UI or set {env_prefix}_BASE_URL.")
    if not resolved_model:
        raise ValueError(f"No model for provider '{name}'. Add it via UI or set {env_prefix}_MODEL.")

    api_type = (store_entry.api_type if store_entry else None) or "openai_compat"
    reasoning_variance = (store_entry.reasoning_variance if store_entry else "") or ""
    config = ProviderConfig(
        name=name,
        api_key=resolved_key,
        base_url=resolved_url.rstrip("/"),
        model=resolved_model,
        api_type=api_type,
        reasoning_variance=reasoning_variance,
    )
    cls = PROVIDER_REGISTRY.get(api_type, OpenAICompatibleProvider)
    primary = cls(config)

    # ponytail: wrap with fallback if backup provider configured
    if fallback_name and provider_manager:
        fallback = resolve_provider(
            fallback_name,
            model=fallback_model,
            provider_manager=provider_manager,
        )
        return FallbackProvider(primary, fallback)

    return primary