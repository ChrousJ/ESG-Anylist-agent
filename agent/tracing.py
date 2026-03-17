from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable


LOG = logging.getLogger(__name__)

_MAX_LOG_LEN = int(os.getenv("LOG_REDACT_MAX_LEN", "800"))

_REDACT_PATTERNS = [
    (r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*([A-Za-z0-9_\-]{8,})", r"\1=<redacted>"),
    (r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}", "bearer <redacted>"),
    (r"sk-[A-Za-z0-9]{16,}", "<redacted>"),
]

_PII_PATTERNS = [
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<redacted-email>"),
    (r"\b\d{3}[-\\s]?\d{3,4}[-\\s]?\d{4}\b", "<redacted-phone>"),
]


def _redact_text(text: str) -> str:
    if not text:
        return text
    redacted = text
    for pattern, repl in _REDACT_PATTERNS:
        redacted = re.sub(pattern, repl, redacted)
    for pattern, repl in _PII_PATTERNS:
        redacted = re.sub(pattern, repl, redacted)
    if len(redacted) > _MAX_LOG_LEN:
        redacted = redacted[:_MAX_LOG_LEN] + "...<truncated>"
    return redacted


def _sanitize_extra(extra: dict | None) -> dict | None:
    if not extra:
        return extra
    sanitized = {}
    for key, value in extra.items():
        if isinstance(value, str):
            sanitized[key] = _redact_text(value)
        else:
            sanitized[key] = value
    return sanitized


def _configure_langsmith() -> bool:
    api_key = os.getenv("LANGSMITH_API_KEY", "").strip()
    if not api_key:
        LOG.warning("LANGSMITH_API_KEY not set; LangSmith tracing disabled.")
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        return False

    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = api_key
    os.environ["LANGCHAIN_PROJECT"] = os.getenv("LANGSMITH_PROJECT", "esg-agent")
    os.environ["LANGCHAIN_ENDPOINT"] = os.getenv(
        "LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"
    )
    return True


LANGSMITH_ENABLED = _configure_langsmith()
_LANGSMITH_AVAILABLE = False


class TraceLogger:
    def __init__(self, node_name: str, trace_id: str = "") -> None:
        self.node_name = node_name
        self.trace_id = trace_id

    def bind_trace(self, trace_id: str) -> "TraceLogger":
        return TraceLogger(self.node_name, trace_id)

    def _prefix(self) -> str:
        if self.trace_id:
            return f"[{self.trace_id}] [{self.node_name}]"
        return f"[{self.node_name}]"

    def info(self, msg: str, extra: dict | None = None) -> None:
        LOG.info(f"{self._prefix()} {_redact_text(msg)}", extra=_sanitize_extra(extra))

    def warning(self, msg: str, extra: dict | None = None) -> None:
        LOG.warning(f"{self._prefix()} {_redact_text(msg)}", extra=_sanitize_extra(extra))

    def error(self, msg: str, extra: dict | None = None, exc_info: bool = False) -> None:
        LOG.error(
            f"{self._prefix()} {_redact_text(msg)}",
            extra=_sanitize_extra(extra),
            exc_info=exc_info,
        )


def generate_trace_id() -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    random_hex = uuid.uuid4().hex[:6]
    return f"esg-{date_str}-{random_hex}"


def generate_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _extract_input_summary(state: dict) -> str:
    query = state.get("user_query", "")
    return query[:80]


def _extract_output_summary(state_before: dict, state_after: dict) -> str:
    return str(state_after.get("eval_o_status", ""))


def _append_node_trace(state: dict, entry: dict) -> None:
    traces = list(state.get("node_trace", []))
    traces.append(entry)
    state["node_trace"] = traces


def trace_node(node_name: str, tags: list[str] | None = None, traceable: bool = True):
    def decorator(fn: Callable):
        def wrapper(state: dict, *args, **kwargs) -> dict:
            trace_id = state.get("trace_id", "")
            log = TraceLogger(node_name, trace_id)
            started_at = datetime.now(timezone.utc).isoformat()
            t_start = time.perf_counter()
            state_before = dict(state)

            log.info(f"input={_extract_input_summary(state)}")

            try:
                result_state = fn(state, *args, **kwargs)

                duration_ms = int((time.perf_counter() - t_start) * 1000)
                finished_at = datetime.now(timezone.utc).isoformat()
                output_summary = _extract_output_summary(state_before, result_state)

                entry = {
                    "node_name": node_name,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "duration_ms": duration_ms,
                    "status": "success",
                    "decision": output_summary,
                    "error": "",
                }
                _append_node_trace(result_state, entry)

                log.info(f"done {duration_ms}ms")
                return result_state

            except Exception as exc:
                duration_ms = int((time.perf_counter() - t_start) * 1000)
                finished_at = datetime.now(timezone.utc).isoformat()
                entry = {
                    "node_name": node_name,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "duration_ms": duration_ms,
                    "status": "failed",
                    "decision": "",
                    "error": str(exc)[:200],
                }
                _append_node_trace(state, entry)
                log.error("failed", exc_info=True)
                raise

        return wrapper

    return decorator


async def run_with_timeout_async(
    fn: Callable,
    args: tuple = (),
    kwargs: dict | None = None,
    timeout_seconds: float = 30.0,
    worker_name: str = "worker",
    trace_id: str = "",
) -> dict:
    log = TraceLogger(worker_name, trace_id)
    kwargs = kwargs or {}
    t_start = time.perf_counter()

    try:
        result = await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout_seconds)
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        log.info(f"async ok {latency_ms}ms")
        return {
            "status": "success",
            "result": result,
            "error_type": "",
            "error_detail": "",
            "latency_ms": latency_ms,
        }
    except asyncio.TimeoutError:
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        log.warning(f"async timeout {timeout_seconds}s {latency_ms}ms")
        return {
            "status": "timeout",
            "result": None,
            "error_type": "TIMEOUT",
            "error_detail": f"exceeded {timeout_seconds}s (cancelled)",
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        tb_str = traceback.format_exc()
        log.error(f"async failed {latency_ms}ms", {"error": str(exc)[:200]})
        return {
            "status": "failed",
            "result": None,
            "error_type": "UNKNOWN",
            "error_detail": tb_str[:500],
            "latency_ms": latency_ms,
        }


def run_with_timeout(
    fn: Callable,
    args: tuple = (),
    kwargs: dict | None = None,
    timeout_seconds: float = 30.0,
    worker_name: str = "worker",
    trace_id: str = "",
) -> dict:
    """
    Sync soft-timeout wrapper. Thread timeout does not kill blocking I/O.
    Use run_with_timeout_async for cancelable async functions.
    """
    if inspect.iscoroutinefunction(fn):
        try:
            return asyncio.run(
                run_with_timeout_async(
                    fn,
                    args=args,
                    kwargs=kwargs,
                    timeout_seconds=timeout_seconds,
                    worker_name=worker_name,
                    trace_id=trace_id,
                )
            )
        except RuntimeError:
            return {
                "status": "failed",
                "result": None,
                "error_type": "ASYNC_CONTEXT",
                "error_detail": "use run_with_timeout_async inside event loop",
                "latency_ms": 0,
            }

    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    log = TraceLogger(worker_name, trace_id)
    kwargs = kwargs or {}
    t_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn, *args, **kwargs)
        try:
            result = future.result(timeout=timeout_seconds)
            latency_ms = int((time.perf_counter() - t_start) * 1000)
            log.info(f"sync ok {latency_ms}ms")
            return {
                "status": "success",
                "result": result,
                "error_type": "",
                "error_detail": "",
                "latency_ms": latency_ms,
            }
        except FuturesTimeout:
            future.cancel()
            latency_ms = int((time.perf_counter() - t_start) * 1000)
            log.warning(f"sync timeout {timeout_seconds}s {latency_ms}ms (soft)")
            return {
                "status": "timeout",
                "result": None,
                "error_type": "TIMEOUT",
                "error_detail": f"exceeded {timeout_seconds}s (soft timeout)",
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t_start) * 1000)
            tb_str = traceback.format_exc()
            log.error(f"sync failed {latency_ms}ms", {"error": str(exc)[:200]})

            error_type = "UNKNOWN"
            err_msg = str(exc).lower()
            if "connection" in err_msg or "database" in err_msg:
                error_type = "DB_CONN_FAIL"
            elif "syntax" in err_msg or "sql" in err_msg:
                error_type = "SQL_ERROR"
            elif "index" in err_msg or "chroma" in err_msg or "embedding" in err_msg:
                error_type = "VECTOR_STORE_ERROR"

            return {
                "status": "failed",
                "result": None,
                "error_type": error_type,
                "error_detail": tb_str[:500],
                "latency_ms": latency_ms,
            }


