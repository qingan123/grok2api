"""OpenAI Chat Completions adapter for console.x.ai high-effort models."""

import asyncio
from typing import AsyncGenerator

import orjson

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
from app.products._account_selection import selection_max_retries
from app.products.openai.chat import (
    _configured_retry_codes,
    _fail_sync,
    _log_task_exception,
    _quota_sync,
    _should_retry_upstream,
)
from app.products.openai._format import (
    build_usage,
    make_chat_response,
    make_response_id,
    make_stream_chunk,
)


async def completions(
    *,
    model: str,
    messages: list[dict],
    stream: bool = True,
    emit_think: bool | None = None,
    temperature: float = 0.7,
    top_p: float = 0.95,
) -> dict | AsyncGenerator[str, None]:
    """Run a console.x.ai model and return OpenAI chat-compatible output."""
    cfg = get_config()
    spec = resolve_model(model)
    timeout_s = cfg.get_float("chat.timeout", 120.0)
    max_retries = selection_max_retries()
    retry_codes = _configured_retry_codes(cfg)
    response_id = make_response_id()

    from app.dataplane.account import _directory as _acct_dir

    if _acct_dir is None:
        raise RateLimitError("Account directory not initialised")
    directory = _acct_dir

    logger.info(
        "console chat request accepted: model={} stream={} messages={}",
        model,
        stream,
        len(messages),
    )

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

                try:
                    payload = build_console_payload(
                        messages=messages,
                        model=model,
                        temperature=temperature,
                        top_p=top_p,
                        stream=True,
                    )
                    try:
                        async for event_type, data in stream_console_chat(
                            token,
                            payload,
                            timeout_s=timeout_s,
                        ):
                            for token_text in adapter.feed(event_type, data):
                                chunk = make_stream_chunk(
                                    response_id,
                                    model,
                                    token_text,
                                )
                                yield f"data: {orjson.dumps(chunk).decode()}\n\n"

                        usage = _usage_from_console(adapter, messages)
                        final = make_stream_chunk(
                            response_id,
                            model,
                            "",
                            is_final=True,
                            usage=usage,
                        )
                        yield f"data: {orjson.dumps(final).decode()}\n\n"
                        yield "data: [DONE]\n\n"
                        success = True
                        logger.info(
                            "console chat stream completed: attempt={}/{} model={} text_len={}",
                            attempt + 1,
                            max_retries + 1,
                            model,
                            len(adapter.full_text),
                        )
                    except UpstreamError as exc:
                        fail_exc = exc
                        if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                            retry = True
                            logger.warning(
                                "console chat retry scheduled: attempt={}/{} status={} token={}...",
                                attempt + 1,
                                max_retries,
                                exc.status,
                                token[:8],
                            )
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
                usage = _usage_from_console(adapter, messages)
                logger.info(
                    "console chat request completed: attempt={}/{} model={} text_len={}",
                    attempt + 1,
                    max_retries + 1,
                    model,
                    len(adapter.full_text),
                )
                return make_chat_response(
                    model,
                    adapter.full_text,
                    response_id=response_id,
                    usage=usage,
                )
            except UpstreamError as exc:
                fail_exc = exc
                if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                    retry = True
                    logger.warning(
                        "console chat retry scheduled: attempt={}/{} status={} token={}...",
                        attempt + 1,
                        max_retries,
                        exc.status,
                        token[:8],
                    )
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


def _usage_from_console(adapter: ConsoleStreamAdapter, prompt: object) -> dict:
    usage = adapter.usage or {}
    prompt_tokens = int(usage.get("input_tokens") or estimate_prompt_tokens(prompt))
    completion_tokens = int(
        usage.get("output_tokens") or estimate_tokens(adapter.full_text)
    )
    return build_usage(prompt_tokens, completion_tokens)


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


__all__ = ["completions"]
