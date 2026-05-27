"""Provider adapters for OpenAI-compatible chat completions."""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Callable, Iterable

from openai import OpenAI

from .. import config
from .utils import _get, _usage_to_dict


TextDeltaCallback = Callable[[str], None]
ChunkCallback = Callable[[], None]


_client: OpenAI | None = None
_client_config: tuple[str | None, str | None, float, int] | None = None
_client_lock = Lock()


def get_client() -> OpenAI:
    global _client, _client_config
    client_config = _current_client_config()
    if _client is None or _client_config != client_config:
        with _client_lock:
            client_config = _current_client_config()
            if _client is None or _client_config != client_config:
                _client = OpenAI(
                    api_key=client_config[0],
                    base_url=client_config[1],
                    timeout=client_config[2],
                    max_retries=client_config[3],
                )
                _client_config = client_config
    return _client


def reset_client() -> None:
    global _client, _client_config
    _client = None
    _client_config = None


def _current_client_config() -> tuple[str | None, str | None, float, int]:
    return (config.API_KEY, config.BASE_URL, 300.0, 2)


def current_adapter() -> "ProviderAdapter":
    return ProviderAdapter(
        name=resolve_provider_name(
            provider=config.PROVIDER,
            base_url=config.BASE_URL,
            model=config.MODEL,
        )
    )


def resolve_provider_name(*, provider: str | None, base_url: str | None, model: str | None) -> str:
    requested = (provider or "auto").strip().lower()
    if requested in {"openai-compatible", "compatible"}:
        return "openai-compatible"
    if requested in {"openai", "deepseek"}:
        return requested
    if requested != "auto":
        raise ValueError(f"Unsupported HARNESS_PROVIDER: {provider}")

    signature = f"{base_url or ''} {model or ''}".lower()
    if "deepseek" in signature:
        return "deepseek"
    if "api.openai.com" in (base_url or "").lower():
        return "openai"
    return "openai-compatible"


@dataclass(frozen=True)
class StreamResult:
    assistant_message: dict
    finish_reason: str | None
    usage: dict | None = None


@dataclass(frozen=True)
class ProviderAdapter:
    name: str

    def chat_kwargs(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        stream: bool = False,
        prompt_cache_key: str | None = None,
        stream_options: dict | None = None,
    ) -> dict:
        kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools is not None:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if stream:
            kwargs["stream"] = True
        if prompt_cache_key:
            kwargs["prompt_cache_key"] = prompt_cache_key
        if stream_options:
            kwargs["stream_options"] = stream_options
        return kwargs

    def assistant_message_from_response(self, msg) -> dict:
        assistant_msg = {"role": "assistant", "content": _get(msg, "content")}
        reasoning_content = _reasoning_content_from(msg)
        if self.requires_reasoning_content_roundtrip and reasoning_content is not None:
            assistant_msg["reasoning_content"] = reasoning_content

        tool_calls = [_normalize_tool_call(tc) for tc in (_get(msg, "tool_calls") or [])]
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        return assistant_msg

    def assistant_message_from_stream(
        self,
        chunks: Iterable,
        *,
        on_text_delta: TextDeltaCallback | None = None,
        on_chunk: ChunkCallback | None = None,
        on_reasoning_start: Callable[[], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        cancellation_token=None,
    ) -> StreamResult:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_by_index: dict[int, dict] = {}
        finish_reason: str | None = None
        usage: dict | None = None

        for chunk in chunks:
            _check_cancelled(cancellation_token)
            if on_chunk is not None:
                on_chunk()
            _check_cancelled(cancellation_token)
            chunk_usage = _usage_to_dict(_get(chunk, "usage"))
            if chunk_usage is not None:
                usage = chunk_usage
            choices = _get(chunk, "choices") or []
            if not choices:
                continue
            choice = choices[0]
            finish = _get(choice, "finish_reason")
            if finish is not None:
                finish_reason = finish
            delta = _get(choice, "delta") or {}

            text_delta = _get(delta, "content")
            if text_delta:
                content_parts.append(text_delta)
                if on_text_delta is not None:
                    on_text_delta(text_delta)

            reasoning_delta = _reasoning_content_from(delta)
            if reasoning_delta:
                if not reasoning_parts and on_reasoning_start is not None:
                    on_reasoning_start()
                reasoning_parts.append(reasoning_delta)
                if on_reasoning_delta is not None:
                    on_reasoning_delta(reasoning_delta)

            for tc_delta in _get(delta, "tool_calls") or []:
                index = _get(tc_delta, "index")
                if index is None:
                    index = len(tool_calls_by_index)
                entry = tool_calls_by_index.setdefault(
                    int(index),
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                call_id = _get(tc_delta, "id")
                if call_id:
                    entry["id"] = call_id
                call_type = _get(tc_delta, "type")
                if call_type:
                    entry["type"] = call_type
                fn_delta = _get(tc_delta, "function") or {}
                name_delta = _get(fn_delta, "name")
                if name_delta:
                    entry["function"]["name"] += name_delta
                args_delta = _get(fn_delta, "arguments")
                if args_delta:
                    entry["function"]["arguments"] += args_delta

        assistant_msg = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if self.requires_reasoning_content_roundtrip and reasoning_parts:
            assistant_msg["reasoning_content"] = "".join(reasoning_parts)
        tool_calls = [
            call
            for _, call in sorted(tool_calls_by_index.items())
            if call.get("id") or call["function"].get("name") or call["function"].get("arguments")
        ]
        if tool_calls:
            for idx, call in enumerate(tool_calls):
                if not call.get("id"):
                    call["id"] = f"call_{idx}"
            assistant_msg["tool_calls"] = tool_calls
        return StreamResult(assistant_message=assistant_msg, finish_reason=finish_reason, usage=usage)

    @property
    def requires_reasoning_content_roundtrip(self) -> bool:
        return self.name == "deepseek"

    @property
    def supports_prompt_cache_key(self) -> bool:
        return self.name == "openai"


def _check_cancelled(cancellation_token) -> None:
    if cancellation_token is not None and getattr(cancellation_token, "is_cancelled", False):
        from .cancellation import CancelledError
        raise CancelledError("Turn cancelled by user")


def _normalize_tool_call(tc) -> dict:
    fn = _get(tc, "function") or {}
    return {
        "id": _get(tc, "id"),
        "type": _get(tc, "type") or "function",
        "function": {
            "name": _get(fn, "name"),
            "arguments": _get(fn, "arguments") or "",
        },
    }


def _reasoning_content_from(value) -> str | None:
    reasoning_content = _get(value, "reasoning_content")
    if reasoning_content is None:
        reasoning_content = _get(_get(value, "model_extra") or {}, "reasoning_content")
    return reasoning_content


