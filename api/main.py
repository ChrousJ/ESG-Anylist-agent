from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, AsyncGenerator

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.abspath("data"))

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("api.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def _parse_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if not raw:
        return [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    return [o.strip() for o in raw.split(",") if o.strip()]


def _parse_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y"}


def _legacy_memory_reads_enabled() -> bool:
    return _parse_bool(os.getenv("LEGACY_MEMORY_READS_ENABLED", "false"), default=False)


def _legacy_memory_writes_enabled() -> bool:
    return _parse_bool(os.getenv("LEGACY_MEMORY_WRITES_ENABLED", "false"), default=False)


def _query_analytics_enabled() -> bool:
    return _parse_bool(os.getenv("QUERY_ANALYTICS_ENABLED", "true"), default=True)


def _memory_mode_label() -> str:
    if _legacy_memory_reads_enabled() or _legacy_memory_writes_enabled():
        return "hybrid"
    return "checkpointer_primary"


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    conversation_id: str = Field(default="")
    stream: bool = Field(default=False)


class SourceItem(BaseModel):
    type: str = ""
    company: str = ""
    year: str = ""
    page: str = ""
    file: str = ""
    score: float = 0.0
    excerpt: str = ""
    query: str = ""
    content: str = ""


class ChartSeries(BaseModel):
    name: str
    data: list[Optional[float]]
    unit: str = ""


class ChartSpec(BaseModel):
    type: str
    title: str
    x_axis: list[str]
    series: list[ChartSeries]


class ChatResponse(BaseModel):
    analysis: str = Field(description="Final analysis in Markdown.")
    key_findings: list[str] = Field(default=[], description="Key findings.")
    chart_spec: Optional[ChartSpec] = Field(default=None, description="Optional chart spec.")

    query_class: str = Field(description="knowledge/complex/clarify")
    entities: dict = Field(default={}, description="Parsed entities.")
    industry: str = Field(default="", description="Industry label.")
    sources: list[SourceItem] = Field(default=[], description="Citations.")

    eval_o_status: str = Field(description="pass/degraded")
    is_degraded: bool = Field(default=False)
    degraded_reason: str = Field(default="")
    missing_summary: str = Field(default="")

    trace_id: str = Field(description="Trace id.")
    conversation_id: str = Field(description="Conversation id.")
    langsmith_run_url: str = Field(default="")
    latency_ms: int = Field(default=0)


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    version: str = "1.0.0"
    db_ok: bool = False
    vector_ok: bool = False
    llm_provider: str = ""
    main_model: str = ""
    checkpointer_backend: str = ""
    memory_mode: str = ""
    legacy_memory_reads: bool = False
    legacy_memory_writes: bool = False
    query_analytics_enabled: bool = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from agent.graph import get_checkpointer_backend_name
        from agent.llm_provider import get_default_model

        log.info(
            "runtime config provider=%s model=%s checkpointer=%s memory_mode=%s legacy_reads=%s legacy_writes=%s query_analytics=%s",
            os.getenv("LLM_PROVIDER", "gemini").strip().lower(),
            get_default_model(),
            get_checkpointer_backend_name(),
            _memory_mode_label(),
            _legacy_memory_reads_enabled(),
            _legacy_memory_writes_enabled(),
            _query_analytics_enabled(),
        )
    except Exception as exc:
        log.warning(f"runtime config introspection failed: {exc}")
    yield


app = FastAPI(
    title="ESG Agent API",
    description="LangGraph + RAG + SQL Agent",
    version="1.0.0",
    lifespan=lifespan,
)

cors_allow_origins = _parse_cors_origins()
cors_allow_credentials = _parse_bool(
    os.getenv("CORS_ALLOW_CREDENTIALS", "true"),
    default=True,
)
if "*" in cors_allow_origins:
    cors_allow_credentials = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins,
    allow_credentials=cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/static/index.html")


@app.get("/api/traces/stats")
def traces_stats():
    import sys
    # Add scripts to path dynamically so we can import trace_stats
    scripts_dir = os.path.abspath("scripts")
    if scripts_dir not in sys.path:
        sys.path.append(scripts_dir)
    try:
        from scripts.trace_stats import get_trace_stats
        return get_trace_stats("api.log")
    except Exception as e:
        import traceback
        return {"error": str(e), "trace": traceback.format_exc()}


@app.middleware("http")
async def request_trace_middleware(request: Request, call_next):
    from agent.tracing import generate_request_id

    request_id = generate_request_id()
    request.state.request_id = request_id

    t_start = time.perf_counter()
    response = await call_next(request)
    latency = int((time.perf_counter() - t_start) * 1000)

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Latency-Ms"] = str(latency)
    return response


def _load_history_from_redis(conversation_id: str) -> list[dict]:
    if not _legacy_memory_reads_enabled():
        return []
    if not conversation_id:
        return []
    try:
        import json
        import redis

        r = redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_timeout=2,
        )
        data = r.get(f"esg:history:{conversation_id}")
        return json.loads(data) if data else []
    except Exception:
        return []


def _load_user_preferences(conversation_id: str) -> dict:
    if not _legacy_memory_reads_enabled():
        return {}
    if not conversation_id:
        return {}
    try:
        import json
        import sqlite3

        db = os.getenv("MEMORY_DB", "./memory.db")
        conn = sqlite3.connect(db, timeout=3)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM user_preferences WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        conn.close()
        if row:
            return {
                "preferred_companies": json.loads(row["preferred_companies"]),
                "preferred_metrics": json.loads(row["preferred_metrics"]),
            }
    except Exception:
        pass
    return {}


def _build_response(final_state: dict, latency_ms: int) -> ChatResponse:
    sources = [SourceItem(**s) for s in final_state.get("sources", [])]
    return ChatResponse(
        analysis=final_state.get("analysis", ""),
        key_findings=final_state.get("key_findings", []),
        chart_spec=final_state.get("chart_spec"),
        query_class=final_state.get("query_class", ""),
        entities=final_state.get("entities", {}),
        industry=final_state.get("industry", ""),
        sources=sources,
        eval_o_status=final_state.get("eval_o_status", ""),
        is_degraded=final_state.get("is_degraded", False),
        degraded_reason=final_state.get("degraded_reason", ""),
        missing_summary=final_state.get("missing_summary", ""),
        trace_id=final_state.get("trace_id", ""),
        conversation_id=final_state.get("conversation_id", ""),
        langsmith_run_url=final_state.get("langsmith_run_url", ""),
        latency_ms=latency_ms,
    )


@app.post("/chat", response_model=ChatResponse, summary="Chat")
async def chat(request: Request, body: ChatRequest):
    from agent.graph import get_graph
    from agent.state import make_initial_state
    from agent.tracing import generate_trace_id, generate_request_id

    t_start = time.perf_counter()
    trace_id = generate_trace_id()
    request_id = getattr(request.state, "request_id", generate_request_id())
    if body.stream:
        return StreamingResponse(
            _stream_generator(body, request_id),
            media_type="text/event-stream",
        )
    conversation_id = body.conversation_id or trace_id

    history = _load_history_from_redis(conversation_id)
    user_preferences = _load_user_preferences(conversation_id)

    init_state = make_initial_state(
        user_query=body.query,
        conversation_id=conversation_id,
        trace_id=trace_id,
        request_id=request_id,
        history=history,
        user_preferences=user_preferences,
    )

    graph = get_graph()
    config = {"configurable": {"thread_id": conversation_id}}
    try:
        final_state = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: graph.invoke(init_state, config=config),
        )
    except Exception as e:
        log.error(f"[{trace_id}] agent failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Agent request failed. Please retry or narrow the query.",
        )

    latency_ms = int((time.perf_counter() - t_start) * 1000)
    return _build_response(final_state, latency_ms)


