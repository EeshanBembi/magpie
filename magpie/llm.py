"""Provider abstraction for local and cloud language models.

``get_provider(config)`` returns an object whose ``chat(messages, stream=False)``
method either returns the full answer as a string or an iterator of text
chunks.  The default provider is Ollama; OpenAI-compatible endpoints and
Anthropic are supported, and a deterministic ``fake`` provider keeps the test
suite offline.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

import httpx

from . import config as config_module

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 1024

DEFAULT_BASE_URLS = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
}
DEFAULT_MODELS = {
    "ollama": "llama3.2:1b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-5-5",
}


class LLMError(RuntimeError):
    """A provider could not be reached or returned an unusable response."""


def _join_url(base_url: str, path: str) -> str:
    base = (base_url or "").rstrip("/")
    if base.endswith("/v1") and path.startswith("/v1/"):
        base = base[:-3]
    return f"{base}{path}"


def _normalise_messages(messages: Iterable[Any]) -> list[dict[str, str]]:
    normalised: list[dict[str, str]] = []
    for message in messages or []:
        if isinstance(message, dict):
            role = str(message.get("role") or "user")
            content = message.get("content", "")
        else:
            role = "user"
            content = message
        if isinstance(content, list):
            pieces: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    pieces.append(str(part.get("text") or ""))
                else:
                    pieces.append(str(part))
            content = "".join(pieces)
        normalised.append({"role": role, "content": str(content)})
    return normalised


def _chunks(text: str, size: int = 24) -> Iterator[str]:
    if not text:
        return
    for index in range(0, len(text), size):
        yield text[index : index + size]


class LLMProvider:
    """Base class for the provider implementations."""

    name = "provider"

    def chat(self, messages: Iterable[Any], stream: bool = False) -> str | Iterator[str]:
        raise LLMError(f"{type(self).__name__} does not implement chat()")

    def close(self) -> None:  # pragma: no cover - overridden where needed
        return None

    def __enter__(self) -> "LLMProvider":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class FakeProvider(LLMProvider):
    """Deterministic offline provider used by tests and demos."""

    name = "fake"

    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer

    def _build_answer(self, messages: Iterable[Any]) -> str:
        if self.answer is not None:
            return self.answer
        normalised = _normalise_messages(messages)
        question = ""
        for message in reversed(normalised):
            if message["role"] == "user":
                question = message["content"].strip()
                break
        if question:
            return f"Fake answer about: {question} [1]"
        return "Fake answer from Magpie [1]"

    def chat(
        self, messages: Iterable[Any], stream: bool = False
    ) -> str | Iterator[str]:
        answer = self._build_answer(messages)
        if stream:
            return _chunks(answer)
        return answer


class HTTPProvider(LLMProvider):
    """Shared HTTP plumbing: JSON requests, streaming lines and clear errors."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str = "",
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key or ""
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                timeout=self.timeout,
                transport=transport,
            )
            self._owns_client = True

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- request helpers ----------------------------------------------

    def _unreachable(self, url: str, exc: Exception) -> LLMError:
        return LLMError(
            f"Could not reach {self.name} at {url}: {exc}. "
            "Check that the model server is running and that the base URL is correct."
        )

    def _bad_response(self, url: str, response: httpx.Response) -> LLMError:
        body = (response.text or "").strip().replace("\n", " ")
        if len(body) > 300:
            body = body[:300] + "…"
        detail = f": {body}" if body else ""
        return LLMError(
            f"{self.name} returned HTTP {response.status_code} from {url}{detail}"
        )

    def _post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        try:
            response = self._client.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.RequestError as exc:
            raise self._unreachable(url, exc) from exc

        if response.status_code >= 400:
            raise self._bad_response(url, response)
        try:
            data = response.json()
        except ValueError as exc:
            body = (response.text or "").strip()[:200]
            raise LLMError(
                f"{self.name} returned invalid JSON from {url}: {body}"
            ) from exc
        if not isinstance(data, dict):
            raise LLMError(f"{self.name} returned an unexpected response from {url}")
        if data.get("error"):
            error = data["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise LLMError(f"{self.name} error: {message}")
        return data

    def _stream_lines(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> Iterator[str]:
        try:
            with self._client.stream(
                "POST",
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            ) as response:
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", "replace")
                    raise LLMError(
                        f"{self.name} returned HTTP {response.status_code} from "
                        f"{url}: {body.strip()[:300]}"
                    )
                yield from response.iter_lines()
        except httpx.RequestError as exc:
            raise self._unreachable(url, exc) from exc


class OllamaProvider(HTTPProvider):
    name = "ollama"

    def chat(
        self, messages: Iterable[Any], stream: bool = False
    ) -> str | Iterator[str]:
        if stream:
            return self._chat_stream(messages)
        return self._chat_once(messages)

    def _payload(self, messages: Iterable[Any], stream: bool) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": _normalise_messages(messages),
            "stream": stream,
        }

    def _chat_once(self, messages: Iterable[Any]) -> str:
        url = _join_url(self.base_url, "/api/chat")
        data = self._post_json(url, self._payload(messages, False), {})
        message = data.get("message")
        if isinstance(message, dict) and "content" in message:
            return str(message.get("content") or "")
        return str(data.get("response") or "")

    def _chat_stream(self, messages: Iterable[Any]) -> Iterator[str]:
        url = _join_url(self.base_url, "/api/chat")
        for line in self._stream_lines(url, self._payload(messages, True), {}):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LLMError(
                    f"ollama sent invalid streaming JSON: {line.strip()[:200]}"
                ) from exc
            if not isinstance(data, dict):
                continue
            if data.get("error"):
                raise LLMError(f"ollama error: {data['error']}")
            message = data.get("message")
            content = ""
            if isinstance(message, dict):
                content = str(message.get("content") or "")
            if not content:
                content = str(data.get("response") or "")
            if content:
                yield content
            if data.get("done"):
                break


class OpenAIProvider(HTTPProvider):
    name = "openai"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, messages: Iterable[Any], stream: bool) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": _normalise_messages(messages),
            "stream": stream,
        }

    def chat(
        self, messages: Iterable[Any], stream: bool = False
    ) -> str | Iterator[str]:
        if stream:
            return self._chat_stream(messages)
        return self._chat_once(messages)

    def _chat_once(self, messages: Iterable[Any]) -> str:
        url = _join_url(self.base_url, "/v1/chat/completions")
        data = self._post_json(url, self._payload(messages, False), self._headers())
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(
                "openai returned no choices; the response did not look like a "
                "chat completion"
            )
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict):
            return str(message.get("content") or "")
        return str(choice.get("text") or "")

    def _chat_stream(self, messages: Iterable[Any]) -> Iterator[str]:
        url = _join_url(self.base_url, "/v1/chat/completions")
        headers = self._headers()
        for line in self._stream_lines(url, self._payload(messages, True), headers):
            text = line.strip()
            if not text or text.startswith(":"):
                continue
            if text.startswith("data:"):
                text = text[5:].strip()
            if text == "[DONE]":
                break
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise LLMError(
                    f"openai sent invalid streaming JSON: {line.strip()[:200]}"
                ) from exc
            if not isinstance(data, dict):
                continue
            if data.get("error"):
                error = data["error"]
                message = error.get("message") if isinstance(error, dict) else error
                raise LLMError(f"openai error: {message}")
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") if isinstance(choice, dict) else None
            content = ""
            if isinstance(delta, dict):
                content = str(delta.get("content") or "")
            if not content and isinstance(choice.get("message"), dict):
                content = str(choice["message"].get("content") or "")
            if content:
                yield content


