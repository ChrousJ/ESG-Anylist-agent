from __future__ import annotations

import logging
import os
import time
from typing import Any

_gemini_client = None
_qwen_client = None
log = logging.getLogger(__name__)


class LLMResponse:
    def __init__(self, text: str) -> None:
        self.text = text


def _get_config_attr(config: Any, name: str, default: Any) -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default)


def _parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _usage_get(usage: Any, key: str) -> Any:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
    return _gemini_client


def _get_qwen_client():
    global _qwen_client
    if _qwen_client is None:
        import openai
        client_max_retries = int(os.getenv("QWEN_CLIENT_MAX_RETRIES", "0"))
        _qwen_client = openai.OpenAI(
            api_key=os.getenv("QWEN_API_KEY", ""),
            base_url=os.getenv(
                "QWEN_BASE_URL",
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            ),
            max_retries=client_max_retries,
        )
    return _qwen_client


def get_default_model() -> str:
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    if provider == "qwen":
        return os.getenv("QWEN_MAIN_MODEL", "qwen3.5-flash")
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")


def llm_generate_content(
    model: str,
    contents: Any,
    config: Any = None,
) -> LLMResponse:
    """
    Provider-agnostic content generation.
    - For Gemini: use google-genai generate_content.
    - For Qwen: use OpenAI-compatible chat.completions.
    """
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    temperature = float(_get_config_attr(config, "temperature", 0.0))
    response_mime = _get_config_attr(config, "response_mime_type", "")
    system_instruction = _get_config_attr(config, "system_instruction", "")
    raw_max_output_tokens = _get_config_attr(
        config,
        "max_output_tokens",
        os.getenv("LLM_MAX_OUTPUT_TOKENS", "4096"),
    )
    max_output_tokens: int | None = None
    if raw_max_output_tokens is not None:
        max_output_tokens = int(raw_max_output_tokens)

    if provider == "qwen":
        client = _get_qwen_client()
        model_name = (model or "").strip() or os.getenv("QWEN_MAIN_MODEL", "qwen3.5-flash")
        raw_request_timeout = _get_config_attr(
            config,
            "timeout",
            os.getenv("QWEN_REQUEST_TIMEOUT_SEC", "60"),
        )
        request_timeout = float(raw_request_timeout) if raw_request_timeout is not None else 60.0
        system_msg = system_instruction or ""
        if response_mime == "application/json":
            system_msg = (system_msg + "\nReturn only valid JSON.").strip()
        user_content = contents
        if isinstance(contents, list):
            user_content = "\n".join([str(c) for c in contents])
        messages = []
        if system_msg:
            messages.append({"role": "system", "content": system_msg})
        messages.append({"role": "user", "content": str(user_content)})
        request_kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "timeout": request_timeout,
        }
        if max_output_tokens is not None:
            request_kwargs["max_tokens"] = max_output_tokens
        raw_enable_thinking = _get_config_attr(
            config,
            "enable_thinking",
            os.getenv("QWEN_ENABLE_THINKING", ""),
        )
        enable_thinking = _parse_bool(raw_enable_thinking)
        if enable_thinking is not None:
            request_kwargs["extra_body"] = {"enable_thinking": enable_thinking}

        t_start = time.perf_counter()
        resp = client.chat.completions.create(**request_kwargs)
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        usage = getattr(resp, "usage", None)
        log.info(
            "qwen call model=%s latency_ms=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s timeout_s=%.1f max_tokens=%s thinking=%s",
            model_name,
            latency_ms,
            _usage_get(usage, "prompt_tokens"),
            _usage_get(usage, "completion_tokens"),
            _usage_get(usage, "total_tokens"),
            request_timeout,
            max_output_tokens,
            enable_thinking if enable_thinking is not None else "default",
        )
        text = resp.choices[0].message.content or ""
        return LLMResponse(text)

    # Default: Gemini
    client = _get_gemini_client()
    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )
    return LLMResponse(resp.text)
