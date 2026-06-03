import json
import importlib
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


class ProductRuntimeTests(unittest.TestCase):
    def test_deepseek_model_profiles_use_intensity_defaults(self):
        from harness_code_agent import config

        try:
            with (
                patch.dict(os.environ, {
                    "OPENAI_BASE_URL": "https://api.deepseek.com",
                    "HARNESS_MODEL_INTENSITY": "hard",
                }, clear=True),
                patch("pathlib.Path.exists", return_value=False),
            ):
                importlib.reload(config)

            fast = config.resolve_model_profile("fast")
            normal = config.resolve_model_profile("normal")
            hard = config.resolve_model_profile("hard")
            max_profile = config.resolve_model_profile("max")

            self.assertEqual(config.MODEL, "deepseek-v4-pro")
            self.assertEqual(config.MODEL_INTENSITY, "hard")
            self.assertEqual((fast.model, fast.thinking, fast.reasoning_effort), ("deepseek-v4-flash", False, None))
            self.assertEqual((normal.model, normal.thinking, normal.reasoning_effort), ("deepseek-v4-flash", True, "high"))
            self.assertEqual((hard.model, hard.thinking, hard.reasoning_effort), ("deepseek-v4-pro", True, "high"))
            self.assertEqual((max_profile.model, max_profile.thinking, max_profile.reasoning_effort), ("deepseek-v4-pro", True, "max"))
        finally:
            importlib.reload(config)

    def test_model_intensity_and_profile_model_overrides(self):
        from harness_code_agent import config

        try:
            with (
                patch.dict(os.environ, {
                    "OPENAI_BASE_URL": "https://api.deepseek.com",
                    "HARNESS_MODEL_INTENSITY": "max",
                    "HARNESS_MODEL_FAST": "custom-fast",
                    "HARNESS_MODEL_MAX": "custom-max",
                }, clear=True),
                patch("pathlib.Path.exists", return_value=False),
            ):
                importlib.reload(config)

            self.assertEqual(config.MODEL_INTENSITY, "max")
            self.assertEqual(config.MODEL, "custom-max")
            self.assertEqual(config.resolve_model_profile("fast").model, "custom-fast")
            self.assertEqual(config.resolve_model_profile("max").model, "custom-max")
            self.assertEqual(config.resolve_model_profile("max").reasoning_effort, "max")
        finally:
            importlib.reload(config)

    def test_deepseek_reasoning_content_round_trips_on_tool_call_assistant_message(self):
        from harness_code_agent.agent.loop import _assistant_message_from_response

        msg = SimpleNamespace(
            content=None,
            reasoning_content="think carefully",
            tool_calls=[
                SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(name="read_file", arguments='{"path":"README.md"}'),
                ),
            ],
        )

        with (
            patch("harness_code_agent.agent.loop.config.BASE_URL", "https://api.deepseek.com"),
            patch("harness_code_agent.agent.loop.config.MODEL", "deepseek-v4-flash"),
        ):
            assistant_msg = _assistant_message_from_response(msg)

        self.assertEqual(assistant_msg["reasoning_content"], "think carefully")
        self.assertEqual(assistant_msg["tool_calls"][0]["function"]["name"], "read_file")

    def test_deepseek_reasoning_content_round_trips_from_model_extra(self):
        from harness_code_agent.agent.loop import _assistant_message_from_response

        msg = SimpleNamespace(
            content=None,
            model_extra={"reasoning_content": "provider extra thinking"},
            tool_calls=[],
        )

        with (
            patch("harness_code_agent.agent.loop.config.BASE_URL", "https://api.deepseek.com"),
            patch("harness_code_agent.agent.loop.config.MODEL", "deepseek-v4-flash"),
        ):
            assistant_msg = _assistant_message_from_response(msg)

        self.assertEqual(assistant_msg["reasoning_content"], "provider extra thinking")

    def test_non_deepseek_assistant_message_omits_reasoning_content(self):
        from harness_code_agent.agent.loop import _assistant_message_from_response

        msg = SimpleNamespace(content="ok", reasoning_content="hidden", tool_calls=None)

        with (
            patch("harness_code_agent.agent.loop.config.BASE_URL", "https://api.openai.com/v1"),
            patch("harness_code_agent.agent.loop.config.MODEL", "gpt-4o"),
        ):
            assistant_msg = _assistant_message_from_response(msg)

        self.assertNotIn("reasoning_content", assistant_msg)

    def test_provider_auto_detection_distinguishes_openai_deepseek_and_compatible(self):
        from harness_code_agent.agent.providers import resolve_provider_name

        self.assertEqual(
            resolve_provider_name(provider="auto", base_url="https://api.openai.com/v1", model="gpt-4o"),
            "openai",
        )
        self.assertEqual(
            resolve_provider_name(provider="auto", base_url="https://api.deepseek.com", model="deepseek-chat"),
            "deepseek",
        )
        self.assertEqual(
            resolve_provider_name(provider="auto", base_url="https://example.invalid/v1", model="custom"),
            "openai-compatible",
        )
        self.assertEqual(
            resolve_provider_name(provider="auto", base_url="https://example.invalid/v1", model="my-deepseek-fork-v1"),
            "openai-compatible",
        )

    def test_cached_provider_client_refreshes_when_config_changes(self):
        from harness_code_agent.agent import providers

        providers.reset_client()
        created = []

        def fake_openai(**kwargs):
            client = SimpleNamespace(kwargs=kwargs)
            created.append(client)
            return client

        try:
            with (
                patch("harness_code_agent.agent.providers.OpenAI", side_effect=fake_openai),
                patch.object(providers.config, "API_KEY", "key-a"),
                patch.object(providers.config, "BASE_URL", "https://one.example/v1"),
            ):
                first = providers.get_client()

            with (
                patch("harness_code_agent.agent.providers.OpenAI", side_effect=fake_openai),
                patch.object(providers.config, "API_KEY", "key-a"),
                patch.object(providers.config, "BASE_URL", "https://two.example/v1"),
            ):
                second = providers.get_client()
        finally:
            providers.reset_client()

        self.assertIsNot(first, second)
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].kwargs["base_url"], "https://one.example/v1")
        self.assertEqual(created[1].kwargs["base_url"], "https://two.example/v1")

    def test_provider_streaming_normalizes_content_reasoning_and_tool_calls(self):
        from harness_code_agent.agent.providers import ProviderAdapter

        def chunk(delta, finish_reason=None):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=delta,
                        finish_reason=finish_reason,
                    )
                ]
            )

        deltas = []
        chunks = [
            chunk(SimpleNamespace(content="hel")),
            chunk(SimpleNamespace(content="lo", reasoning_content="think ")),
            chunk(
                SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id="call_1",
                            type="function",
                            function=SimpleNamespace(name="read_file", arguments='{"pa'),
                        )
                    ]
                )
            ),
            chunk(
                SimpleNamespace(
                    reasoning_content="carefully",
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            function=SimpleNamespace(arguments='th":"README.md"}'),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            ),
        ]

        result = ProviderAdapter("deepseek").assistant_message_from_stream(chunks, on_text_delta=deltas.append)

        self.assertEqual(deltas, ["hel", "lo"])
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(result.assistant_message["content"], "hello")
        self.assertEqual(result.assistant_message["reasoning_content"], "think carefully")
        self.assertEqual(result.assistant_message["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(result.assistant_message["tool_calls"][0]["function"]["arguments"], '{"path":"README.md"}')

    def test_provider_streaming_checks_cancellation_between_chunks(self):
        from harness_code_agent.agent.cancellation import CancellationToken, CancelledError
        from harness_code_agent.agent.providers import ProviderAdapter

        def chunk(text):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=text),
                        finish_reason=None,
                    )
                ]
            )

        token = CancellationToken()
        deltas = []

        def chunks():
            yield chunk("hel")
            token.cancel()
            yield chunk("lo")

        with self.assertRaises(CancelledError):
            ProviderAdapter("openai-compatible").assistant_message_from_stream(
                chunks(),
                on_text_delta=deltas.append,
                cancellation_token=token,
            )

        self.assertEqual(deltas, ["hel"])

    def test_streaming_request_falls_back_to_non_stream_before_first_chunk(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation

        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("stream"):
                    raise RuntimeError("stream unavailable")
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="fallback", tool_calls=None),
                            finish_reason="stop",
                        )
                    ]
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        fake_client = FakeClient()
        with patch("harness_code_agent.agent.loop.get_client", return_value=fake_client):
            conversation = AgentConversation(Agent("test", "system", use_tools=False, stream_callback=lambda _: None))
            completion = conversation._request_assistant_message(
                conversation.provider.chat_kwargs(model="m", messages=[], max_tokens=10)
            )

        self.assertEqual(completion[0]["content"], "fallback")
        self.assertTrue(fake_client.chat.completions.calls[0]["stream"])
        self.assertNotIn("stream", fake_client.chat.completions.calls[1])

    def test_streaming_request_traces_pre_chunk_fallback_reason(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation

        class FakeCompletions:
            def create(self, **kwargs):
                if kwargs.get("stream"):
                    raise RuntimeError("stream unavailable")
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="fallback", tool_calls=None),
                            finish_reason="stop",
                        )
                    ]
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
            conversation = AgentConversation(Agent("test", "system", use_tools=False, stream_callback=lambda _: None))

        with patch.object(conversation.trace, "error") as trace_error:
            completion = conversation._request_assistant_message(
                conversation.provider.chat_kwargs(model="m", messages=[], max_tokens=10)
            )

        self.assertEqual(completion[0]["content"], "fallback")
        trace_error.assert_called_once()
        self.assertEqual(trace_error.call_args.args[0], "stream_fallback")
        self.assertIn("stream unavailable", trace_error.call_args.args[1])

    def test_streaming_request_collects_text_deltas_without_non_stream_fallback(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation

        def chunk(text, finish_reason=None):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=text),
                        finish_reason=finish_reason,
                    )
                ]
            )

        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return [chunk("hel"), chunk("lo", finish_reason="stop")]

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        deltas = []
        fake_client = FakeClient()
        with patch("harness_code_agent.agent.loop.get_client", return_value=fake_client):
            conversation = AgentConversation(Agent("test", "system", use_tools=False, stream_callback=deltas.append))
            completion = conversation._request_assistant_message(
                conversation.provider.chat_kwargs(model="m", messages=[], max_tokens=10)
            )

        self.assertEqual(completion[0]["content"], "hello")
        self.assertEqual(completion[1], "stop")
        self.assertEqual(deltas, ["hel", "lo"])
        self.assertTrue(conversation.last_run_streamed_text)
        self.assertEqual(len(fake_client.chat.completions.calls), 1)

    def test_tool_enabled_agent_builds_chat_kwargs_once(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation
        from harness_code_agent.agent.providers import ProviderAdapter

        class FakeCompletions:
            def create(self, **kwargs):
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="done", tool_calls=None),
                            finish_reason="stop",
                        )
                    ]
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        class CountingProvider:
            def __init__(self):
                self.calls = []
                self.delegate = ProviderAdapter("openai")

            @property
            def supports_prompt_cache_key(self):
                return self.delegate.supports_prompt_cache_key

            def chat_kwargs(self, **kwargs):
                chat_kwargs = self.delegate.chat_kwargs(**kwargs)
                self.calls.append(chat_kwargs)
                return chat_kwargs

            def assistant_message_from_response(self, msg):
                return self.delegate.assistant_message_from_response(msg)

        schema = [{"type": "function", "function": {"name": "read_file"}}]
        provider = CountingProvider()
        with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
            conversation = AgentConversation(Agent("test", "system", use_tools=True, tool_schemas=schema))
        conversation.provider = provider

        with (
            patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 1),
            patch("harness_code_agent.agent.loop.context.count_tokens", return_value=1),
        ):
            result = conversation.run_until_idle()

        self.assertEqual(result, "done")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["tools"], schema)
        self.assertEqual(provider.calls[0]["tool_choice"], "auto")

    def test_agent_loop_uses_configured_model_intensity_profile(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation
        from harness_code_agent.agent.providers import ProviderAdapter

        class FakeCompletions:
            def create(self, **kwargs):
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="done", tool_calls=None),
                            finish_reason="stop",
                        )
                    ]
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        class CapturingProvider:
            def __init__(self):
                self.calls = []
                self.delegate = ProviderAdapter("deepseek")

            @property
            def supports_prompt_cache_key(self):
                return self.delegate.supports_prompt_cache_key

            def chat_kwargs(self, **kwargs):
                chat_kwargs = self.delegate.chat_kwargs(**kwargs)
                self.calls.append(chat_kwargs)
                return chat_kwargs

            def assistant_message_from_response(self, msg):
                return self.delegate.assistant_message_from_response(msg)

        provider = CapturingProvider()
        with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
            conversation = AgentConversation(Agent("test", "system", use_tools=False))
        conversation.provider = provider

        with (
            patch("harness_code_agent.agent.loop.config.BASE_URL", "https://api.deepseek.com"),
            patch("harness_code_agent.agent.loop.config.MODEL_INTENSITY", "hard"),
            patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 1),
            patch("harness_code_agent.agent.loop.context.count_tokens", return_value=1),
        ):
            conversation.run_until_idle()

        self.assertEqual(provider.calls[0]["model"], "deepseek-v4-pro")
        self.assertEqual(provider.calls[0]["reasoning_effort"], "high")
        self.assertEqual(provider.calls[0]["extra_body"], {"thinking": {"type": "enabled"}})

    def test_llm_call_simple_uses_fast_profile(self):
        from harness_code_agent.agent import loop

        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="summary"))]
                )

        completions = FakeCompletions()
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        with (
            patch("harness_code_agent.agent.loop.get_client", return_value=fake_client),
            patch.object(loop.config, "BASE_URL", "https://api.deepseek.com"),
        ):
            result = loop.llm_call_simple([{"role": "user", "content": "summarize"}])

        self.assertEqual(result, "summary")
        self.assertEqual(completions.calls[0]["model"], "deepseek-v4-flash")
        self.assertNotIn("reasoning_effort", completions.calls[0])
        self.assertEqual(completions.calls[0]["extra_body"], {"thinking": {"type": "disabled"}})

    def test_profile_router_uses_fast_profile(self):
        from harness_code_agent.profiles import router

        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"profile_name":"coding-agent","confidence":0.9,"reason":"coding"}'))]
                )

        completions = FakeCompletions()
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch("harness_code_agent.profiles.router.get_client", return_value=fake_client),
                patch.object(router.config, "BASE_URL", "https://api.deepseek.com"),
            ):
                decision = router.route_profile_for_task("fix tests", workspace=Path(tmpdir))

        self.assertEqual(decision.profile_name, "coding-agent")
        self.assertEqual(completions.calls[0]["model"], "deepseek-v4-flash")
        self.assertNotIn("reasoning_effort", completions.calls[0])
        self.assertEqual(completions.calls[0]["extra_body"], {"thinking": {"type": "disabled"}})

    def test_profile_router_falls_back_when_profile_catalog_fails(self):
        from harness_code_agent.profiles import router

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("harness_code_agent.profiles.router.list_profiles", side_effect=RuntimeError("catalog failed")):
                decision = router.route_profile_for_task("fix tests", workspace=Path(tmpdir))

        self.assertEqual(decision.profile_name, "coding-agent")
        self.assertTrue(decision.fallback_used)
        self.assertIn("catalog failed", decision.fallback_reason)

    def test_router_json_parser_strips_only_outer_code_fence(self):
        from harness_code_agent.profiles.router import _parse_router_json

        parsed = _parse_router_json(
            '```json\n{"profile_name":"coding-agent","confidence":0.9,"reason":"keep `literal` ticks"}\n```'
        )

        self.assertEqual(parsed["reason"], "keep `literal` ticks")

    def test_turn_summary_event_serializes_payload(self):
        from harness_code_agent.sessions.events import TurnSummaryEvent

        event = TurnSummaryEvent(
            turn=2,
            summary="- changed app.py",
            duration_seconds=12.5,
            tool_counts={"read_file": 1},
            changed_files=["app.py"],
            checkpoint="checkpoint created: abc",
            generated_by={"intensity": "fast", "model": "custom-fast"},
        ).to_event()

        self.assertEqual(event.type, "turn_summary")
        self.assertTrue(event.payload["long_task"])
        self.assertTrue(event.payload["fold_details"])
        self.assertEqual(event.payload["turn"], 2)
        self.assertEqual(event.payload["tool_counts"], {"read_file": 1})
        self.assertEqual(event.payload["changed_files"], ["app.py"])
        self.assertEqual(event.payload["generated_by"]["intensity"], "fast")

    def test_turn_summary_long_task_detection_rules(self):
        from harness_code_agent.sessions.turn_summary import should_summarize_turn

        simple = [{"type": "assistant_message", "payload": {"text": "hello"}}]
        three_tools = [
            {"type": "tool_result", "payload": {"tool": "read_file"}},
            {"type": "tool_result", "payload": {"tool": "read_file"}},
            {"type": "tool_result", "payload": {"tool": "read_file"}},
        ]
        final_plan_update = [{
            "type": "tool_result",
            "payload": {
                "tool": "update_plan_state",
                "metadata": {"planning_state": {"update_kind": "final"}},
            },
        }]

        self.assertFalse(should_summarize_turn(simple, profile_name="coding-agent", duration_seconds=1))
        self.assertFalse(should_summarize_turn(three_tools, profile_name="plan", duration_seconds=1))
        self.assertTrue(should_summarize_turn(three_tools, profile_name="coding-agent", duration_seconds=1))
        self.assertTrue(should_summarize_turn([{"type": "file_change", "payload": {"path": "app.py"}}], profile_name="coding-agent", duration_seconds=1))
        self.assertTrue(should_summarize_turn([{"type": "tool_result", "payload": {"tool": "run_bash"}}], profile_name="coding-agent", duration_seconds=1))
        self.assertTrue(should_summarize_turn([{"type": "agent_fallback", "payload": {"reason": "max_iterations"}}], profile_name="coding-agent", duration_seconds=1))
        self.assertTrue(should_summarize_turn(simple, profile_name="coding-agent", duration_seconds=45))
        self.assertTrue(should_summarize_turn(final_plan_update, profile_name="coding-agent", duration_seconds=1))

    def test_generate_turn_summary_uses_configured_fast_profile(self):
        from harness_code_agent.sessions import turn_summary
        from harness_code_agent import config

        calls = []

        def fake_create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="- summary from fast"))]
            )

        profile = config.ModelProfile(
            provider="deepseek",
            model="custom-fast",
            thinking=False,
            reasoning_effort=None,
        )
        with patch.object(turn_summary.config, "resolve_model_profile", return_value=profile):
            result = turn_summary.generate_turn_summary(
                [{"type": "tool_result", "payload": {"tool": "read_file"}}],
                user_prompt="fix",
                assistant_text="done",
                checkpoint="",
                llm_create=fake_create,
            )

        self.assertEqual(result.summary, "- summary from fast")
        self.assertEqual(result.generated_by["model"], "custom-fast")
        self.assertEqual(calls[0]["model"], "custom-fast")
        self.assertEqual(calls[0]["extra_body"], {"thinking": {"type": "disabled"}})

    def test_generate_turn_summary_falls_back_when_llm_fails(self):
        from harness_code_agent.sessions import turn_summary

        def broken_create(**kwargs):
            raise RuntimeError("nope")

        result = turn_summary.generate_turn_summary(
            [
                {"type": "tool_result", "payload": {"tool": "write_file"}},
                {"type": "file_change", "payload": {"path": "app.py"}},
            ],
            user_prompt="fix app",
            assistant_text="updated app.py",
            checkpoint="checkpoint created: abc",
            llm_create=broken_create,
        )

        self.assertIn("fix app", result.summary)
        self.assertIn("app.py", result.summary)
        self.assertEqual(result.tool_counts, {"write_file": 1})

    def test_openai_provider_accepts_prompt_cache_key_and_stream_usage_options(self):
        from harness_code_agent.agent.providers import ProviderAdapter

        kwargs = ProviderAdapter("openai").chat_kwargs(
            model="m",
            messages=[],
            max_tokens=10,
            prompt_cache_key="cache-key",
            stream_options={"include_usage": True},
        )

        self.assertEqual(kwargs["prompt_cache_key"], "cache-key")
        self.assertEqual(kwargs["stream_options"], {"include_usage": True})

    def test_provider_adapter_maps_model_profile_kwargs(self):
        from harness_code_agent import config
        from harness_code_agent.agent.providers import ProviderAdapter

        deepseek = ProviderAdapter("deepseek").chat_kwargs(
            profile=config.ModelProfile(
                provider="deepseek",
                model="deepseek-v4-pro",
                thinking=True,
                reasoning_effort="high",
            ),
            messages=[],
            max_tokens=10,
        )
        openai = ProviderAdapter("openai").chat_kwargs(
            profile=config.ModelProfile(
                provider="openai",
                model="gpt-4o",
                thinking=True,
                reasoning_effort="high",
            ),
            messages=[],
            max_tokens=10,
        )

        self.assertEqual(deepseek["model"], "deepseek-v4-pro")
        self.assertEqual(deepseek["reasoning_effort"], "high")
        self.assertEqual(deepseek["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(openai["reasoning_effort"], "high")
        self.assertNotIn("extra_body", openai)

    def test_agent_loop_uses_prompt_cache_key_only_for_openai_provider(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation
        from harness_code_agent.agent.providers import ProviderAdapter

        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="done", tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        class CapturingProvider:
            def __init__(self, name):
                self.name = name
                self.calls = []
                self.delegate = ProviderAdapter(name)

            @property
            def supports_prompt_cache_key(self):
                return self.delegate.supports_prompt_cache_key

            def chat_kwargs(self, **kwargs):
                self.calls.append(kwargs)
                return self.delegate.chat_kwargs(**kwargs)

            def assistant_message_from_response(self, msg):
                return self.delegate.assistant_message_from_response(msg)

        with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
            openai_conv = AgentConversation(Agent("test", "system", use_tools=False))
            compatible_conv = AgentConversation(Agent("test", "system", use_tools=False))
        openai_conv.provider = CapturingProvider("openai")
        compatible_conv.provider = CapturingProvider("openai-compatible")

        with (
            patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 1),
            patch("harness_code_agent.agent.loop.context.count_tokens", return_value=1),
        ):
            openai_conv.run_until_idle()
            compatible_conv.run_until_idle()

        self.assertIn("prompt_cache_key", openai_conv.provider.calls[0])
        self.assertNotIn("prompt_cache_key", compatible_conv.provider.calls[0])

    def test_prompt_cache_key_changes_when_system_prompt_changes(self):
        from harness_code_agent.agent.loop import Agent
        from harness_code_agent.agent.utils import _prompt_cache_key

        first = _prompt_cache_key(Agent("test", "system\nHARNESS A", use_tools=False), None)
        second = _prompt_cache_key(Agent("test", "system\nHARNESS B", use_tools=False), None)

        self.assertNotEqual(first, second)

    def test_prompt_cache_key_uses_stable_prefix_identity_and_tools_hash(self):
        from harness_code_agent.agent.loop import Agent
        from harness_code_agent.agent.utils import _prompt_cache_key

        first = _prompt_cache_key(
            Agent(
                "test",
                "rendered system",
                use_tools=False,
                prompt_cache_identity={"global_rules_hash": "a"},
            ),
            [{"type": "function", "function": {"name": "read_file"}}],
        )
        second = _prompt_cache_key(
            Agent(
                "test",
                "rendered system",
                use_tools=False,
                prompt_cache_identity={"global_rules_hash": "b"},
            ),
            [{"type": "function", "function": {"name": "read_file"}}],
        )
        third = _prompt_cache_key(
            Agent(
                "test",
                "rendered system",
                use_tools=False,
                prompt_cache_identity={"global_rules_hash": "a"},
            ),
            [{"type": "function", "function": {"name": "write_file"}}],
        )

        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)

    def test_prompt_cache_key_canonicalizes_tool_schema_order(self):
        from harness_code_agent.agent.loop import Agent
        from harness_code_agent.agent.utils import _prompt_cache_key

        agent = Agent(
            "test",
            "rendered system",
            use_tools=True,
            prompt_cache_identity={"global_rules_hash": "a"},
        )
        read_schema = {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {
                    "type": "object",
                    "required": ["path", "max_lines"],
                    "properties": {
                        "path": {"type": "string"},
                        "max_lines": {"type": "integer"},
                    },
                },
            },
        }
        write_schema = {
            "type": "function",
            "function": {
                "name": "write_file",
                "parameters": {
                    "type": "object",
                    "required": ["content", "path"],
                    "properties": {
                        "content": {"type": "string"},
                        "path": {"type": "string"},
                    },
                },
            },
        }

        first = _prompt_cache_key(agent, [read_schema, write_schema])
        second = _prompt_cache_key(agent, [write_schema, read_schema])

        self.assertEqual(first, second)

    def test_context_replacement_detaches_observation_indexes_and_summarizes_survivors(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation
        from harness_code_agent.runtime.tool_result import ToolResult

        with patch("harness_code_agent.agent.loop.get_client"):
            conversation = AgentConversation(Agent("test", "system", use_tools=False))

        result = ToolResult(tool="read_file", status="success", output="SECRET_RAW_CONTENT")
        observation = conversation.observation_store.create(
            tool="read_file",
            args={"path": "note.txt"},
            result=result,
            fact_tracker=conversation.fact_tracker,
        )
        observed = conversation.observation_store.observed_message(observation, result)
        conversation.messages.extend(
            [
                {"role": "user", "content": "old turn"},
                {"role": "tool", "tool_call_id": "tc_read", "content": observed},
            ]
        )
        observation.message_index = 2

        conversation._replace_messages(
            [
                conversation.messages[0],
                {"role": "user", "content": "summary"},
                {"role": "tool", "tool_call_id": "tc_read", "content": observed},
            ]
        )

        prompt_text = json.dumps(conversation.messages, ensure_ascii=False)
        self.assertNotIn("SECRET_RAW_CONTENT", prompt_text)
        self.assertIn("historical", prompt_text)
        self.assertIsNone(observation.message_index)

    def test_agent_loop_records_llm_cached_token_usage_event(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation
        from harness_code_agent.sessions.events import EventBus

        class FakeCompletions:
            def create(self, **kwargs):
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="done", tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                    usage=SimpleNamespace(
                        prompt_tokens=100,
                        completion_tokens=20,
                        total_tokens=120,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=80),
                    ),
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        events = []
        with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
            conversation = AgentConversation(Agent("test", "system", use_tools=False))
        conversation._event_bus = EventBus(listener=events.append)

        with (
            patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 1),
            patch("harness_code_agent.agent.loop.context.count_tokens", return_value=1),
        ):
            conversation.run_until_idle()

        usage = [event for event in events if event.type == "llm_usage"][0]
        self.assertEqual(usage.payload["cached_tokens"], 80)
        self.assertEqual(usage.payload["prompt_tokens"], 100)
        self.assertEqual(usage.payload["cache_hit_ratio"], 0.8)

    def test_invalidated_long_observation_is_compressed_and_notice_is_appended(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation

        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(json.loads(json.dumps(kwargs["messages"])))
                call_count = len(self.calls)
                if call_count == 1:
                    message = SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="tc_read",
                                type="function",
                                function=SimpleNamespace(
                                    name="read_file",
                                    arguments='{"path":"note.txt"}',
                                ),
                            )
                        ],
                    )
                    finish_reason = "tool_calls"
                elif call_count == 2:
                    message = SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="tc_write",
                                type="function",
                                function=SimpleNamespace(
                                    name="write_file",
                                    arguments='{"path":"note.txt","content":"updated"}',
                                ),
                            )
                        ],
                    )
                    finish_reason = "tool_calls"
                else:
                    message = SimpleNamespace(content="done", tool_calls=None)
                    finish_reason = "stop"
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
                    usage=None,
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("SECRET_FULL_CONTENT_UNSAFE" * 700, encoding="utf-8")
            fake_client = FakeClient()
            with (
                patch("harness_code_agent.agent.loop.get_client", return_value=fake_client),
                patch("harness_code_agent.agent.loop.config.WORKSPACE", str(root)),
            ):
                conversation = AgentConversation(Agent("test", "system", use_tools=True))

            with (
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 3),
                patch("harness_code_agent.agent.loop.context.count_tokens", return_value=1),
                patch("harness_code_agent.runtime.tools.config.WORKSPACE", str(root)),
            ):
                conversation.run_until_idle()

            second_prompt = json.dumps(fake_client.chat.completions.calls[1], ensure_ascii=False)
            third_prompt = json.dumps(fake_client.chat.completions.calls[2], ensure_ascii=False)

            self.assertIn("SECRET_FULL_CONTENT_UNSAFE", second_prompt)
            self.assertNotIn("SECRET_FULL_CONTENT_UNSAFE", third_prompt)
            self.assertIn("[OBS", third_prompt)
            self.assertIn("stale", third_prompt)
            self.assertIn("FACT INVALIDATION", third_prompt)
            self.assertIn("Compressed stale long observations", third_prompt)
            self.assertTrue(list((root / ".harness" / "observations").rglob("*.txt")))

    def test_invalidated_short_observation_keeps_original_message_and_appends_notice(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation

        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(json.loads(json.dumps(kwargs["messages"])))
                call_count = len(self.calls)
                if call_count == 1:
                    message = SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="tc_read",
                                type="function",
                                function=SimpleNamespace(
                                    name="read_file",
                                    arguments='{"path":"note.txt"}',
                                ),
                            )
                        ],
                    )
                    finish_reason = "tool_calls"
                elif call_count == 2:
                    message = SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="tc_write",
                                type="function",
                                function=SimpleNamespace(
                                    name="write_file",
                                    arguments='{"path":"note.txt","content":"updated"}',
                                ),
                            )
                        ],
                    )
                    finish_reason = "tool_calls"
                else:
                    message = SimpleNamespace(content="done", tool_calls=None)
                    finish_reason = "stop"
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
                    usage=None,
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("SHORT_STALE_CONTENT", encoding="utf-8")
            fake_client = FakeClient()
            with (
                patch("harness_code_agent.agent.loop.get_client", return_value=fake_client),
                patch("harness_code_agent.agent.loop.config.WORKSPACE", str(root)),
            ):
                conversation = AgentConversation(Agent("test", "system", use_tools=True))

            with (
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 3),
                patch("harness_code_agent.agent.loop.context.count_tokens", return_value=1),
                patch("harness_code_agent.runtime.tools.config.WORKSPACE", str(root)),
            ):
                conversation.run_until_idle()

            third_prompt = json.dumps(fake_client.chat.completions.calls[2], ensure_ascii=False)

            self.assertIn("SHORT_STALE_CONTENT", third_prompt)
            self.assertIn("FACT INVALIDATION", third_prompt)
            self.assertNotIn("Compressed stale long observations", third_prompt)

    def test_trace_writer_stores_traces_under_harness_directory_without_stderr_by_default(self):
        from harness_code_agent.agent.loop import TraceWriter

        with tempfile.TemporaryDirectory() as tmp:
            stderr = StringIO()
            with (
                patch("harness_code_agent.agent.loop.config.WORKSPACE", tmp),
                patch("harness_code_agent.agent.loop.config.TRACE_STDERR", False),
            ):
                with redirect_stderr(stderr):
                    writer = TraceWriter("main_agent")
                    writer.iteration(1, 42)

            trace_path = Path(tmp) / ".harness" / "traces" / "trace_main_agent.jsonl"
            self.assertTrue(trace_path.exists())
            self.assertFalse((Path(tmp) / "_trace_main_agent.jsonl").exists())
            self.assertEqual(stderr.getvalue(), "")

    def test_builtin_tool_registry_exposes_schema_and_dispatch_exports(self):
        from harness_code_agent.runtime import tools

        registry_names = {
            schema["function"]["name"]
            for schema in tools.BUILTIN_TOOL_REGISTRY.schemas()
        }
        exported_schema_names = {
            schema["function"]["name"]
            for schema in tools.TOOL_SCHEMAS + tools.BROWSER_TOOL_SCHEMAS
        }

        self.assertEqual(registry_names, exported_schema_names)
        self.assertIs(tools.BUILTIN_TOOL_REGISTRY.get("read_file"), tools.TOOL_DISPATCH["read_file"])
        self.assertIs(tools.BUILTIN_TOOL_REGISTRY.get("stop_dev_server"), tools.TOOL_DISPATCH["stop_dev_server"])
        self.assertEqual(tools.BUILTIN_TOOL_REGISTRY.permission_for("web_search"), "network_read")
        self.assertTrue(all(spec.permission for spec in tools.BUILTIN_TOOL_REGISTRY.specs()))
        self.assertIsNone(tools.BUILTIN_TOOL_REGISTRY.get("missing_tool"))

    def test_tool_registry_requires_explicit_permission_classification(self):
        from harness_code_agent.runtime import tools

        registry = tools.ToolRegistry()
        schema = {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a location.",
                "parameters": {
                    "type": "object",
                    "required": ["location"],
                    "properties": {"location": {"type": "string"}},
                },
            },
        }

        with self.assertRaisesRegex(ValueError, "permission"):
            registry.register(schema, lambda **_: "sunny")

        with self.assertRaisesRegex(ValueError, "unknown permission"):
            registry.register(schema, lambda **_: "sunny", permission="weatherish")

    def test_agent_update_tool_schemas_invalidates_conversation_prompt_cache(self):
        from harness_code_agent.agent.loop import Agent

        class DummyConversation:
            pass

        agent = Agent("test", "system", use_tools=True, tool_schemas=[])
        conversation = DummyConversation()
        conversation._cached_prompt_cache_key = "old-cache-key"
        agent._conversations.add(conversation)

        agent.update_tool_schemas(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "dynamic_tool",
                        "description": "Dynamic tool",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        )

        self.assertEqual(agent.allowed_tool_names, {"dynamic_tool"})
        self.assertIsNone(conversation._cached_prompt_cache_key)

    def test_agent_update_tool_schemas_preserves_prompt_cache_when_unchanged(self):
        from harness_code_agent.agent.loop import Agent

        class DummyConversation:
            pass

        schemas = [
            {
                "type": "function",
                "function": {
                    "name": "dynamic_tool",
                    "description": "Dynamic tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        agent = Agent("test", "system", use_tools=True, tool_schemas=schemas)
        conversation = DummyConversation()
        conversation._cached_prompt_cache_key = "old-cache-key"
        agent._conversations.add(conversation)

        agent.update_tool_schemas(list(schemas))

        self.assertEqual(agent.allowed_tool_names, {"dynamic_tool"})
        self.assertEqual(conversation._cached_prompt_cache_key, "old-cache-key")

    def test_network_read_tool_permission_is_allowed_without_approval(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy

        policy = PermissionPolicy(mode="workspace-write")

        decision = policy.decide_tool_call(
            "get_weather",
            {"location": "Hong Kong"},
            tool_permission="network_read",
        )

        self.assertTrue(decision.allowed)
        self.assertFalse(decision.requires_approval)
        self.assertEqual(decision.risk, "network_read")

    def test_ask_user_tool_appends_other_and_returns_structured_choice(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.questions import StaticQuestionProvider
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(root / ".harness" / "events.jsonl"),
                question_provider=StaticQuestionProvider(index=1),
            )

            result = tools.execute_tool(
                "ask_user",
                {"question": "Pick a path", "options": ["Fast path"]},
                tool_context=context,
                agent_name="main_agent",
            )

            data = json.loads(result)
            self.assertEqual(data["selected_index"], 1)
            self.assertEqual(data["label"], "其他")
            self.assertTrue(data["is_other"])
            self.assertIn("ask_user", [schema["function"]["name"] for schema in tools.TOOL_SCHEMAS])

    def test_structured_event_schema_covers_mvp_event_types(self):
        from harness_code_agent.sessions.events import (
            AgentBudgetWarningEvent,
            AgentFallbackEvent,
            AssistantMessageEvent,
            FailureEvent,
            FileChangeEvent,
            FinalReportEvent,
            SessionFinishedEvent,
            TaskOutcomeEvent,
            ToolCallEvent,
            ToolResultEvent,
            UserInputEvent,
        )

        event_types = {
            AgentBudgetWarningEvent(limit_type="total_tokens", used=80, limit=100).to_event().type,
            AgentFallbackEvent(reason="loop_detected").to_event().type,
            UserInputEvent(text="fix").to_event().type,
            AssistantMessageEvent(text="done").to_event().type,
            ToolCallEvent(tool="read_file", args={"path": "README.md"}).to_event().type,
            ToolResultEvent(tool="read_file", status="success", output="ok").to_event().type,
            FileChangeEvent(path="app.py").to_event().type,
            FailureEvent(category="tool_error", message="boom").to_event().type,
            FinalReportEvent(status="success", reason="completed", summary="done").to_event().type,
            SessionFinishedEvent(reason="user_exit", status="closed").to_event().type,
            TaskOutcomeEvent(status="success", evidence=["tests_passed"], summary="done").to_event().type,
        }

        self.assertEqual(event_types, {
            "agent_budget_warning",
            "agent_fallback",
            "user_input",
            "assistant_message",
            "tool_call",
            "tool_result",
            "file_change",
            "failure",
            "final_report",
            "session_finished",
            "task_outcome",
        })

    def test_failure_classification_uses_stable_sources_before_text(self):
        from harness_code_agent.runtime.tool_result import ToolResult
        from harness_code_agent.sessions.events import classify_tool_failure

        cases = [
            (
                ToolResult(tool="read_file", status="failed", error="missing", metadata={"status_source": "native"}),
                "tool_error",
            ),
            (
                ToolResult(tool="write_file", status="failed", error="empty path", metadata={"status_source": "validation"}),
                "validation_error",
            ),
            (
                ToolResult(tool="run_bash", status="failed", error="Command exited with code 1", metadata={"status_source": "shell"}),
                "runtime_error",
            ),
            (
                ToolResult(tool="write_file", status="failed", error="user said no", metadata={"status_source": "approval"}),
                "user_cancelled",
            ),
            (
                ToolResult(tool="custom", status="failed", error="", metadata={}),
                "unknown",
            ),
        ]

        for result, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_tool_failure(result), expected)

    def test_tool_result_serializes_and_tool_execution_records_structured_events(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.runtime.tool_result import ToolResult
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        result = ToolResult(
            tool="read_file",
            status="failed",
            output="",
            error="missing",
            return_code=2,
            metadata={"path": "missing.txt"},
        )

        self.assertEqual(result.to_dict()["tool"], "read_file")
        self.assertEqual(result.to_dict()["status"], "failed")
        self.assertFalse(result.to_dict()["ok"])
        self.assertEqual(result.to_dict()["error"], "missing")
        self.assertEqual(result.to_text(), "[error] missing")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("hello", encoding="utf-8")
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            output = tools.execute_tool(
                "read_file",
                {"path": "note.txt"},
                tool_context=context,
                agent_name="main_agent",
            )
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(output, "hello")
            event_types = [event["type"] for event in events]
            self.assertEqual(event_types, ["tool_call", "tool_result"])
            tool_result = next(event for event in events if event["type"] == "tool_result")
            self.assertEqual(tool_result["payload"]["tool"], "read_file")
            self.assertEqual(tool_result["payload"]["status"], "success")
            self.assertTrue(tool_result["payload"]["ok"])
            self.assertEqual(tool_result["payload"]["output"], "[redacted read_file output: 5 chars]")
            self.assertTrue(tool_result["payload"]["metadata"]["output_redacted"])
            self.assertEqual(tool_result["payload"]["metadata"]["output_length"], 5)

    def test_read_file_supports_line_ranges_and_line_numbers(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(root / ".harness" / "events.jsonl"),
            )

            output = tools.execute_tool(
                "read_file",
                {
                    "path": "note.txt",
                    "start_line": 2,
                    "max_lines": 2,
                    "include_line_numbers": True,
                },
                tool_context=context,
                agent_name="main_agent",
            )

        self.assertEqual(output, "2: two\n3: three")

    def test_read_file_requires_bounded_ranges_for_files_over_500_lines(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "big.txt").write_text(
                "\n".join(f"line {i}" for i in range(1, 502)),
                encoding="utf-8",
            )
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(root / ".harness" / "events.jsonl"),
            )

            output = tools.execute_tool(
                "read_file",
                {"path": "big.txt"},
                tool_context=context,
                agent_name="main_agent",
            )

        self.assertIn("[error]", output)
        self.assertIn("500 lines", output)
        self.assertIn("start_line", output)
        self.assertIn("max_lines", output)

    def test_read_file_rejects_ranges_over_500_lines(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "big.txt").write_text(
                "\n".join(f"line {i}" for i in range(1, 700)),
                encoding="utf-8",
            )
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(root / ".harness" / "events.jsonl"),
            )

            output = tools.execute_tool(
                "read_file",
                {"path": "big.txt", "start_line": 1, "max_lines": 501},
                tool_context=context,
                agent_name="main_agent",
            )

        self.assertIn("[error]", output)
        self.assertIn("max_lines must be <= 500", output)

    def test_read_file_rejects_windows_with_too_much_output(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "wide.txt").write_text("x" * 100_001, encoding="utf-8")
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(root / ".harness" / "events.jsonl"),
            )

            output = tools.execute_tool(
                "read_file",
                {"path": "wide.txt", "start_line": 1, "max_lines": 1},
                tool_context=context,
                agent_name="main_agent",
            )

        self.assertIn("[error]", output)
        self.assertIn("too large", output)
        self.assertNotIn("[TRUNCATED]", output)

    def test_tool_result_does_not_infer_status_from_raw_tool_text(self):
        from unittest.mock import patch

        from harness_code_agent.runtime.approvals import StaticApprovalProvider
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
                approval_provider=StaticApprovalProvider(approved=True, reason="test approval"),
            )

            with patch.object(
                tools.BUILTIN_TOOL_REGISTRY,
                "get",
                return_value=lambda **kwargs: "[error] this is domain output, not execution status",
            ):
                output = tools.execute_tool(
                    "custom_tool",
                    {},
                    tool_context=context,
                    agent_name="main_agent",
                )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            tool_result = [event for event in events if event["type"] == "tool_result"][0]

            self.assertEqual(output, "[error] this is domain output, not execution status")
            self.assertEqual(tool_result["payload"]["status"], "unknown")
            self.assertIsNone(tool_result["payload"]["ok"])
            self.assertEqual(tool_result["payload"]["metadata"]["status_source"], "unstructured")
            self.assertFalse(any(event["type"] == "failure" for event in events))

    def test_unknown_tool_records_structured_failure_events(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            output = tools.execute_tool(
                "missing_tool",
                {"secret": "nope"},
                tool_context=context,
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            event_types = [event["type"] for event in events]
            tool_result = next(event for event in events if event["type"] == "tool_result")

            self.assertEqual(output, "[error] Unknown tool: missing_tool")
            self.assertIn("tool_call", event_types)
            self.assertIn("failure", event_types)
            self.assertEqual(tool_result["payload"]["status"], "failed")
            self.assertFalse(tool_result["payload"]["ok"])
            failure = next(event for event in events if event["type"] == "failure")
            self.assertEqual(failure["payload"]["category"], "tool_error")
            self.assertEqual(failure["payload"]["tool"], "missing_tool")

    def test_tool_validation_failures_return_typed_failed_results(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            missing = tools.execute_tool(
                "read_file",
                {"path": "missing.txt"},
                tool_context=context,
                agent_name="main_agent",
            )
            empty_write = tools.execute_tool(
                "write_file",
                {"path": "", "content": "x"},
                tool_context=context,
                agent_name="main_agent",
            )
            empty_patch = tools.execute_tool(
                "apply_patch",
                {"path": "", "search": "x", "replace": "y"},
                tool_context=context,
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            failed_results = [
                event for event in events
                if event["type"] == "tool_result"
                and event["payload"].get("status") == "failed"
            ]

            self.assertIn("[error] File not found: missing.txt", missing)
            self.assertIn("[auto-fix] Empty file path", empty_write)
            self.assertIn("[error] Empty file path", empty_patch)
            self.assertEqual(len(failed_results), 3)
            failures = [event for event in events if event["type"] == "failure"]
            self.assertEqual(len(failures), 3)
            self.assertEqual(
                [event["payload"]["category"] for event in failures],
                ["tool_error", "validation_error", "validation_error"],
            )

    def test_session_store_creates_metadata_and_jsonl_events(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            session = store.create(
                profile="terminal",
                cwd=Path(tmp),
                model="test-model",
                permission_mode="workspace-write",
            )
            bus = store.event_bus(session)
            bus.emit("session_started", agent="main_agent", payload={"task": "fix bug"})

            metadata = json.loads(session.metadata_path.read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in session.events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(metadata["profile"], "terminal")
            self.assertEqual(metadata["model"], "test-model")
            self.assertEqual(metadata["permission_mode"], "workspace-write")
            self.assertEqual(events[0]["type"], "session_started")
            self.assertEqual(events[0]["sequence"], 1)

    def test_session_store_lists_and_reads_sessions(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            first = store.create(
                profile="terminal",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
            )
            second = store.create(
                profile="plan",
                cwd=Path(tmp),
                model="model-b",
                permission_mode="read-only",
            )
            store.event_bus(second).emit("session_finished", agent="main_agent", payload={})

            sessions = store.list_sessions()
            metadata = store.read_metadata(second.id)
            events = store.read_events(second.id)

            self.assertEqual([item["id"] for item in sessions], [second.id, first.id])
            self.assertEqual(metadata["profile"], "plan")
            self.assertEqual(events[0]["type"], "session_finished")

    def test_session_store_forks_session_metadata_and_lineage_event(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            source = store.create(
                profile="coding-agent",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
            )
            store.event_bus(source).emit("session_started", agent="main_agent", payload={})
            store.event_bus(source).emit("session_finished", agent="main_agent", payload={})

            fork = store.fork(source.id)
            metadata = store.read_metadata(fork.id)
            events = store.read_events(fork.id)

            self.assertNotEqual(fork.id, source.id)
            self.assertEqual(metadata["profile"], "coding-agent")
            self.assertEqual(metadata["model"], "model-a")
            self.assertEqual(metadata["permission_mode"], "workspace-write")
            self.assertEqual(metadata["forked_from"], source.id)
            self.assertEqual(metadata["forked_from_event_count"], 2)
            self.assertEqual(events[0]["type"], "session_forked")
            self.assertEqual(events[0]["payload"]["source_session_id"], source.id)

    def test_session_store_reads_fork_lineage_and_resumed_metadata(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            source = store.create(
                profile="coding-agent",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
            )
            fork = store.fork(source.id)
            resumed = store.create(
                profile="coding-agent",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
                resumed_from=fork.id,
            )

            lineage = store.read_lineage(fork.id)
            resumed_metadata = store.read_metadata(resumed.id)

            self.assertEqual([item["id"] for item in lineage], [source.id, fork.id])
            self.assertEqual(resumed_metadata["resumed_from"], fork.id)

    def test_session_store_reads_latest_session_and_persisted_summary(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            first = store.create(
                profile="coding-agent",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
            )
            second = store.create(
                profile="plan",
                cwd=Path(tmp),
                model="model-b",
                permission_mode="read-only",
            )
            store.event_bus(second).emit("user_input", agent="main_agent", payload={"text": "plan it"})
            summary = store.write_summary(second.id)

            latest = store.latest_session()

            self.assertEqual(latest["id"], second.id)
            self.assertIn("Session summary", summary)
            self.assertIn("profile: plan", store.read_summary(second.id))
            with self.assertRaises(FileNotFoundError):
                store.read_summary(first.id)

    def test_session_summary_formats_human_readable_event_overview(self):
        from harness_code_agent.sessions.summary import format_session_summary

        metadata = {
            "id": "session-a",
            "profile": "coding-agent",
            "model": "model-a",
            "permission_mode": "workspace-write",
            "status": "running",
            "cwd": "C:/workspace",
            "created_at": "2026-05-20T00:00:00+00:00",
            "forked_from": "session-parent",
        }
        events = [
            {"sequence": 1, "type": "session_started", "agent": "main_agent", "payload": {}},
            {"sequence": 2, "type": "turn_started", "agent": "main_agent", "payload": {"turn": 1}},
            {"sequence": 3, "type": "tool_result", "agent": "main_agent", "payload": {"tool": "write_file", "status": "success", "ok": True}},
            {"sequence": 4, "type": "file_change", "agent": "main_agent", "payload": {"path": "app.py"}},
            {"sequence": 5, "type": "approval_requested", "agent": "main_agent", "payload": {"tool": "run_bash"}},
            {"sequence": 6, "type": "approval_decided", "agent": "main_agent", "payload": {"tool": "run_bash", "approved": False}},
            {
                "sequence": 7,
                "type": "profile_switched",
                "agent": "main_agent",
                "payload": {"previous_profile": "coding-agent", "profile": "plan", "reason": "slash command"},
            },
            {"sequence": 8, "type": "plan_ready", "agent": "main_agent", "payload": {"profile": "plan"}},
            {"sequence": 9, "type": "task_outcome", "agent": "main_agent", "payload": {"status": "success", "summary": "done"}},
            {"sequence": 10, "type": "agent_fallback", "agent": "main_agent", "payload": {"reason": "loop_detected"}},
            {"sequence": 11, "type": "session_finished", "agent": "main_agent", "payload": {"status": "closed", "reason": "user_exit"}},
        ]

        summary = format_session_summary(metadata, events)

        self.assertIn("Session summary", summary)
        self.assertIn("id: session-a", summary)
        self.assertIn("status: closed", summary)
        self.assertIn("forked_from: session-parent", summary)
        self.assertIn("turns: 1 started, 0 finished", summary)
        self.assertIn("tools: 1 call(s): write_file=1", summary)
        self.assertIn("changed_files: app.py", summary)
        self.assertIn("approvals: 1 requested, 0 approved, 1 denied", summary)
        self.assertIn("profile_switches: coding-agent -> plan (slash command)", summary)
        self.assertIn("plans_ready: 1", summary)
        self.assertIn("fallbacks: 1 (latest: loop_detected)", summary)
        self.assertIn("task_outcome: success - done", summary)
        self.assertIn("recent_events:", summary)

    def test_session_summary_uses_final_report_for_phase_two_status_and_categories(self):
        from harness_code_agent.sessions.summary import format_session_summary

        metadata = {"id": "session-final", "profile": "coding-agent", "status": "running"}
        events = [
            {"sequence": 1, "type": "user_input", "agent": "main_agent", "payload": {"text": "fix"}},
            {"sequence": 2, "type": "tool_result", "agent": "main_agent", "payload": {"tool": "read_file", "status": "failed"}},
            {"sequence": 3, "type": "failure", "agent": "main_agent", "payload": {"category": "tool_error", "message": "missing"}},
            {
                "sequence": 4,
                "type": "final_report",
                "agent": "main_agent",
                "payload": {
                    "status": "failed",
                    "reason": "verification_failed",
                    "summary": "tests still fail",
                    "statistics": {
                        "events": 3,
                        "user_inputs": 1,
                        "assistant_messages": 0,
                        "tool_calls": 1,
                        "failures": 1,
                        "file_changes": 0,
                    },
                    "failure_categories": {"tool_error": 1},
                    "tool_counts": {"read_file": 1},
                    "changed_files": [],
                },
            },
        ]

        summary = format_session_summary(metadata, events)

        self.assertIn("status: failed", summary)
        self.assertIn("final_report: failed - tests still fail", summary)
        self.assertIn("failure_categories: tool_error=1", summary)

    def test_session_summary_handles_empty_or_sparse_events(self):
        from harness_code_agent.sessions.summary import format_session_summary

        summary = format_session_summary(
            {"id": "empty-session", "profile": "plan", "status": "running"},
            [{"type": "tool_result", "payload": None}],
        )

        self.assertIn("id: empty-session", summary)
        self.assertIn("profile: plan", summary)
        self.assertIn("events: 1", summary)
        self.assertIn("tools: 1 call(s): unknown=1", summary)
        self.assertIn("changed_files: unknown", summary)
        self.assertIn("task_outcome: unknown", summary)

    def test_final_report_payload_is_statistics_ready_for_replay_and_evaluation(self):
        from harness_code_agent.sessions.events import FinalReportEvent
        from harness_code_agent.sessions.report import build_final_report

        metadata = {"id": "session-a", "created_at": "2026-05-21T00:00:00+00:00"}
        events = [
            {"sequence": 1, "type": "user_input", "agent": "main_agent", "payload": {"text": "fix"}},
            {"sequence": 2, "type": "assistant_message", "agent": "main_agent", "payload": {"text": "I changed app.py"}},
            {"sequence": 3, "type": "tool_result", "agent": "main_agent", "payload": {"tool": "write_file", "status": "success"}},
            {"sequence": 4, "type": "file_change", "agent": "main_agent", "payload": {"path": "app.py"}},
            {"sequence": 5, "type": "failure", "agent": "main_agent", "payload": {"category": "validation_error", "message": "empty"}},
            {"sequence": 6, "type": "agent_fallback", "agent": "main_agent", "payload": {"reason": "token_budget_exceeded"}},
        ]

        report = build_final_report(
            metadata,
            events,
            status="closed",
            reason="user_exit",
            summary="I changed app.py",
        )
        event = FinalReportEvent(**report).to_event()

        self.assertEqual(event.type, "final_report")
        self.assertEqual(event.payload["session_id"], "session-a")
        self.assertEqual(event.payload["status"], "closed")
        self.assertEqual(event.payload["reason"], "user_exit")
        self.assertEqual(event.payload["summary"], "I changed app.py")
        self.assertEqual(event.payload["statistics"]["user_inputs"], 1)
        self.assertEqual(event.payload["statistics"]["assistant_messages"], 1)
        self.assertEqual(event.payload["statistics"]["tool_calls"], 1)
        self.assertEqual(event.payload["statistics"]["failures"], 1)
        self.assertEqual(event.payload["statistics"]["fallbacks"], 1)
        self.assertEqual(event.payload["statistics"]["latest_fallback"], "token_budget_exceeded")
        self.assertEqual(event.payload["failure_categories"], {"validation_error": 1})
        self.assertEqual(event.payload["tool_counts"], {"write_file": 1})
        self.assertEqual(event.payload["changed_files"], ["app.py"])

    def test_session_report_and_summary_share_event_helpers(self):
        from harness_code_agent.sessions import _event_helpers
        from harness_code_agent.sessions import report, summary

        self.assertIs(report._count_events, _event_helpers.count_events)
        self.assertIs(report._tool_counts, _event_helpers.tool_counts)
        self.assertIs(report._failure_categories, _event_helpers.failure_categories)
        self.assertIs(report._changed_files, _event_helpers.changed_files)
        self.assertIs(report._event_type, _event_helpers.event_type)
        self.assertIs(report._payload, _event_helpers.payload)
        self.assertIs(summary._count_events, _event_helpers.count_events)
        self.assertIs(summary._tool_counts, _event_helpers.tool_counts)
        self.assertIs(summary._event_failure_categories, _event_helpers.failure_categories)
        self.assertIs(summary._changed_files, _event_helpers.changed_files)
        self.assertIs(summary._event_type, _event_helpers.event_type)
        self.assertIs(summary._payload, _event_helpers.payload)

    def test_workspace_service_resolves_paths_and_snapshots_before_write(self):
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots")
            target = root / "src" / "app.py"
            target.parent.mkdir()
            target.write_text("old", encoding="utf-8")

            result = workspace.write_text("src/app.py", "new")

            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertTrue(result.snapshot_path.exists())
            self.assertEqual(result.snapshot_path.read_text(encoding="utf-8"), "old")
            with self.assertRaises(ValueError):
                workspace.resolve("../outside.txt")

    def test_workspace_service_applies_unique_text_patch_and_rejects_ambiguous_patch(self):
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots")
            target = root / "app.py"
            target.write_text("alpha\nbeta\n", encoding="utf-8")

            result = workspace.apply_text_patch("app.py", search="beta\n", replace="gamma\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "alpha\ngamma\n")
            self.assertTrue(result.snapshot_path.exists())

            target.write_text("same\nsame\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                workspace.apply_text_patch("app.py", search="same\n", replace="once\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "same\nsame\n")

    def test_workspace_service_rolls_back_latest_snapshot_for_file(self):
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots")
            target = root / "app.py"
            target.write_text("old\n", encoding="utf-8")
            workspace.write_text("app.py", "new\n")

            result = workspace.rollback_latest_snapshot("app.py")

            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
            self.assertTrue(result.snapshot_path.exists())

    def test_permission_policy_uses_codex_sandbox_modes(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy

        workspace_policy = PermissionPolicy(mode="workspace-write")
        read_decision = workspace_policy.decide_tool_call("read_file", {"path": "x.txt"})
        edit_decision = workspace_policy.decide_tool_call("write_file", {"path": "x.txt"})
        plan_decision = workspace_policy.decide_tool_call("update_plan_state", {"mode": "light"})
        safe_shell_decision = workspace_policy.decide_tool_call(
            "run_bash",
            {"command": "git status --short"},
        )
        risky_shell_decision = workspace_policy.decide_tool_call(
            "run_bash",
            {"command": "rm -rf build"},
        )
        reset_decision = workspace_policy.decide_tool_call(
            "run_bash",
            {"command": "git reset --hard"},
        )
        blocked_commands = [
            "rm -rf /",
            "rm -rf ~",
            "rm -rf *",
            "Remove-Item C:\\ -Recurse",
            "mkfs.ext4 /dev/sda",
            "dd if=/dev/zero of=/dev/sda",
        ]
        blocked_decisions = [
            workspace_policy.decide_tool_call("run_bash", {"command": command})
            for command in blocked_commands
        ]
        unknown_decision = workspace_policy.decide_tool_call("new_tool", {})

        full_access_policy = PermissionPolicy(mode="danger-full-access")
        full_access_decision = full_access_policy.decide_tool_call(
            "run_bash",
            {"command": "rm -rf build"},
        )
        full_access_blocked_decision = full_access_policy.decide_tool_call(
            "run_bash",
            {"command": "dd if=/dev/zero of=/dev/sda"},
        )

        self.assertTrue(read_decision.allowed)
        self.assertTrue(edit_decision.allowed)
        self.assertTrue(plan_decision.allowed)
        self.assertTrue(safe_shell_decision.allowed)
        self.assertTrue(risky_shell_decision.requires_approval)
        self.assertEqual(risky_shell_decision.risk, "shell_risky")
        self.assertTrue(reset_decision.requires_approval)
        self.assertEqual(reset_decision.risk, "shell_risky")
        for command, blocked_decision in zip(blocked_commands, blocked_decisions):
            with self.subTest(command=command):
                self.assertFalse(blocked_decision.allowed)
                self.assertFalse(blocked_decision.requires_approval)
                self.assertEqual(blocked_decision.risk, "shell_blocked")
        self.assertTrue(unknown_decision.requires_approval)
        self.assertTrue(full_access_decision.allowed)
        self.assertFalse(full_access_blocked_decision.allowed)
        self.assertEqual(full_access_blocked_decision.risk, "shell_blocked")

    def test_permission_policy_rejects_read_only_mode(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy

        with self.assertRaisesRegex(ValueError, "Unknown permission mode"):
            PermissionPolicy(mode="read-only")

    def test_permission_policy_rejects_unknown_mode_names(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy

        with self.assertRaises(ValueError):
            PermissionPolicy(mode="unsupported-mode")

    def test_interactive_session_switches_permission_mode_and_updates_context(self):
        from harness_code_agent.core.interactive import InteractiveSession
        from harness_code_agent.runtime.permissions import PermissionPolicy

        with tempfile.TemporaryDirectory() as tmp:
            session = InteractiveSession(cwd=tmp, profile_name="coding-agent")
            try:
                result = session.toggle_permission_mode()
                self.assertFalse(session.is_bound)

                session.ensure_profile_bound_for_first_task("inspect permissions")
                metadata = session.session_store.read_metadata(session.session.id)

                self.assertIn("workspace-write -> danger-full-access", result)
                self.assertEqual(session.permission_mode, "danger-full-access")
                self.assertEqual(metadata["permission_mode"], "danger-full-access")
                self.assertIsInstance(session.tool_context.permission_policy, PermissionPolicy)
                self.assertTrue(
                    session.tool_context.permission_policy.decide_tool_call(
                        "run_bash",
                        {"command": "rm -rf build"},
                    ).allowed
                )

                result = session.toggle_permission_mode()

                self.assertIn("danger-full-access -> workspace-write", result)
                self.assertEqual(session.permission_mode, "workspace-write")
            finally:
                session.close()

    def test_execute_tool_records_events_and_snapshots(self):
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("old", encoding="utf-8")
            events_path = root / ".harness" / "events.jsonl"
            workspace = WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots")
            context = ToolContext(
                workspace=workspace,
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            result = tools.execute_tool(
                "write_file",
                {"path": "note.txt", "content": "new"},
                tool_context=context,
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertIn("Wrote", result)
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "new")
            self.assertEqual(len(list((root / ".harness" / "snapshots").rglob("*.*"))), 1)
            event_types = [event["type"] for event in events]
            self.assertIn("tool_call", event_types)
            self.assertIn("tool_result", event_types)
            self.assertIn("file_change", event_types)

    def test_permission_middleware_denies_approval_and_emits_events(self):
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.runtime.permission_middleware import PermissionMiddleware
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )
            middleware = PermissionMiddleware(
                tool_context=context,
                tool_registry=tools.BUILTIN_TOOL_REGISTRY,
            )

            blocked = middleware.before_tool(
                "run_bash",
                {"command": "rm -rf build"},
                messages=[],
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertIn("[approval_denied]", blocked)
            event_types = [event["type"] for event in events]
            self.assertIn("approval_requested", event_types)
            self.assertIn("approval_decided", event_types)
            approval = [
                event for event in events
                if event["type"] == "approval_decided" and event["payload"].get("tool") == "run_bash"
            ][0]
            self.assertFalse(approval["payload"]["approved"])

    def test_permission_middleware_denial_in_agent_loop_emits_tool_result_and_failure_events(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation
        from harness_code_agent.runtime import tools
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.permission_middleware import PermissionMiddleware
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService

        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    message = SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="tc_shell",
                                type="function",
                                function=SimpleNamespace(
                                    name="run_bash",
                                    arguments='{"command":"rm -rf build"}',
                                ),
                            )
                        ],
                    )
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
                        usage=None,
                    )
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content="done", tool_calls=None),
                        finish_reason="stop",
                    )],
                    usage=None,
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(),
            )
            middleware = PermissionMiddleware(
                tool_context=context,
                tool_registry=tools.BUILTIN_TOOL_REGISTRY,
            )
            shell_schemas = tools.tool_schemas_for_profile(allowed_permissions={"shell"})

            with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
                conversation = AgentConversation(
                    Agent(
                        "main_agent",
                        "system",
                        use_tools=True,
                        tool_schemas=shell_schemas,
                        middlewares=[middleware],
                        tool_context=context,
                    )
                )
            with patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 2):
                conversation.run_until_idle()

            event_types = [event.type for event in context.event_bus.events]
            self.assertIn("approval_requested", event_types)
            self.assertIn("approval_decided", event_types)
            self.assertIn("tool_call", event_types)
            self.assertIn("tool_result", event_types)
            self.assertIn("failure", event_types)
            tool_result = [event for event in context.event_bus.events if event.type == "tool_result"][0]
            self.assertEqual(tool_result.payload["status"], "failed")
            self.assertEqual(tool_result.payload["metadata"]["status_source"], "approval")

    def test_agent_loop_blocks_tool_calls_not_advertised_in_schema(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation
        from harness_code_agent.runtime import tools
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService

        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    message = SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="tc_write",
                                type="function",
                                function=SimpleNamespace(
                                    name="write_file",
                                    arguments='{"path":"should_not_exist.txt","content":"bad"}',
                                ),
                            )
                        ],
                    )
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
                        usage=None,
                    )
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content="done", tool_calls=None),
                        finish_reason="stop",
                    )],
                    usage=None,
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(),
            )
            read_only_schemas = tools.tool_schemas_for_profile(allowed_permissions={"read"})
            with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
                conversation = AgentConversation(
                    Agent(
                        "consult_test",
                        "system",
                        use_tools=True,
                        tool_schemas=read_only_schemas,
                        tool_context=context,
                    )
                )
            with patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 2):
                conversation.run_until_idle()

            self.assertFalse((root / "should_not_exist.txt").exists())
            tool_result = [event for event in context.event_bus.events if event.type == "tool_result"][0]
            self.assertEqual(tool_result.payload["status"], "failed")
            self.assertEqual(tool_result.payload["metadata"]["status_source"], "permission")
            self.assertIn("not available", tool_result.payload["output"])

    def test_agent_loop_token_budget_fallback_blocks_pending_tool_calls(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation
        from harness_code_agent.runtime import tools
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService

        class FakeCompletions:
            def create(self, **kwargs):
                message = SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="tc_write",
                            type="function",
                            function=SimpleNamespace(
                                name="write_file",
                                arguments='{"path":"note.txt","content":"should not write"}',
                            ),
                        )
                    ],
                )
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
                    usage=SimpleNamespace(prompt_tokens=7, completion_tokens=5, total_tokens=12),
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="danger-full-access"),
                event_bus=EventBus(),
            )
            write_schemas = tools.tool_schemas_for_profile(allowed_permissions={"edit"})
            with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
                conversation = AgentConversation(
                    Agent(
                        "main_agent",
                        "system",
                        use_tools=True,
                        tool_schemas=write_schemas,
                        tool_context=context,
                    )
                )
            with (
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_TOTAL_TOKENS", 10),
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_TOOL_CALLS", 100),
                patch("harness_code_agent.agent.loop.context.count_tokens", return_value=1),
            ):
                text = conversation.run_until_idle()

            self.assertFalse((root / "note.txt").exists())
            self.assertIn("Agent fallback triggered", text)
            fallback = [event for event in context.event_bus.events if event.type == "agent_fallback"][0]
            self.assertEqual(fallback.payload["reason"], "token_budget_exceeded")
            self.assertEqual(fallback.payload["limit_type"], "total_tokens")
            tool_result = [event for event in context.event_bus.events if event.type == "tool_result"][0]
            self.assertEqual(tool_result.payload["status"], "failed")
            self.assertEqual(tool_result.payload["metadata"]["status_source"], "budget")

    def test_agent_loop_tool_call_budget_blocks_unexecuted_pending_calls(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation
        from harness_code_agent.runtime import tools
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService

        class FakeCompletions:
            def create(self, **kwargs):
                message = SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="tc_first",
                            type="function",
                            function=SimpleNamespace(
                                name="write_file",
                                arguments='{"path":"first.txt","content":"one"}',
                            ),
                        ),
                        SimpleNamespace(
                            id="tc_second",
                            type="function",
                            function=SimpleNamespace(
                                name="write_file",
                                arguments='{"path":"second.txt","content":"two"}',
                            ),
                        ),
                    ],
                )
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="danger-full-access"),
                event_bus=EventBus(),
            )
            write_schemas = tools.tool_schemas_for_profile(allowed_permissions={"edit"})
            with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
                conversation = AgentConversation(
                    Agent(
                        "main_agent",
                        "system",
                        use_tools=True,
                        tool_schemas=write_schemas,
                        tool_context=context,
                    )
                )
            with (
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_TOTAL_TOKENS", 100),
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_TOOL_CALLS", 1),
                patch("harness_code_agent.agent.loop.context.count_tokens", return_value=1),
            ):
                text = conversation.run_until_idle()

            self.assertTrue((root / "first.txt").exists())
            self.assertFalse((root / "second.txt").exists())
            self.assertIn("Agent fallback triggered", text)
            self.assertEqual(len([msg for msg in conversation.messages if msg.get("role") == "tool"]), 2)
            fallback = [event for event in context.event_bus.events if event.type == "agent_fallback"][0]
            self.assertEqual(fallback.payload["reason"], "tool_call_budget_exceeded")
            results = [event for event in context.event_bus.events if event.type == "tool_result"]
            self.assertEqual([event.payload["status"] for event in results], ["success", "failed"])
            self.assertEqual(results[-1].payload["metadata"]["status_source"], "budget")

    def test_agent_loop_max_iterations_emits_fallback_event(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation
        from harness_code_agent.runtime import tools
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService

        class FakeCompletions:
            def create(self, **kwargs):
                message = SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="tc_read",
                            type="function",
                            function=SimpleNamespace(
                                name="read_file",
                                arguments='{"path":"README.md"}',
                            ),
                        )
                    ],
                )
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("hello", encoding="utf-8")
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(),
            )
            read_schemas = tools.tool_schemas_for_profile(allowed_permissions={"read"})
            with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
                conversation = AgentConversation(
                    Agent(
                        "main_agent",
                        "system",
                        use_tools=True,
                        tool_schemas=read_schemas,
                        tool_context=context,
                    )
                )
            with (
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 1),
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_TOTAL_TOKENS", 100),
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_TOOL_CALLS", 100),
                patch("harness_code_agent.agent.loop.context.count_tokens", return_value=1),
            ):
                text = conversation.run_until_idle()

            self.assertIn("Agent fallback triggered", text)
            fallback = [event for event in context.event_bus.events if event.type == "agent_fallback"][0]
            self.assertEqual(fallback.payload["reason"], "max_iterations")
            self.assertEqual(fallback.payload["limit_type"], "iterations")

    def test_agent_loop_budget_warning_emits_once(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation
        from harness_code_agent.runtime import tools
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService

        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    message = SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="tc_read",
                                type="function",
                                function=SimpleNamespace(
                                    name="read_file",
                                    arguments='{"path":"README.md"}',
                                ),
                            )
                        ],
                    )
                else:
                    message = SimpleNamespace(content="done", tool_calls=None)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if self.calls == 1 else "stop")],
                    usage=SimpleNamespace(prompt_tokens=30, completion_tokens=30, total_tokens=60),
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("hello", encoding="utf-8")
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(),
            )
            read_schemas = tools.tool_schemas_for_profile(allowed_permissions={"read"})
            with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
                conversation = AgentConversation(
                    Agent(
                        "main_agent",
                        "system",
                        use_tools=True,
                        tool_schemas=read_schemas,
                        tool_context=context,
                    )
                )
            with (
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_TOTAL_TOKENS", 200),
                patch("harness_code_agent.agent.loop.config.MAX_AGENT_TOOL_CALLS", 100),
                patch("harness_code_agent.agent.loop.config.AGENT_BUDGET_WARN_FRACTION", 0.25),
                patch("harness_code_agent.agent.loop.context.count_tokens", return_value=1),
            ):
                conversation.run_until_idle()

            warnings = [event for event in context.event_bus.events if event.type == "agent_budget_warning"]
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0].payload["limit_type"], "total_tokens")

    def test_permission_middleware_blocks_blacklisted_shell_without_approval(self):
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.runtime.permission_middleware import PermissionMiddleware
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(),
            )
            middleware = PermissionMiddleware(
                tool_context=context,
                tool_registry=tools.BUILTIN_TOOL_REGISTRY,
            )

            blocked = middleware.before_tool(
                "run_bash",
                {"command": "rm -rf /"},
                messages=[],
                agent_name="main_agent",
            )

            self.assertIn("[blocked]", blocked)
            self.assertIn("blacklisted", blocked.lower())

    def test_env_shell_command_requires_approval(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy

        policy = PermissionPolicy(mode="workspace-write")
        decision = policy.decide_tool_call("run_bash", {"command": "env"})

        self.assertTrue(decision.requires_approval)
        self.assertEqual(decision.risk, "shell_risky")

    def test_execute_tool_apply_patch_records_snapshot_and_rejects_ambiguous_patch(self):
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("old\n", encoding="utf-8")
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            result = tools.execute_tool(
                "apply_patch",
                {"path": "note.txt", "search": "old\n", "replace": "new\n"},
                tool_context=context,
                agent_name="main_agent",
            )
            ambiguous = tools.execute_tool(
                "apply_patch",
                {"path": "note.txt", "search": "", "replace": "x"},
                tool_context=context,
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertIn("Patched note.txt", result)
            self.assertIn("[error] ValueError", ambiguous)
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "new\n")
            self.assertTrue(any(event["type"] == "file_change" for event in events))
            self.assertFalse(any(event["type"] == "file_changed" for event in events))

    def test_permission_middleware_allows_approved_tool_call(self):
        from harness_code_agent.runtime.approvals import StaticApprovalProvider
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.runtime.permission_middleware import PermissionMiddleware
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
                approval_provider=StaticApprovalProvider(approved=True, reason="test approval"),
            )
            middleware = PermissionMiddleware(
                tool_context=context,
                tool_registry=tools.BUILTIN_TOOL_REGISTRY,
            )

            result = middleware.before_tool(
                "run_bash",
                {"command": "npm run test"},
                messages=[],
                agent_name="main_agent",
            )
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertIsNone(result)
            requested = [event for event in events if event["type"] == "approval_requested"][0]
            decided = [event for event in events if event["type"] == "approval_decided"][0]
            self.assertEqual(requested["payload"]["tool"], "run_bash")
            self.assertEqual(requested["payload"]["risk"], "shell_risky")
            self.assertEqual(decided["payload"]["tool"], "run_bash")
            self.assertTrue(decided["payload"]["approved"])

    def test_static_verifier_passes_clean_python_file(self):
        import subprocess
        from harness_code_agent.runtime.middlewares import StaticVerifierMiddleware

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, capture_output=True)
            (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True)
            (root / "ok.py").write_text("x = 2\n", encoding="utf-8")

            mw = StaticVerifierMiddleware(workspace_root=str(root))
            result = mw.pre_exit(messages=[])

            self.assertIsNone(result)

    def test_static_verifier_ignores_preexisting_dirty_python_files(self):
        from harness_code_agent.runtime.middlewares import StaticVerifierMiddleware
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.py").write_text("def f(\n", encoding="utf-8")

            workspace = WorkspaceService(root=root)
            mw = StaticVerifierMiddleware(workspace_root=str(root), workspace=workspace)
            mw.begin_turn("task", messages=[])
            result = mw.pre_exit(messages=[])

            self.assertIsNone(result)

    def test_static_verifier_blocks_syntax_error_from_current_turn_workspace_change(self):
        from harness_code_agent.runtime.middlewares import StaticVerifierMiddleware
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceService(root=root)
            mw = StaticVerifierMiddleware(workspace_root=str(root), workspace=workspace)
            mw.begin_turn("task", messages=[])
            workspace.write_text("bad.py", "def f(\n")

            result = mw.pre_exit(messages=[])

            self.assertIsNotNone(result)
            self.assertIn("LINT CHECK FAILED", result)
            self.assertIn("bad.py", result)

    def test_static_verifier_warns_only_once_then_allows_exit(self):
        from harness_code_agent.runtime.middlewares import StaticVerifierMiddleware
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceService(root=root)
            mw = StaticVerifierMiddleware(workspace_root=str(root), workspace=workspace)
            mw.begin_turn("task", messages=[])
            workspace.write_text("warn.py", "x = 1\n")

            with patch(
                "harness_code_agent.runtime.middlewares._check_ruff_diff",
                return_value=[("warn.py", "W292", "no newline at end of file")],
            ):
                first = mw.pre_exit(messages=[])
                second = mw.pre_exit(messages=[])

            self.assertIsNotNone(first)
            self.assertIn("Lint warnings", first)
            self.assertIsNone(second)

    def test_static_verifier_skips_non_python_files(self):
        import subprocess
        from harness_code_agent.runtime.middlewares import StaticVerifierMiddleware

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=root, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True)
            (root / "data.json").write_text("{}", encoding="utf-8")

            mw = StaticVerifierMiddleware(workspace_root=str(root))
            result = mw.pre_exit(messages=[])

            self.assertIsNone(result)

    def test_static_verifier_ruff_diff_not_installed_gracefully_skips(self):
        from unittest.mock import patch as _patch
        from harness_code_agent.runtime.middlewares import _check_ruff_diff

        def fake_run(*a, **kw):
            raise FileNotFoundError

        with _patch("subprocess.run", side_effect=fake_run):
            result = _check_ruff_diff("/tmp")

        self.assertEqual(result, [])

    # ------------------------------------------------------------------
    # safe_args_preview
    # ------------------------------------------------------------------

    def test_safe_args_preview_masks_sensitive_fields(self):
        from harness_code_agent.agent.loop import safe_args_preview

        result = safe_args_preview({"path": "x.py", "content": "print('hello' * 999)"})
        self.assertNotIn("print", result)
        self.assertIn("chars", result)
        self.assertIn("path", result)

        result2 = safe_args_preview({"path": "x.py", "patch": "+def foo():\n    pass"})
        self.assertNotIn("def foo", result2)
        self.assertIn("chars", result2)

    def test_safe_args_preview_handles_large_fields(self):
        from harness_code_agent.agent.loop import safe_args_preview

        large = "x" * 500
        result = safe_args_preview({"key": large})
        self.assertIn("chars", result)
        # JSON-serialized string includes quotes, so the char count is 500 + 2
        self.assertIn("502 chars", result)

    def test_safe_args_preview_sorts_keys_stably(self):
        from harness_code_agent.agent.loop import safe_args_preview

        # Call multiple times — result should be identical
        args = {"z": 3, "a": 1, "path": "f.py"}
        r1 = safe_args_preview(args)
        r2 = safe_args_preview(args)
        self.assertEqual(r1, r2)
        # 'a' should come before 'z'
        self.assertLess(r1.index("a"), r1.index("z"))

    def test_safe_args_preview_respects_max_chars(self):
        from harness_code_agent.agent.loop import safe_args_preview

        args = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6}
        result = safe_args_preview(args, max_chars=30)
        self.assertLessEqual(len(result), 33)  # 30 + "..."

    def test_safe_args_preview_redacts_sensitive_key_names(self):
        from harness_code_agent.agent.loop import safe_args_preview

        result = safe_args_preview({
            "path": "x.py",
            "api_key": "sk-1234567890",
            "jwt": "header.payload.signature",
            "token": "short-secret",
        })

        self.assertIn('"api_key": "[redacted]"', result)
        self.assertIn('"jwt": "[redacted]"', result)
        self.assertIn('"token": "[redacted]"', result)
        self.assertNotIn("short-secret", result)

    def test_loop_detection_preview_does_not_import_agent_loop(self):
        middleware_source = Path("harness_code_agent/runtime/middlewares.py").read_text(encoding="utf-8")

        self.assertNotIn("agent.loop", middleware_source)

    # ------------------------------------------------------------------
    # _file_warned reset per turn
    # ------------------------------------------------------------------

    def test_loop_detection_file_warned_resets_per_turn(self):
        from harness_code_agent.runtime.middlewares import LoopDetectionMiddleware

        mw = LoopDetectionMiddleware(file_edit_threshold=2)

        # First turn — writes to file twice, triggers warning
        mw.begin_turn("task 1", [])
        self.assertEqual(len(mw._file_warned), 0)

        mw.post_tool("write_file", {"path": "a.py", "content": "x"}, "ok", [])
        mw.post_tool("write_file", {"path": "a.py", "content": "y"}, "ok", [])
        self.assertIn("a.py", mw._file_warned)

        # Next turn — _file_warned is cleared, same file triggers warning again
        mw.begin_turn("task 2", [])
        self.assertEqual(len(mw._file_warned), 0)

        mw.post_tool("write_file", {"path": "a.py", "content": "z"}, "ok", [])
        mw.post_tool("write_file", {"path": "a.py", "content": "w"}, "ok", [])
        self.assertIn("a.py", mw._file_warned)


if __name__ == "__main__":
    unittest.main()
