"""Run with AstrBot on PYTHONPATH and a disposable ASTRBOT_ROOT."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.provider import DecisionChoiceAnswer, ProviderType
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.provider.decision import DecisionService
from astrbot.core.provider.sources.system_one_decision_source import (
    SystemOneDecisionProvider,
)

PLUGIN_PATH = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "thought_guard_under_test", PLUGIN_PATH / "main.py"
)
plugin_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin_module)


def make_guard(provider):
    manager = SimpleNamespace(
        get_using_provider_async=AsyncMock(return_value=provider),
        get_provider_by_id=AsyncMock(side_effect=AssertionError("No plugin override")),
    )
    context = SimpleNamespace(
        decision=DecisionService(manager),
        get_current_chat_provider_id=AsyncMock(return_value="chat-for-session"),
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(
                completion_text="<think>discard this</think>Final answer."
            )
        ),
    )
    schema = json.loads((PLUGIN_PATH / "_conf_schema.json").read_text("utf-8"))
    config = {key: item["default"] for key, item in schema.items()}
    return plugin_module.ThoughtGuard(context, config), context, manager


def make_event(text):
    result = MessageEventResult(
        chain=[At(qq=123), Plain(text), Image.fromURL("https://images.invalid/a.png")],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    event = SimpleNamespace(
        unified_msg_origin="webchat:FriendMessage:test-session",
        get_result=lambda: result,
    )
    return event, result


@pytest.mark.asyncio
async def test_default_decision_switch_controls_cleaning_and_preserves_components():
    requests = []

    def respond(request):
        requests.append(request)
        probability = 0.1 if request.url.host == "first.invalid" else 0.75
        return httpx.Response(
            200, json={"answers": {"leaks_reasoning": {"noul": probability}}}
        )

    providers = [
        SystemOneDecisionProvider(
            {
                "api_base": f"https://{name}.invalid/v1",
                "model": name,
                "key": "core-key",
            },
            {},
            transport=httpx.MockTransport(respond),
        )
        for name in ("first", "second")
    ]
    guard, context, manager = make_guard(providers[0])
    guard.config.update(
        threshold=0.75,
        max_check_chars=12,
        jev_base_url="https://obsolete.invalid",
        jev_api_key="obsolete-key",
        jev_model="obsolete-model",
    )
    text = "Internal reasoning. Final answer."
    event, result = make_event(text)
    original_chain = list(result.chain)
    try:
        await guard.guard(event)
        assert result.chain == original_chain
        context.llm_generate.assert_not_awaited()

        manager.get_using_provider_async.return_value = providers[1]
        await guard.guard(event)

        assert result.chain == [
            original_chain[0],
            Plain("Final answer."),
            original_chain[2],
        ]
        context.get_current_chat_provider_id.assert_awaited_once_with(
            event.unified_msg_origin
        )
        context.llm_generate.assert_awaited_once_with(
            chat_provider_id="chat-for-session",
            system_prompt=guard.config["cleaner_system_prompt"],
            prompt=f"<reply>\n{text}\n</reply>",
        )
        assert manager.get_using_provider_async.await_count == 2
        manager.get_using_provider_async.assert_awaited_with(ProviderType.DECISION)
        assert [json.loads(request.content)["model"] for request in requests] == [
            "first",
            "second",
        ]
        for request in requests:
            payload = json.loads(request.content)
            assert request.url.path == "/v1/systemone"
            assert request.headers["Authorization"] == "Bearer core-key"
            assert payload["state"] == text[:12]
            assert payload["questions"]["leaks_reasoning"] == {
                "type": "noul",
                "instructions": guard.config["detect_instructions"],
            }
    finally:
        for provider in providers:
            await provider.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["unconfigured", "http_error", "invalid_response", "wrong_answer_type"]
)
async def test_decision_failure_keeps_original_reply(failure):
    def respond(request):
        if failure == "http_error":
            return httpx.Response(503)
        return httpx.Response(200, json={"answers": {"leaks_reasoning": {"noul": 2}}})

    provider = None
    if failure != "unconfigured":
        provider = SystemOneDecisionProvider(
            {"api_base": "https://decision.invalid/v1"},
            {},
            transport=httpx.MockTransport(respond),
        )
        if failure == "wrong_answer_type":
            provider.ask = AsyncMock(
                return_value={
                    "leaks_reasoning": DecisionChoiceAnswer(
                        choice="yes",
                        probability=1,
                        probabilities={"yes": 1},
                        confidence=1,
                    )
                }
            )
    guard, context, _ = make_guard(provider)
    event, result = make_event("Original reply.")
    original_chain = list(result.chain)
    try:
        await guard.guard(event)
        assert result.chain == original_chain
        context.llm_generate.assert_not_awaited()
    finally:
        if provider is not None:
            await provider.terminate()
