"""
api/routers/config_editor.py
-----------------------------
GET  /api/config         → configuración actual legible por el front
PATCH /api/config        → actualiza config.yaml (símbolos, posiciones, leverage, filtros)

El bot necesita reiniciarse manualmente para aplicar los cambios.
"""

from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/config", tags=["config"])

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "config.yaml"


class SymbolParamsPatch(BaseModel):
    session_block_hours:    list[int] | None = None   # [lo, hi] o null para quitar
    usdt_norm_block_range:  list[float] | None = None
    momentum_q3_block:      list[float] | None = None
    rsi_overbought_block:   float | None = None
    failed_retest_filter:   bool | None = None
    volume_trigger_ratio_max: float | None = None


class ConfigPatch(BaseModel):
    symbols:           list[str] | None = Field(None, min_length=1, max_length=10)
    max_open_positions: int | None = Field(None, ge=1, le=10)
    leverage:          int | None = Field(None, ge=1, le=20)
    symbol_params:     dict[str, dict[str, Any]] | None = None


@router.get("")
def get_config(request: Request):
    """Devuelve la config relevante para el panel de control."""
    cfg = request.app.state.trader.config
    return {
        "symbols":           cfg.get("symbols", []),
        "max_open_positions": cfg["risk"]["max_open_positions"],
        "leverage":          cfg.get("futures", {}).get("leverage", 1),
        "futures_enabled":   cfg.get("futures", {}).get("enabled", False),
        "take_profit_pct":   cfg["risk"]["take_profit_pct"],
        "sl_behind_level_pct": cfg["risk"]["sl_behind_level_pct"],
        "volume_trigger_ratio":     cfg["levels"]["volume_trigger_ratio"],
        "volume_trigger_ratio_max": cfg["levels"].get("volume_trigger_ratio_max", 3.0),
        "failed_retest_filter":     cfg["levels"]["failed_retest_filter"],
        "symbol_params":     cfg.get("symbol_params", {}),
    }


@router.patch("")
def patch_config(body: ConfigPatch, request: Request):
    """
    Actualiza config.yaml con los campos provistos.
    Escribe atómicamente (temp file → rename).
    El bot debe reiniciarse para que los cambios surtan efecto.
    """
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    changed: list[str] = []

    if body.symbols is not None:
        cfg["symbols"] = body.symbols
        changed.append("symbols")

    if body.max_open_positions is not None:
        cfg.setdefault("risk", {})["max_open_positions"] = body.max_open_positions
        changed.append("max_open_positions")

    if body.leverage is not None:
        cfg.setdefault("futures", {})["leverage"] = body.leverage
        changed.append("leverage")

    if body.symbol_params is not None:
        cfg["symbol_params"] = _merge_symbol_params(
            cfg.get("symbol_params", {}), body.symbol_params
        )
        changed.append("symbol_params")

    if not changed:
        raise HTTPException(status_code=400, detail="Sin cambios que aplicar")

    # Escritura atómica
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(yaml.dump(cfg, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)

    # Recargar todo el estado runtime del trader en caliente (sin reinicio)
    request.app.state.trader.reload_config(cfg)

    return {"updated": changed, "message": "Config guardada y aplicada. El bot usa los nuevos parámetros en el siguiente ciclo."}


def _merge_symbol_params(existing: dict, patch: dict) -> dict:
    """Merge per-symbol params: None value en el patch elimina la clave."""
    result = dict(existing)
    for sym, params in patch.items():
        if sym not in result:
            result[sym] = {}
        for k, v in params.items():
            if v is None:
                result[sym].pop(k, None)
            else:
                result[sym][k] = v
        if not result[sym]:
            del result[sym]
    return result
