# ============================================================
# Module: server.py — Thin Shell / Entry Point
# 模块：server.py — 薄壳 / 启动入口
#
# Responsibilities:
#   1. Load config, init logging
#   2. Initialize 5 engines + import engine
#   3. Create MCP server instance
#   4. Call register_tools() and register_routes()
#   5. Wire desire tick callback
#   6. Start (stdio / SSE / streamable-http)
#
# All tool logic lives in tools.py.
# All HTTP route logic lives in routes.py.
# All outbound IO (Telegram) lives in services.py.
# ============================================================

import os
import sys
import logging
import asyncio
import time

import httpx

# --- Ensure same-directory modules can be imported ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from embedding_engine import EmbeddingEngine
from import_memory import ImportEngine
from utils import load_config, setup_logging, strip_wikilinks

from desire_engine import DesireEngine

# =============================================================
# 1. Config & Logging
# =============================================================
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Runtime env vars ---
try:
    OMBRE_PORT = int(os.environ.get("OMBRE_PORT", "8000") or "8000")
except ValueError:
    logger.warning("OMBRE_PORT 不是合法整数，回退到 8000")
    OMBRE_PORT = 8000

OMBRE_HOOK_URL = os.environ.get("OMBRE_HOOK_URL", "").strip()
OMBRE_HOOK_SKIP = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in (
    "1", "true", "yes", "on"
)


async def _fire_webhook(event: str, payload: dict) -> None:
    """Fire-and-forget POST to OMBRE_HOOK_URL."""
    if OMBRE_HOOK_SKIP or not OMBRE_HOOK_URL:
        return
    try:
        body = {"event": event, "timestamp": time.time(), "payload": payload}
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(OMBRE_HOOK_URL, json=body)
    except Exception as e:
        logger.warning(f"Webhook push failed ({event} → {OMBRE_HOOK_URL}): {e}")


# =============================================================
# 2. Initialize Engines
# =============================================================
embedding_engine = EmbeddingEngine(config)
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)
dehydrator = Dehydrator(config)
decay_engine = DecayEngine(config, bucket_mgr)
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)
desire_engine = DesireEngine(config)

# =============================================================
# 3. Create MCP Server
# =============================================================
mcp = FastMCP(
    "Ombre Brain",
    host="0.0.0.0",
    port=OMBRE_PORT,
)

# =============================================================
# 4. Register Tools & Routes
# =============================================================
from tools import register_tools, merge_or_create
from routes import register_routes

register_tools(
    mcp=mcp,
    config=config,
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    desire_engine=desire_engine,
    fire_webhook=_fire_webhook,
)

register_routes(
    mcp=mcp,
    config=config,
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    desire_engine=desire_engine,
    import_engine=import_engine,
    fire_webhook=_fire_webhook,
)

# =============================================================
# 5. Wire Desire Tick Callback → services.py
# =============================================================
from services import on_desire_tick


async def _on_desire_tick(snapshot: dict) -> None:
    """Bridge: desire_engine ticks → services.on_desire_tick with injected deps."""
    await on_desire_tick(
        snapshot,
        desire_engine=desire_engine,
        bucket_mgr=bucket_mgr,
        dehydrator=dehydrator,
        strip_wikilinks=strip_wikilinks,
        merge_or_create=merge_or_create,
    )


desire_engine.set_tick_callback(_on_desire_tick)

# =============================================================
# 6. Entry Point
# =============================================================
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        # --- Keepalive loop: ping /health every 60s ---
        async def _keepalive_loop():
            await asyncio.sleep(10)
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        await client.get(
                            f"http://localhost:{OMBRE_PORT}/health", timeout=5
                        )
                        logger.debug("Keepalive ping OK")
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed: {e}")
                    await asyncio.sleep(60)

        def _start_keepalive():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

        # --- CORS middleware for remote transport ---
        if transport == "streamable-http":
            _app = mcp.streamable_http_app()
        else:
            _app = mcp.sse_app()
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        logger.info("CORS middleware enabled")
        uvicorn.run(_app, host="0.0.0.0", port=OMBRE_PORT)
    else:
        mcp.run(transport=transport)
