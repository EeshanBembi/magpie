from __future__ import annotations

import json

import httpx
import pytest

from magpie.llm import (
    AnthropicProvider,
    FakeProvider,
    LLMError,
    OllamaProvider,
    OpenAIProvider,
    get_provider,
)


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fake_provider_is_deterministic_and_streams_the_same_answer():
    provider = get_provider({"provider": "fake"})
    assert isinstance(provider, FakeProvider)

    messages = [{"role": "user", "content": "What did I work on?"}]
    answer = provider.chat(messages)
    assert "What did I work on?" in answer
    assert "[1]" in answer
    assert provider.chat(messages) == answer

    chunks = list(provider.chat(messages, stream=True))
    assert len(chunks) > 1
    assert "".join(chunks) == answer


def test_fake_provider_accepts_a_fixed_answer():
    provider = FakeProvider("Custom memory answer [1]")
    assert provider.chat([{"role": "user", "content": "ignored"}]) == (
        "Custom memory answer [1]"
    )
    assert "".join(provider.chat([], stream=True)) == "Custom memory answer [1]"


def test_unknown_provider_has_clear_error():
    with pytest.raises(LLMError, match="Unknown LLM provider"):
        get_provider({"provider": "mystery"})


def test_get_provider_dispatch_and_defaults():
    assert isinstance(get_provider({"provider": "ollama"}), OllamaProvider)
    assert isinstance(get_provider({"provider": "openai"}), OpenAIProvider)
    assert isinstance(get_provider({"provider": "anthropic"}), AnthropicProvider)
    ollama = get_provider({"provider": "ollama"})
    assert ollama.base_url == "http://localhost:11434"
    assert ollama.model == "llama3.2:1b"

    openai = get_provider({"provider": "openai"})
    assert openai.model == "gpt-4o-mini"
    anthropic = get_provider({"provider": "anthropic"})
    assert anthropic.model == "claude-sonnet-5-5"


def test_openai_non_streaming_request_and_response():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Hello from GPT"}}]}
        )

    config = {
        "provider": "openai",
        "providers": {
            "openai": {
                "base_url": "https://api.example.test",
                "api_key": "secret-key",
                "model": "gpt-test",
            }
        },
    }
    provider = get_provider(config, client=client_for(handler))
    answer = provider.chat([{"role": "user", "content": "hi"}])

    assert answer == "Hello from GPT"
    assert seen["url"] == "https://api.example.test/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key"
    assert seen["body"]["model"] == "gpt-test"
    assert seen["body"]["stream"] is False


def test_openai_base_url_already_ending_in_v1_is_not_doubled():
    def handler(request):
        assert str(request.url) == "https://api.example.test/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = get_provider(
        {
            "provider": "openai",
            "providers": {
                "openai": {"base_url": "https://api.example.test/v1", "model": "m"}
            },
        },
        transport=httpx.MockTransport(handler),
    )
    assert provider.chat([{"role": "user", "content": "hi"}]) == "ok"
    provider.close()


def test_openai_streaming_chunks():
    body = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request):
        payload = json.loads(request.content)
        assert payload["stream"] is True
        return httpx.Response(200, text=body)

    provider = get_provider(
        {
            "provider": "openai",
            "providers": {
                "openai": {"base_url": "https://api.example.test", "model": "m"}
            },
        },
        client=client_for(handler),
    )
    chunks = list(provider.chat([{"role": "user", "content": "hi"}], stream=True))
    assert chunks == ["Hel", "lo"]


def test_anthropic_non_streaming_splits_system_and_merges_text_blocks():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["version"] = request.headers.get("anthropic-version")
        seen["api_key"] = request.headers.get("x-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "Hi "},
                    {"type": "text", "text": "there"},
                ]
            },
        )

    config = {
        "provider": "anthropic",
        "providers": {
            "anthropic": {"api_key": "ant-key", "model": "claude-test"}
        },
    }
    provider = get_provider(config, client=client_for(handler))
    answer = provider.chat(
        [
            {"role": "system", "content": "You are Magpie."},
            {"role": "user", "content": "hello"},
        ]
    )

    assert answer == "Hi there"
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["version"] == "2023-06-01"
    assert seen["api_key"] == "ant-key"
    assert seen["body"]["system"] == "You are Magpie."
    assert seen["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert seen["body"]["max_tokens"] == 1024


def test_anthropic_streaming_text_deltas():
    body = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"m"}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":'
        '{"type":"text_delta","text":"Hello"}}\n\n'
        'data: {"type":"content_block_delta","delta":'
        '{"type":"text_delta","text":"!"}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )

    provider = get_provider(
        {
            "provider": "anthropic",
            "providers": {"anthropic": {"api_key": "k", "model": "m"}},
        },
        client=client_for(lambda request: httpx.Response(200, text=body)),
    )
    chunks = list(provider.chat([{"role": "user", "content": "hi"}], stream=True))
    assert "".join(chunks) == "Hello!"


def test_ollama_non_streaming():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "local answer"}})

    provider = get_provider({"provider": "ollama"}, client=client_for(handler))
    answer = provider.chat([{"role": "user", "content": "hi"}])

    assert answer == "local answer"
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["body"]["stream"] is False


def test_ollama_streaming_ndjson():
    body = (
        '{"message":{"content":"loc"},"done":false}\n'
        '{"message":{"content":"al"},"done":false}\n'
        '{"message":{"content":""},"done":true}\n'
    )

    provider = get_provider(
        {"provider": "ollama"},
        client=client_for(lambda request: httpx.Response(200, text=body)),
    )
    chunks = list(provider.chat([{"role": "user", "content": "hi"}], stream=True))
    assert "".join(chunks) == "local"


def test_http_error_is_a_clear_llm_error():
    provider = get_provider(
        {
            "provider": "openai",
            "providers": {"openai": {"base_url": "https://x.test", "model": "m"}},
        },
        client=client_for(
            lambda request: httpx.Response(500, text="upstream exploded")
        ),
    )
    with pytest.raises(LLMError, match="HTTP 500"):
        provider.chat([{"role": "user", "content": "hi"}])


def test_connection_error_is_a_clear_llm_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    provider = get_provider(
        {
            "provider": "ollama",
            "providers": {"ollama": {"base_url": "http://127.0.0.1:1"}},
        },
        client=client_for(handler),
    )
    with pytest.raises(LLMError, match="Could not reach ollama"):
        provider.chat([{"role": "user", "content": "hi"}])