class AnthropicProvider(HTTPProvider):
    name = "anthropic"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _payload(self, messages: Iterable[Any], stream: bool) -> dict[str, Any]:
        system_parts: list[str] = []
        converted: list[dict[str, str]] = []
        for message in _normalise_messages(messages):
            if message["role"] == "system":
                system_parts.append(message["content"])
            else:
                role = "assistant" if message["role"] == "assistant" else "user"
                converted.append({"role": role, "content": message["content"]})
        if not converted:
            converted.append({"role": "user", "content": ""})

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": converted,
            "stream": stream,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        return payload

    def chat(
        self, messages: Iterable[Any], stream: bool = False
    ) -> str | Iterator[str]:
        if stream:
            return self._chat_stream(messages)
        return self._chat_once(messages)

    def _chat_once(self, messages: Iterable[Any]) -> str:
        url = _join_url(self.base_url, "/v1/messages")
        data = self._post_json(url, self._payload(messages, False), self._headers())
        return self._extract_text(data)

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        content = data.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type", "text") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)

    def _chat_stream(self, messages: Iterable[Any]) -> Iterator[str]:
        url = _join_url(self.base_url, "/v1/messages")
        headers = self._headers()
        for line in self._stream_lines(url, self._payload(messages, True), headers):
            text = line.strip()
            if not text or text.startswith(":"):
                continue
            if text.startswith(("event:", "id:", "retry:")):
                continue
            if text.startswith("data:"):
                text = text[5:].strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise LLMError(
                    f"anthropic sent invalid streaming JSON: {line.strip()[:200]}"
                ) from exc
            if not isinstance(data, dict):
                continue
            kind = data.get("type")
            if kind == "error":
                error = data.get("error")
                message = error.get("message") if isinstance(error, dict) else error
                raise LLMError(f"anthropic error: {message}")
            if kind == "content_block_delta":
                delta = data.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    chunk = str(delta.get("text") or "")
                    if chunk:
                        yield chunk
            elif kind == "message_stop":
                break


