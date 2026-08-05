"""
api/routers/lab.py
------------------
Endpoints del Laboratorio de simulación.

POST /api/lab/simulate       → lanza simulación en background, devuelve job_id
GET  /api/lab/jobs/{job_id}  → estado + progreso + resultado cuando termina
GET  /api/lab/symbols        → lista de símbolos disponibles con info de datos
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api import lab_runner

router = APIRouter(prefix="/api/lab", tags=["lab"])

# Símbolos disponibles en Binance con año aproximado de inicio de datos
AVAILABLE_SYMBOLS = [
    # ── Majors ─────────────────────────────────────────────────────────────────
    {"symbol": "BTC/USDT",    "since_year": 2017, "name": "Bitcoin"},
    {"symbol": "ETH/USDT",    "since_year": 2017, "name": "Ethereum"},
    {"symbol": "BNB/USDT",    "since_year": 2017, "name": "BNB"},
    {"symbol": "XRP/USDT",    "since_year": 2018, "name": "Ripple"},
    {"symbol": "LTC/USDT",    "since_year": 2017, "name": "Litecoin"},
    {"symbol": "TRX/USDT",    "since_year": 2018, "name": "Tron"},
    # ── Smart contracts ─────────────────────────────────────────────────────────
    {"symbol": "ADA/USDT",    "since_year": 2018, "name": "Cardano"},
    {"symbol": "SOL/USDT",    "since_year": 2020, "name": "Solana"},
    {"symbol": "DOT/USDT",    "since_year": 2020, "name": "Polkadot"},
    {"symbol": "AVAX/USDT",   "since_year": 2020, "name": "Avalanche"},
    {"symbol": "ATOM/USDT",   "since_year": 2019, "name": "Cosmos"},
    {"symbol": "EGLD/USDT",   "since_year": 2020, "name": "MultiversX"},
    {"symbol": "NEAR/USDT",   "since_year": 2020, "name": "NEAR Protocol"},
    {"symbol": "ALGO/USDT",   "since_year": 2019, "name": "Algorand"},
    {"symbol": "ICP/USDT",    "since_year": 2021, "name": "Internet Computer"},
    {"symbol": "THETA/USDT",  "since_year": 2019, "name": "Theta"},
    {"symbol": "VET/USDT",    "since_year": 2019, "name": "VeChain"},
    # ── L2 / infra ──────────────────────────────────────────────────────────────
    {"symbol": "MATIC/USDT",  "since_year": 2019, "name": "Polygon"},
    {"symbol": "ARB/USDT",    "since_year": 2023, "name": "Arbitrum"},
    {"symbol": "OP/USDT",     "since_year": 2022, "name": "Optimism"},
    {"symbol": "APT/USDT",    "since_year": 2022, "name": "Aptos"},
    {"symbol": "STX/USDT",    "since_year": 2019, "name": "Stacks"},
    # ── DeFi ────────────────────────────────────────────────────────────────────
    {"symbol": "LINK/USDT",   "since_year": 2019, "name": "Chainlink"},
    {"symbol": "AAVE/USDT",   "since_year": 2020, "name": "Aave"},
    {"symbol": "UNI/USDT",    "since_year": 2020, "name": "Uniswap"},
    {"symbol": "INJ/USDT",    "since_year": 2021, "name": "Injective"},
    # ── Otros ───────────────────────────────────────────────────────────────────
    {"symbol": "DOGE/USDT",   "since_year": 2019, "name": "Dogecoin"},
    {"symbol": "XLM/USDT",    "since_year": 2018, "name": "Stellar"},
    {"symbol": "HBAR/USDT",   "since_year": 2019, "name": "Hedera"},
    {"symbol": "FIL/USDT",    "since_year": 2020, "name": "Filecoin"},
    {"symbol": "TON/USDT",    "since_year": 2021, "name": "Toncoin"},
    {"symbol": "AXS/USDT",    "since_year": 2020, "name": "Axie Infinity"},
    {"symbol": "SAND/USDT",   "since_year": 2020, "name": "The Sandbox"},
]

_CURRENT_YEAR = 2026


class SimulateRequest(BaseModel):
    symbols:          list[str] = Field(min_length=1, max_length=16)
    capital:          float     = Field(gt=0, le=1_000_000)
    max_positions:    int       = Field(ge=1, le=10)
    years:            int       = Field(ge=1, le=15, default=10)
    leverage:         int       = Field(ge=1, le=10, default=3)
    symbol_params:    dict[str, dict] | None = None
    date_from:        str | None = None
    date_to:          str | None = None
    strategy_entries: list[dict] | None = None  # [{symbol, strategy, ...params}]


@router.get("/symbols")
def get_symbols(request: Request):
    """Lista de símbolos disponibles con años de historia aproximados."""
    active = request.app.state.trader.symbols
    return {
        "active_symbols": active,
        "available": AVAILABLE_SYMBOLS,
    }


@router.post("/simulate")
def start_simulation(body: SimulateRequest, request: Request):
    """
    Lanza la simulación en background.
    Devuelve job_id para hacer polling en GET /api/lab/jobs/{job_id}.
    """
    from datetime import date as _date
    config = request.app.state.trader.config

    # ── Modo rango de fechas ──────────────────────────────────────────────────
    if body.date_from and body.date_to:
        d_from = _date.fromisoformat(body.date_from)
        d_to   = _date.fromisoformat(body.date_to)
        days   = (d_to - d_from).days
        actual_years = round(days / 365, 1)
        capped = False
    else:
        # ── Modo años (por defecto) ───────────────────────────────────────────
        min_since = _CURRENT_YEAR
        for sym in body.symbols:
            meta = next((s for s in AVAILABLE_SYMBOLS if s["symbol"] == sym), None)
            if meta and meta["since_year"] > min_since:
                min_since = meta["since_year"]
        max_available_years = _CURRENT_YEAR - min_since
        actual_years = min(body.years, max_available_years) if max_available_years > 0 else body.years
        days   = actual_years * 365
        capped = body.years > actual_years

    job_id = lab_runner.create_job()
    lab_runner.start_simulation(
        job_id=job_id,
        symbols=body.symbols,
        capital=body.capital,
        max_positions=body.max_positions,
        days=days,
        leverage=body.leverage,
        config=config,
        symbol_params_override=body.symbol_params,
        date_from=body.date_from,
        date_to=body.date_to,
        strategy_entries=body.strategy_entries,
    )

    return {
        "job_id":          job_id,
        "requested_years": body.years,
        "actual_years":    actual_years,
        "days":            days,
        "symbols":         body.symbols,
        "capped":          capped,
        "date_from":       body.date_from,
        "date_to":         body.date_to,
    }


@router.get("/jobs/{job_id}")
def poll_job(job_id: str):
    """Devuelve estado del job: pending | running | done | error | cancelled."""
    job = lab_runner.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job no encontrado o expirado (>30min)")
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Cancela una simulación en curso."""
    ok = lab_runner.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job no encontrado o ya terminado")
    return {"message": "Simulación cancelada."}
