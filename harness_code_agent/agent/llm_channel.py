"""LLM request channel: streaming and non-streaming calls with timing events.

Separated from AgentConversation so request mechanics (retries, stream
fallback, thought events) live apart from conversation orchestration.
"""
from __future__ import annotations

import logging
import random
import time

from .. import config
from .cancellation import CancelledError
from .providers import ProviderAdapter, get_client, reset_client
from .utils import _usage_to_dict

log = logging.getLogger("harness")


def llm_call_simple(messages: list[dict]) -> str:
    """Simple LLM call without tools — used for summarization.
    Retries on rate limits to avoid crashing the agent during context compaction."""
    profile = config.resolve_model_profile("fast")
    adapter = ProviderAdapter(profile.provider)
    for attempt in range(4):
        try:
            resp = get_client().chat.completions.create(**adapter.chat_kwargs(
                profile=profile,
                messages=messages,
                max_tokens=10000,
            ))
            return resp.choices[0].message.content or ""
        except Exception as e:
            err_str = str(e)
            if ("rate_limit" in err_str.lower() or "429" in err_str) and attempt < 3:
                wait = min(2 ** (attempt + 1), 30) + random.uniform(0, 3)
                log.warning(f"llm_call_simple rate limited, waiting {wait:.1f}s (attempt {attempt+1}/4)")
                time.sleep(wait)
                continue
            log.error(f"llm_call_simple failed: {e}")
            # Return a minimal summary rather than crashing
            return "[context summarization failed — continuing with truncated context]"
    return "[context summarization failed after retries]"


class LlmChannel:
    """Performs one assistant request (streaming when the agent asks for it)."""

    def __init__(self, conversation):
        # The channel reads provider/client/trace/stream state through the
        # conversation it serves; it owns no state of its own.
        self.conversation = conversation

    def request_assistant_message(self, kwargs: dict, cancellation_token=None) -> tuple[dict, str | None] | None:
        conv = self.conversation
        if getattr(conv, "_client_needs_refresh", False):
            conv.client = get_client()
            conv._client_needs_refresh = False
        if conv.agent.stream_callback is not None:
            stream_kwargs = dict(kwargs)
            stream_kwargs["stream"] = True
            call_id = conv.next_call_id()
            request_started = time.perf_counter()
            first_token_ms: int | None = None
            conv.emitter.emit_llm_request_started(call_id, streamed=True, model=str(stream_kwargs.get("model") or config.MODEL))
            saw_chunk = False
            thought_start_time: float | None = None

            def on_chunk() -> None:
                nonlocal saw_chunk
                saw_chunk = True

            def on_text_delta(delta: str) -> None:
                nonlocal first_token_ms
                if first_token_ms is None:
                    first_token_ms = int((time.perf_counter() - request_started) * 1000)
                    conv.emitter.emit_llm_first_token(call_id, first_token_ms, model=str(stream_kwargs.get("model") or config.MODEL))
                conv.last_run_streamed_text = True
                conv.agent.stream_callback(delta)

            def on_reasoning_start() -> None:
                nonlocal thought_start_time
                thought_start_time = time.time()
                if conv.event_bus is not None:
                    from ..sessions.events import ThoughtStartedEvent
                    conv.event_bus.emit_event(ThoughtStartedEvent().to_event())

            def on_reasoning_delta(delta: str) -> None:
                pass  # Reasoning content collected silently, not displayed

            remove_cancel_callback = self._interrupt_client_on_cancel(cancellation_token)
            try:
                if conv.provider.supports_prompt_cache_key:
                    stream_kwargs.setdefault("stream_options", {"include_usage": True})
                stream = conv.client.chat.completions.create(**stream_kwargs)
                result = conv.provider.assistant_message_from_stream(
                    stream,
                    on_text_delta=on_text_delta,
                    on_chunk=on_chunk,
                    on_reasoning_start=on_reasoning_start,
                    on_reasoning_delta=on_reasoning_delta,
                    cancellation_token=cancellation_token,
                )
                if thought_start_time is not None and conv.event_bus is not None:
                    duration = time.time() - thought_start_time
                    from ..sessions.events import ThoughtFinishedEvent
                    conv.event_bus.emit_event(
                        ThoughtFinishedEvent(
                            duration_seconds=duration,
                            source=conv.provider.name,
                        ).to_event()
                    )
                conv.emitter.emit_llm_response_finished(
                    call_id,
                    int((time.perf_counter() - request_started) * 1000),
                    finish_reason=result.finish_reason,
                    streamed=True,
                    first_token_ms=first_token_ms,
                    model=str(stream_kwargs.get("model") or config.MODEL),
                )
                conv.record_llm_usage(result.usage, kwargs.get("prompt_cache_key"))
                return result.assistant_message, result.finish_reason
            except CancelledError:
                raise
            except Exception as exc:
                if cancellation_token is not None and cancellation_token.is_cancelled:
                    raise CancelledError("Turn cancelled by user") from exc
                if saw_chunk:
                    raise
                conv.trace.error("stream_fallback", str(exc))
            finally:
                remove_cancel_callback()

        conv._check_cancelled(cancellation_token)
        call_id = conv.next_call_id()
        request_started = time.perf_counter()
        conv.emitter.emit_llm_request_started(call_id, streamed=False, model=str(kwargs.get("model") or config.MODEL))
        remove_cancel_callback = self._interrupt_client_on_cancel(cancellation_token)
        try:
            response = conv.client.chat.completions.create(**kwargs)
        except Exception as exc:
            if cancellation_token is not None and cancellation_token.is_cancelled:
                raise CancelledError("Turn cancelled by user") from exc
            raise
        finally:
            remove_cancel_callback()
        conv._check_cancelled(cancellation_token)
        if not response.choices:
            return None
        choice = response.choices[0]
        conv.emitter.emit_llm_response_finished(
            call_id,
            int((time.perf_counter() - request_started) * 1000),
            finish_reason=choice.finish_reason,
            streamed=False,
            first_token_ms=None,
            model=str(kwargs.get("model") or config.MODEL),
        )
        conv.record_llm_usage(_usage_to_dict(getattr(response, "usage", None)), kwargs.get("prompt_cache_key"))

        return conv.provider.assistant_message_from_response(choice.message), choice.finish_reason

    def _interrupt_client_on_cancel(self, cancellation_token):
        if cancellation_token is None:
            return lambda: None
        conv = self.conversation
        client = conv.client

        def interrupt() -> None:
            try:
                client.close()
            except (OSError, RuntimeError):
                pass
            finally:
                reset_client()
                conv._client_needs_refresh = True

        return cancellation_token.add_callback(interrupt)
