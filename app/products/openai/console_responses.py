"""OpenAI Responses API adapter for console.x.ai high-effort models."""

import asyncio
from typing import Any, AsyncGenerator

from app.control.account.enums import FeedbackKind
from app.control.account.invalid_credentials import feedback_kind_for_error
from app.control.model.registry import resolve as resolve_model
from app.dataplane.reverse.protocol.xai_console_chat import (
    ConsoleStreamAdapter,
    build_console_payload,
    stream_console_chat,
)
from app.platform.config.snapshot import get_config
from app.platform.errors import RateLimitError, UpstreamError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_s
from app.platform.tokens import estimate_prompt_tokens, estimate_tokens
from app.products._account_selection import console_selection_max_retries
from app.products.openai.chat import (
    _configured_retry_codes,
    _fail_sync,
    _log_task_exception,
    _quota_sync,
    _should_retry_upstream,
)
from app.products.openai._format import (
    build_resp_usage,
    format_sse,
    make_resp_id,
    make_resp_object,
)


async def create(
    *,
    model: str,
    input_val: str | list,
    instructions: str | None,
    stream: bool,
    reasoning_effort: str | None,
    temperature: float,
    top_p: float,
) -> dict | AsyncGenerator[str, None]:
    cfg = get_config()
    spec = resolve_model(model)
    timeout_s = cfg.get_float("chat.timeout", 120.0)
    max_retries = console_selection_max_retries()
    retry_codes = _configured_retry_codes(cfg)
    response_id = make_resp_id("resp")
    message_id = make_resp_id("msg")
    messages = _messages_from_input(input_val, instructions)

    from app.dataplane.account import _directory as _acct_dir

    if _acct_dir is None:
        raise RateLimitError("Account directory not initialised")
    directory = _acct_dir

    if stream:

        async def _run_stream() -> AsyncGenerator[str, None]:
            excluded: list[str] = []
            for attempt in range(max_retries + 1):
                acct = await directory.reserve_any(
                    spec.pool_candidates(),
                    now_s_override=now_s(),
                    exclude_tokens=excluded or None,
                )
                if acct is None:
                    raise RateLimitError("No available accounts for this model tier")
                selected_mode_id = acct.mode_id

                token = acct.token
                success = False
                retry = False
                fail_exc: BaseException | None = None
                adapter = ConsoleStreamAdapter()
                output_started = False

                try:
                    payload = build_console_payload(
                        messages=messages,
                        model=model,
                        temperature=temperature,
                        top_p=top_p,
                        reasoning_effort=reasoning_effort,
                        stream=True,
                    )
                    try:
                        yield format_sse(
                            "response.created",
                            {
                                "type": "response.created",
                                "response": make_resp_object(
                                    response_id,
                                    model,
                                    "in_progress",
                                    [],
                                ),
                            },
                        )
                        async for event_type, data in stream_console_chat(
                            token,
                            payload,
                            timeout_s=timeout_s,
                        ):
                            for token_text in adapter.feed(event_type, data):
                                if not output_started:
                                    output_started = True
                                    yield format_sse(
                                        "response.output_item.added",
                                        {
                                            "type": "response.output_item.added",
                                            "output_index": 0,
                                            "item": {
                                                "id": message_id,
                                                "type": "message",
                                                "role": "assistant",
                                                "content": [],
                                                "status": "in_progress",
                                            },
                                        },
                                    )
                                    yield format_sse(
                                        "response.content_part.added",
                                        {
                                            "type": "response.content_part.added",
                                            "item_id": message_id,
                                            "output_index": 0,
                                            "content_index": 0,
                                            "part": {
                                                "type": "output_text",
                                                "text": "",
                                                "annotations": [],
                                            },
                                        },
                                    )
                                yield format_sse(
                                    "response.output_text.delta",
                                    {
                                        "type": "response.output_text.delta",
                                        "item_id": message_id,
                                        "output_index": 0,
                                        "content_index": 0,
                                        "delta": token_text,
                                    },
                                )

                        full_text = adapter.full_text
                        msg_item = _message_item(message_id, full_text)
                        if output_started:
                            yield format_sse(
                                "response.output_text.done",
                                {
                                    "type": "response.output_text.done",
                                    "item_id": message_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "text": full_text,
                                },
                            )
                            yield format_sse(
                                "response.content_part.done",
                                {
                                    "type": "response.content_part.done",
                                    "item_id": message_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "part": msg_item["content"][0],
                                },
                            )
                            yield format_sse(
                                "response.output_item.done",
                                {
                                    "type": "response.output_item.done",
                                    "output_index": 0,
                                    "item": msg_item,
                                },
                            )

                        usage = _usage_from_console(adapter, messages)
                        yield format_sse(
                            "response.completed",
                            {
                                "type": "response.completed",
                                "response": make_resp_object(
                                    response_id,
                                    model,
                                    "completed",
                                    [msg_item],
                                    usage,
                                ),
                            },
                        )
                        yield "data: [DONE]\n\n"
                        success = True
                        logger.info(
                            "console responses stream completed: attempt={}/{} model={} text_len={}",
                            attempt + 1,
                            max_retries + 1,
                            model,
                            len(full_text),
                        )
                    except UpstreamError as exc:
                        fail_exc = exc
                        if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                            retry = True
                        else:
                            raise
                finally:
                    await directory.release(acct)
                    await _apply_feedback(
                        directory,
                        token,
                        selected_mode_id,
                        success,
                        fail_exc,
                    )

                if success or not retry:
                    return
                excluded.append(token)

        return _run_stream()

    excluded: list[str] = []
    for attempt in range(max_retries + 1):
        acct = await directory.reserve_any(
            spec.pool_candidates(),
            now_s_override=now_s(),
            exclude_tokens=excluded or None,
        )
        if acct is None:
            raise RateLimitError("No available accounts for this model tier")
        selected_mode_id = acct.mode_id

        token = acct.token
        success = False
        retry = False
        fail_exc: BaseException | None = None
        adapter = ConsoleStreamAdapter()

        try:
            payload = build_console_payload(
                messages=messages,
                model=model,
                temperature=temperature,
                top_p=top_p,
                reasoning_effort=reasoning_effort,
                stream=True,
            )
            try:
                async for event_type, data in stream_console_chat(
                    token,
                    payload,
                    timeout_s=timeout_s,
                ):
                    adapter.feed(event_type, data)
                success = True
                return make_resp_object(
                    response_id,
                    model,
                    "completed",
                    [_message_item(message_id, adapter.full_text)],
                    _usage_from_console(adapter, messages),
                )
            except UpstreamError as exc:
                fail_exc = exc
                if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                    retry = True
                else:
                    raise
        finally:
            await directory.release(acct)
            await _apply_feedback(
                directory,
                token,
                selected_mode_id,
                success,
                fail_exc,
            )

        if not retry:
            break
        excluded.append(token)

    raise RateLimitError("No available accounts after retries")


