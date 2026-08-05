"""
api/main.py
-----------
Punto de entrada de FastAPI.

Arranca el PaperTrader en un thread de fondo y expone la API REST + WebSocket.
El frontend (React) se servirá desde /app cuando esté disponible.

Ejecutar en desarrollo:
    uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

En Docker el CMD del Dockerfile lo lanza directamente.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send
from loguru import logger

from api.bot_runner import BotRunner
from api.db.database import create_tables
from api.routers import bot, trades, levels, lab, config_editor

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Inicialización ────────────────────────────────────────────────────────
    logger.info("Inicializando base de datos...")
    create_tables()

    logger.info("Cargando configuración del bot...")
    config = yaml.safe_load(CONFIG_PATH.read_text())

    from execution.paper_trader import PaperTrader
    trader = PaperTrader(config)
    runner = BotRunner(trader, interval_seconds=10)

    app.state.trader = trader
    app.state.runner = runner

    logger.info("Arrancando loop del bot en background thread...")
    runner.start()

    # Tarea periódica de sincronización de trades completados a DB
    sync_task = asyncio.create_task(_db_sync_loop(app))

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    sync_task.cancel()
    runner.stop()
    logger.info("API apagada limpiamente.")


async def _db_sync_loop(app: FastAPI):
    """Sincroniza el trade_log en memoria con PostgreSQL cada 60 segundos."""
    from api.db.database import SessionLocal
    from api.db.models import Trade

    synced_keys: set[str] = set()

    while True:
        await asyncio.sleep(60)
        try:
            trader = app.state.trader
            db = SessionLocal()
            for trade in trader.trade_log:
                key = f"{trade.get('symbol')}_{trade.get('entry_time')}"
                if key in synced_keys:
                    continue
                exists = db.query(Trade).filter_by(
                    symbol=trade.get("symbol"),
                    entry_time=str(trade.get("entry_time", "")),
                ).first()
                if not exists:
                    from api.routers.trades import _persist_trade
                    _persist_trade(db, trade)
                synced_keys.add(key)
            db.close()
        except Exception as e:
            logger.warning(f"[DBSync] Error en sincronización: {e}")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="PaperTrade Dashboard API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # En producción restringir al dominio del frontend
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bot.router)
app.include_router(trades.router)
app.include_router(levels.router)
app.include_router(lab.router)
app.include_router(config_editor.router)

# Servir el build de React si ya existe (producción)
# StaticFiles solo maneja HTTP — ignorar silenciosamente WebSocket upgrades
class _HTTPOnlyStaticFiles(StaticFiles):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        await super().__call__(scope, receive, send)

if FRONTEND_DIST.exists():
    app.mount("/", _HTTPOnlyStaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