async def _stream_generator(body: ChatRequest, request_id: str) -> AsyncGenerator[str, None]:
    from agent.graph import get_graph
    from agent.state import make_initial_state
    from agent.tracing import generate_trace_id

    trace_id = generate_trace_id()
    conversation_id = body.conversation_id or trace_id
    history = _load_history_from_redis(conversation_id)
    user_preferences = _load_user_preferences(conversation_id)

    init_state = make_initial_state(
        user_query=body.query,
        conversation_id=conversation_id,
        trace_id=trace_id,
        request_id=request_id,
        history=history,
        user_preferences=user_preferences,
    )

    graph = get_graph()
    config = {"configurable": {"thread_id": conversation_id}}
    yield f"data: {{\"event\": \"trace_id\", \"trace_id\": \"{trace_id}\"}}\n\n"

    try:
        seen_nodes: set[str] = set()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue()
        sentinel = object()

        def _run_stream():
            try:
                for chunk in graph.stream(init_state, stream_mode="updates", config=config):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, sentinel)

        stream_future = loop.run_in_executor(None, _run_stream)
        final_state: dict = {}

        while True:
            item = await queue.get()
            if item is sentinel:
                break
            if isinstance(item, Exception):
                raise item

            chunk = item
            for node_name, node_state in chunk.items():
                if node_name in seen_nodes:
                    continue
                seen_nodes.add(node_name)

                node_traces = node_state.get("node_trace", [])
                duration_ms = 0
                if node_traces:
                    last = node_traces[-1]
                    if last.get("node_name") == node_name:
                        duration_ms = last.get("duration_ms", 0)

                event = {
                    "event": "node_complete",
                    "node": node_name,
                    "duration_ms": duration_ms,
                    "status": "ok",
                }
                yield f"data: {JSONResponse(content=event).body.decode()}\n\n"

            for _, ns in chunk.items():
                final_state.update(ns)

        await stream_future
        response = _build_response(final_state, 0)

        complete_event = {
            "event": "analysis_complete",
            "analysis": response.analysis,
            "key_findings": response.key_findings,
            "chart_spec": response.chart_spec.dict() if response.chart_spec else None,
            "eval_o_status": response.eval_o_status,
            "is_degraded": response.is_degraded,
            "missing_summary": response.missing_summary,
            "sources": [s.dict() for s in response.sources[:5]],
            "conversation_id": conversation_id,
            "trace_id": trace_id,
            "langsmith_run_url": response.langsmith_run_url,
        }
        yield f"data: {JSONResponse(content=complete_event).body.decode()}\n\n"

    except Exception as e:
        err_event = {
            "event": "error",
            "message": "Request failed. Please retry or narrow the query.",
            "trace_id": trace_id,
        }
        yield f"data: {JSONResponse(content=err_event).body.decode()}\n\n"


