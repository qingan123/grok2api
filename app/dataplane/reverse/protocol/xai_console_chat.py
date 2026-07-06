"""console.x.ai Responses API protocol helpers for high-effort chat models."""

from typing import Any, AsyncGenerator

import orjson

from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger


CONSOLE_MODELS: dict[str, str] = {
    "grok-4.3": "grok-4.3",
    "grok-4.3-high": "grok-4.3",
    "grok-4.20-multi-agent-high": "grok-4.20-multi-agent-0309",
}

_FIXED_EFFORT: dict[str, str] = {
    "grok-4.3": "high",
    "grok-4.3-high": "high",
    "grok-4.20-multi-agent-high": "high",
}

_MODELS_WITH_REASONING: frozenset[str] = frozenset({
    "grok-4.3",
    "grok-4.20-multi-agent-0309",
})

_MODELS_WITH_SEARCH_TOOLS: frozenset[str] = frozenset({
    "grok-4.3",
    "grok-4.20-multi-agent-0309",
})

_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "grok-4.20-multi-agent-0309": 2_000_000,
}

_EFFORT_MAP: dict[str, str] = {
    "none": "none",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
}


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "input_text", "text": str(content)}]

    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            blocks.append({"type": "input_text", "text": block.get("text", "")})
        elif block_type == "image_url":
            url = (block.get("image_url") or {}).get("url", "")
            if url:
                blocks.append({"type": "input_image", "image_url": url})
        else:
            text = block.get("text") or str(block)
            blocks.append({"type": "input_text", "text": text})
    return blocks


def build_console_payload(
    *,
    messages: list[dict[str, Any]],
    model: str,
    temperature: float = 0.7,
    top_p: float = 0.95,
    reasoning_effort: str | None = None,
    stream: bool = True,
) -> dict[str, Any]:
    """Build a console.x.ai /v1/responses payload from OpenAI chat messages."""
    console_model = CONSOLE_MODELS.get(model, model)
    effort = _FIXED_EFFORT.get(model) or _EFFORT_MAP.get(
        reasoning_effort or "medium",
        "medium",
    )

    input_items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        if role in {"system", "developer"}:
            api_role = "system"
        elif role == "assistant":
            api_role = "assistant"
        else:
            api_role = "user"

        blocks = _content_blocks(message.get("content", ""))
        if blocks:
            input_items.append({"role": api_role, "content": blocks})

    payload: dict[str, Any] = {
        "model": console_model,
        "input": input_items,
        "max_output_tokens": _MAX_OUTPUT_TOKENS.get(console_model, 1_000_000),
        "temperature": temperature,
        "top_p": top_p,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "stream": stream,
    }
    if console_model in _MODELS_WITH_REASONING:
        payload["reasoning"] = {"effort": effort}
    if console_model in _MODELS_WITH_SEARCH_TOOLS:
        payload["tools"] = [
            {"type": "web_search", "enable_image_understanding": True},
            {"type": "x_search", "enable_video_understanding": True},
        ]
        payload["tool_choice"] = "auto"

    logger.debug(
        "console payload built: public_model={} console_model={} items={} effort={}",
        model,
        console_model,
        len(input_items),
        effort,
    )
    return payload


class ConsoleStreamAdapter:
    """Collect response.output_text.delta tokens and final usage."""

    __slots__ = ("text_buf", "usage", "_done")

    def __init__(self) -> None:
        self.text_buf: list[str] = []
        self.usage: dict[str, Any] | None = None
        self._done = False

    def feed(self, event_type: str, data: str) -> list[str]:
        if self._done:
            return []
        try:
            obj = orjson.loads(data)
        except (orjson.JSONDecodeError, ValueError):
            return []

        if event_type == "response.output_text.delta":
            delta = obj.get("delta", "")
            if delta:
                self.text_buf.append(delta)
                return [delta]
        elif event_type == "response.completed":
            response = obj.get("response", {})
            if isinstance(response, dict):
                usage = response.get("usage")
                self.usage = usage if isinstance(usage, dict) else None
            self._done = True
        elif event_type == "error":
            message = obj.get("message") or str(obj)
            raise UpstreamError(f"Console API error: {message}", status=502)

        return []

    @property
    def full_text(self) -> str:
        return "".join(self.text_buf)


def classify_console_line(line: str | bytes) -> tuple[str, str]:
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.strip()
    if not line:
        return "skip", ""
    if line.startswith("event:"):
        return "event", line[6:].strip()
    if line.startswith("data:"):
        data = line[5:].strip()
        if data == "[DONE]":
            return "done", ""
        return "data", data
    return "skip", ""


async def stream_console_chat(
    token: str,
    payload: dict[str, Any],
    *,
    timeout_s: float = 120.0,
) -> AsyncGenerator[tuple[str, str], None]:
    from app.dataplane.proxy import get_proxy_runtime
    from app.dataplane.proxy.adapters.headers import build_console_headers
    from app.dataplane.proxy.adapters.session import (
        ResettableSession,
        build_session_kwargs,
    )
    from app.dataplane.reverse.runtime.endpoint_table import CONSOLE_RESPONSES

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()

    headers = build_console_headers(token, lease=lease)
    session_kwargs = build_session_kwargs(lease=lease)

    async with ResettableSession(**session_kwargs) as session:
        try:
            response = await session.post(
                CONSOLE_RESPONSES,
                headers=headers,
                data=orjson.dumps(payload),
                timeout=timeout_s,
                stream=True,
            )
        except Exception as exc:
            raise UpstreamError(f"Console transport failed: {exc}", status=502) from exc

        if response.status_code != 200:
            try:
                body = response.content.decode("utf-8", "replace")[:400]
            except Exception:
                body = ""
            raise UpstreamError(
                f"Console API returned {response.status_code}",
                status=response.status_code,
                body=body,
            )

        current_event = ""
        try:
            async for raw_line in response.aiter_lines():
                kind, value = classify_console_line(raw_line)
                if kind == "event":
                    current_event = value
                elif kind == "data":
                    yield current_event, value
                    current_event = ""
                elif kind == "done":
                    return
        except Exception as exc:
            raise UpstreamError(f"Console stream read failed: {exc}", status=502) from exc


__all__ = [
    "CONSOLE_MODELS",
    "ConsoleStreamAdapter",
    "build_console_payload",
    "classify_console_line",
    "stream_console_chat",
]