def _messages_from_input(input_val: str | list, instructions: str | None) -> list[dict]:
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if isinstance(input_val, str):
        messages.append({"role": "user", "content": input_val})
        return messages

    for item in input_val:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "message" if "role" in item else None)
        if item_type != "message":
            continue
        role = item.get("role", "user")
        content = item.get("content", "")
        if isinstance(content, list):
            normalized: list[dict[str, Any]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type", "")
                if part_type in {"input_text", "output_text"}:
                    normalized.append({"type": "text", "text": part.get("text", "")})
                elif part_type in {"image", "input_image"}:
                    source = part.get("image_url") or part.get("source") or {}
                    url = source.get("url", "") if isinstance(source, dict) else str(source)
                    if url:
                        normalized.append({"type": "image_url", "image_url": {"url": url}})
            content = normalized
        messages.append({"role": role, "content": content})
    return messages


def _message_item(message_id: str, text: str) -> dict:
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
        "status": "completed",
    }


def _usage_from_console(adapter: ConsoleStreamAdapter, prompt: object) -> dict:
    usage = adapter.usage or {}
    input_tokens = int(usage.get("input_tokens") or estimate_prompt_tokens(prompt))
    output_tokens = int(usage.get("output_tokens") or estimate_tokens(adapter.full_text))
    return build_resp_usage(input_tokens, output_tokens, 0)


async def _apply_feedback(
    directory,
    token: str,
    mode_id: int,
    success: bool,
    fail_exc: BaseException | None,
) -> None:
    kind = (
        FeedbackKind.SUCCESS
        if success
        else feedback_kind_for_error(fail_exc)
        if fail_exc
        else FeedbackKind.SERVER_ERROR
    )
    await directory.feedback(token, kind, mode_id, now_s_val=now_s())
    if mode_id < 0:
        return
    if success:
        asyncio.create_task(_quota_sync(token, mode_id)).add_done_callback(
            _log_task_exception
        )
    else:
        asyncio.create_task(_fail_sync(token, mode_id, fail_exc)).add_done_callback(
            _log_task_exception
        )


__all__ = ["create"]