def llm_call_with_retry(
    fn: Callable,
    args: tuple = (),
    kwargs: dict | None = None,
    max_retries: int = 2,
    base_delay: float = 1.0,
    timeout_seconds: float = 60.0,
    caller_name: str = "llm_call",
    trace_id: str = "",
) -> Any:
    log = TraceLogger(caller_name, trace_id)
    kwargs = kwargs or {}
    last_error: dict | None = None
    # Global soft rate limit for Gemini/Qwen calls (prevents 429 storms)
    global_min_interval = float(os.getenv("LLM_MIN_INTERVAL_SEC", "2.0"))
    jitter = float(os.getenv("LLM_JITTER_SEC", "0.5"))
    max_backoff = float(os.getenv("LLM_MAX_BACKOFF_SEC", "20.0"))
    max_retries = int(os.getenv("LLM_MAX_RETRIES", str(max_retries)))
    base_delay = float(os.getenv("LLM_BASE_DELAY_SEC", str(base_delay)))

    # Simple process-level rate limiter
    if not hasattr(llm_call_with_retry, "_last_call_ts"):
        llm_call_with_retry._last_call_ts = 0.0  # type: ignore[attr-defined]

    for attempt in range(max_retries + 1):
        now = time.time()
        last_ts = getattr(llm_call_with_retry, "_last_call_ts")  # type: ignore[attr-defined]
        elapsed = now - last_ts
        if elapsed < global_min_interval:
            sleep_for = (global_min_interval - elapsed) + (jitter * 0.5)
            time.sleep(max(0.0, sleep_for))
        setattr(llm_call_with_retry, "_last_call_ts", time.time())  # type: ignore[attr-defined]

        result = run_with_timeout(
            fn,
            args=args,
            kwargs=kwargs,
            timeout_seconds=timeout_seconds,
            worker_name=caller_name,
            trace_id=trace_id,
        )
        if result["status"] == "success":
            return result["result"]

        last_error = result
        err_detail = (result.get("error_detail") or "")[:200]
        log.warning(f"llm_call failed status={result['status']} attempt={attempt+1} err={err_detail}")
        if attempt < max_retries:
            # Exponential backoff with cap; add jitter for bursty 429
            sleep_for = min(max_backoff, base_delay * (2 ** attempt)) + jitter
            # If we detect rate-limit hints, wait a bit longer
            err_lower = (result.get("error_detail") or "").lower()
            if "429" in err_lower or "rate" in err_lower or "quota" in err_lower:
                sleep_for = max(sleep_for, max_backoff)
            time.sleep(sleep_for)

    raise RuntimeError(
        f"LLM call failed after {max_retries+1} attempts: {last_error}"
    )