def _profile_for(
    config: dict[str, Any], provider_name: str
) -> dict[str, Any]:
    defaults = config_module.DEFAULTS.get("providers", {})
    base = defaults.get(provider_name, {})
    profile = dict(base) if isinstance(base, dict) else {}
    providers = config.get("providers")
    if isinstance(providers, dict):
        selected = providers.get(provider_name)
        if isinstance(selected, dict):
            profile.update(selected)
    # Also accept a flat provider profile, which is convenient for snippets and
    # for tests that do not want to build the full nested config.
    for key in ("base_url", "model", "api_key", "max_tokens", "answer"):
        if key in config:
            profile[key] = config[key]
    return profile


def get_provider(
    config: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    provider: str | None = None,
    client: httpx.Client | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> LLMProvider:
    """Build the configured provider.

    ``client`` or ``transport`` may be supplied for tests; nothing here performs
    a network request until ``chat`` is called.
    """

    cfg = config if config is not None else config_module.load_config()
    if not isinstance(cfg, dict):
        cfg = {}
    provider_name = str(
        name or provider or cfg.get("provider") or cfg.get("name") or "ollama"
    ).strip().lower()

    if provider_name == "fake":
        profile = _profile_for(cfg, "fake")
        answer = profile.get("answer")
        return FakeProvider(str(answer) if answer is not None else None)

    if provider_name not in ("ollama", "openai", "anthropic"):
        raise LLMError(
            f"Unknown LLM provider {provider_name!r}; choose ollama, openai, "
            "anthropic or fake."
        )

    profile = _profile_for(cfg, provider_name)
    base_url = str(
        profile.get("base_url") or DEFAULT_BASE_URLS.get(provider_name, "")
    )
    model = str(profile.get("model") or DEFAULT_MODELS.get(provider_name, ""))
    api_key = str(profile.get("api_key") or "")
    max_tokens = int(profile.get("max_tokens") or DEFAULT_MAX_TOKENS)

    common: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "client": client,
        "transport": transport,
        "timeout": timeout,
        "max_tokens": max_tokens,
    }
    if provider_name == "ollama":
        return OllamaProvider(**common)
    if provider_name == "openai":
        return OpenAIProvider(**common)
    return AnthropicProvider(**common)