@app.post("/chat/stream", summary="Chat stream (node progress)")
async def chat_stream(request: Request, body: ChatRequest):
    request_id = getattr(request.state, "request_id", "")
    return StreamingResponse(
        _stream_generator(body, request_id),
        media_type="text/event-stream",
    )


@app.get("/health", response_model=HealthResponse, summary="Health check")
async def health_check():
    from agent.graph import get_checkpointer_backend_name
    from agent.llm_provider import get_default_model

    db_ok = False
    vector_ok = False

    db_path = os.getenv("DB_PATH", "./data/esg_data.db")
    try:
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=2)
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except Exception:
        db_ok = False

    vector_dir = os.getenv("VECTOR_STORE_DIR", "./data/vector_store")
    vector_ok = os.path.exists(vector_dir)

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        timestamp=datetime.now(timezone.utc).isoformat(),
        db_ok=db_ok,
        vector_ok=vector_ok,
        llm_provider=os.getenv("LLM_PROVIDER", "gemini").strip().lower(),
        main_model=get_default_model(),
        checkpointer_backend=get_checkpointer_backend_name(),
        memory_mode=_memory_mode_label(),
        legacy_memory_reads=_legacy_memory_reads_enabled(),
        legacy_memory_writes=_legacy_memory_writes_enabled(),
        query_analytics_enabled=_query_analytics_enabled(),
    )


@app.get("/trace/{trace_id}")
async def trace_detail(trace_id: str):
    try:
        import sqlite3

        db = os.getenv("MEMORY_DB", "./memory.db")
        conn = sqlite3.connect(db, timeout=3)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM query_log WHERE trace_id=? ORDER BY id DESC LIMIT 1",
            (trace_id,),
        ).fetchone()
        conn.close()

        if not row:
            raise HTTPException(status_code=404, detail=f"TraceID {trace_id} not found")

        return {k: row[k] for k in row.keys()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
