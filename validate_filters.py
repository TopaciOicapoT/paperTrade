"""
validate_filters.py
-------------------
Valida los 3 filtros detectados en analyze_features.py ANTES de implementarlos.

Método: post-hoc simulation — se recogen todos los trades del backtest (con sus
features de entrada) y se simulan curvas de equity alternativas saltando los trades
que habrían sido bloqueados por cada filtro. Sin look-ahead bias: todas las features
se calculan ANTES de abrir el trade (igual que en tiempo real).

Filtros a validar:
  F1  Momentum  — saltar si momentum 5 velas está en zona +0.3%..+1.6% (Q3 ADA)
  F2  Sesión    — saltar LINK en sesión europea (08–14h UTC, WR=16.7%)
  F3  Vol Q3    — saltar si volumen USDT normalizado está entre 2.1× y 2.7×
  F1+F2+F3      — los tres combinados

Para cada filtro se muestra: trades_usados | trades_saltados | WR | PF | capital_final
y si mejora o empeora respecto al baseline sin filtros.
"""

import sys
import yaml
import pandas as pd
import numpy as np
from pathlib import Path
from loguru import logger
from dataclasses import dataclass

logger.remove()
logger.add(sys.stderr, level="WARNING")

from data.fetcher import get_data_exchange, fetch_ohlcv
from backtesting.engine import simular_trades

CONFIG_PATH = Path("config/config.yaml")
with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)

SYMBOLS   = ["ADA/USDT", "LINK/USDT", "EGLD/USDT"]
DAYS      = 1095
LEVERAGE  = config["futures"].get("leverage", 3)
FEE       = config.get("paper_trading", {}).get("fee_pct", 0.1)
MLOOKBACK = config["levels"].get("monthly_lookback", 6)
EXTRA     = MLOOKBACK * 30 + 30
VOL_MIN   = config["levels"].get("volume_trigger_ratio", 2.0)
VOL_MAX   = config["levels"].get("volume_trigger_ratio_max", 3.0)


# ─────────────────────────────────────────────────────────────────────────────
# Feature extraction (idéntica a analyze_features.py — sin look-ahead)
# ─────────────────────────────────────────────────────────────────────────────

def compute_features(trade, df5m: pd.DataFrame) -> dict | None:
    idx = trade.entry_index
    if idx < 20:
        return None
    candle  = df5m.iloc[idx]
    window  = df5m.iloc[max(0, idx - 50): idx + 1]

    # Volumen USDT normalizado
    usdt_vol      = float(candle["volume"]) * float(candle["close"])
    usdt_vol_mean = (window["volume"] * window["close"]).mean()
    usdt_norm     = usdt_vol / usdt_vol_mean if usdt_vol_mean > 0 else 1.0

    # Momentum 5 velas
    momentum_5 = 0.0
    if idx >= 5:
        prev5 = float(df5m.iloc[idx - 5]["close"])
        momentum_5 = (float(candle["close"]) - prev5) / prev5 * 100

    # Hora UTC de entrada
    hour = df5m.index[idx].hour

    return {
        "usdt_norm":  round(usdt_norm, 4),
        "momentum_5": round(momentum_5, 4),
        "hour":       hour,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Simulación de equity con filtro (post-hoc)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FilterResult:
    name:       str
    symbol:     str
    used:       int
    skipped:    int
    wins:       int
    losses:     int
    capital:    float
    baseline_capital: float = 0.0

    @property
    def wr(self):
        return self.wins / self.used * 100 if self.used else 0.0

    @property
    def pf(self):
        # Recalcular PF desde wins/losses y los valores de TP/SL aproximados
        # (usamos wr y el ratio R:R estándar 3:1 leveraged como proxy)
        if self.losses == 0:
            return float("inf")
        tp_net = config["risk"]["take_profit_pct"] * LEVERAGE / 100 - FEE * 2 / 100
        sl_net = config["risk"]["sl_behind_level_pct"] * LEVERAGE / 100 + FEE * 2 / 100
        return (self.wins * tp_net) / (self.losses * sl_net)

    @property
    def delta_pct(self):
        if self.baseline_capital == 0:
            return 0.0
        return (self.capital - self.baseline_capital) / self.baseline_capital * 100


def simulate_equity(trades_with_feats: list[dict], filter_fn, initial=1000.0) -> FilterResult:
    """
    Simula la curva de equity aplicando filter_fn a cada trade.
    filter_fn(feat_dict) → True = MANTENER, False = SALTAR
    """
    capital = initial
    used = skipped = wins = losses = 0
    for item in trades_with_feats:
        feat = item["feat"]
        trade = item["trade"]
        if feat is None or not filter_fn(feat, item["symbol"]):
            skipped += 1
            continue
        pnl = trade.pnl_pct / 100.0
        capital *= (1 + pnl)
        used += 1
        if trade.result == "win":
            wins += 1
        else:
            losses += 1
    return FilterResult(
        name="", symbol="", used=used, skipped=skipped,
        wins=wins, losses=losses, capital=capital,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Definición de filtros
# ─────────────────────────────────────────────────────────────────────────────

# Rangos detectados en analyze_features.py:
#   Momentum Q3 (peor WR) para ADA: +0.30% a +1.57%
#   Pero probamos también umbrales alternativos para ver si hay versión más robusta
MOMENTUM_ZONES = [
    ("mom_q3_ada",     0.30,  1.60),   # zona exacta de ADA (peor bucket)
    ("mom_any_pos",    0.20,  99.0),   # cualquier momentum positivo moderado
    ("mom_strong_pos", 1.00,  99.0),   # solo momentum muy fuerte
]

VOLUME_Q3_LO = 2.10
VOLUME_Q3_HI = 2.70


def f_baseline(feat, sym):
    return True

def make_momentum_filter(lo, hi):
    def fn(feat, sym):
        m = feat["momentum_5"]
        return not (lo <= m <= hi)   # True = mantener (fuera de zona mala)
    return fn

def f_session_link(feat, sym):
    if sym == "LINK/USDT" and 8 <= feat["hour"] < 14:
        return False   # bloquear LINK en sesión europea
    return True

def f_volume_q3(feat, sym):
    n = feat["usdt_norm"]
    return not (VOLUME_Q3_LO <= n <= VOLUME_Q3_HI)   # True = mantener

def f_session_link_egld(feat, sym):
    """Sesión por símbolo: LINK bloquear Europa, EGLD bloquear América+Noche."""
    if sym == "LINK/USDT" and 8  <= feat["hour"] < 14:
        return False
    if sym == "EGLD/USDT" and 14 <= feat["hour"] < 24:
        return False
    return True

def f_all_combined(feat, sym):
    """Los 3 filtros juntos (usando momentum zona Q3 de ADA como base)."""
    if feat["momentum_5"] is not None and 0.30 <= feat["momentum_5"] <= 1.60:
        return False
    if sym == "LINK/USDT" and 8 <= feat["hour"] < 14:
        return False
    if VOLUME_Q3_LO <= feat["usdt_norm"] <= VOLUME_Q3_HI:
        return False
    return True

def f_f2b_f3(feat, sym):
    """F2b + F3 sin momentum (los dos filtros claramente validados)."""
    if sym == "LINK/USDT" and 8 <= feat["hour"] < 14:
        return False
    if sym == "EGLD/USDT" and 14 <= feat["hour"] < 24:
        return False
    if VOLUME_Q3_LO <= feat["usdt_norm"] <= VOLUME_Q3_HI:
        return False
    return True

def f_f2b_f3_f1ada(feat, sym):
    """F2b + F3 + momentum solo para ADA (donde sí funciona)."""
    if sym == "LINK/USDT" and 8 <= feat["hour"] < 14:
        return False
    if sym == "EGLD/USDT" and 14 <= feat["hour"] < 24:
        return False
    if VOLUME_Q3_LO <= feat["usdt_norm"] <= VOLUME_Q3_HI:
        return False
    if sym == "ADA/USDT" and 0.30 <= feat["momentum_5"] <= 1.60:
        return False
    return True


FILTER_SCENARIOS = [
    ("Baseline (sin filtros)",          f_baseline),
    ("F1: Momentum Q3 [+0.3,+1.6%]",   make_momentum_filter(0.30, 1.60)),
    ("F1c: Momentum fuerte [>+1.0%]",   make_momentum_filter(1.00, 99.0)),
    ("F2: Sesión LINK Europa block",    f_session_link),
    ("F2b: Sesión LINK+EGLD optimal",   f_session_link_egld),
    ("F3: Vol USDT Q3 block [2.1-2.7]", f_volume_q3),
    ("F2b+F3 (mejores 2 combinados)",   f_f2b_f3),
    ("F2b+F3+F1ada (mejor combo)",      f_f2b_f3_f1ada),
    ("F1+F2+F3 todos",                  f_all_combined),
]


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

exchange = get_data_exchange()

print(f"\n{'═'*72}")
print(f"  VALIDACIÓN DE FILTROS — {DAYS} días ({DAYS//365} años) × {len(SYMBOLS)} símbolos")
print(f"{'═'*72}")

# Acumulador global para validar sobre los 3 símbolos combinados
global_items = []

for sym in SYMBOLS:
    print(f"\n\n{'─'*72}")
    print(f"  {sym}")
    print(f"{'─'*72}")

    df5m = fetch_ohlcv(sym, "5m", limit=DAYS * 24 * 12, exchange=exchange)
    df1d = fetch_ohlcv(sym, "1d", limit=DAYS + EXTRA,   exchange=exchange)
    df1w = fetch_ohlcv(sym, "1w", limit=52,              exchange=exchange)

    res = simular_trades(
        df_entry=df5m, df_daily=df1d, df_weekly=df1w,
        symbol=sym, monthly_lookback=MLOOKBACK,
        tp_pct=config["risk"]["take_profit_pct"],
        sl_behind_pct=config["risk"]["sl_behind_level_pct"],
        volume_ratio_min=VOL_MIN, volume_ratio_max=VOL_MAX,
        fee_pct=FEE, ml_threshold=0.0, volatility_filter=False,
        leverage=LEVERAGE, initial_capital=1000.0,
        failed_retest_filter=True,
        symbol_params=config.get("symbol_params"),
    )

    # Construir lista de items {trade, feat, symbol}
    items = []
    for t in res.trades:
        if t.result == "open":
            continue
        feat = compute_features(t, df5m)
        items.append({"trade": t, "feat": feat, "symbol": sym})
        global_items.append({"trade": t, "feat": feat, "symbol": sym})

    # Calcular baseline capital (referencia)
    base_res = simulate_equity(items, f_baseline)
    baseline_cap = base_res.capital

    # Cabecera tabla
    print(f"\n  {'Escenario':<38} {'Usados':>6} {'Sltd':>5} {'WR':>7} {'PF':>5} {'Capital':>10} {'Δ vs base':>10}")
    print(f"  {'─'*38} {'──────':>6} {'────':>5} {'──────':>7} {'────':>5} {'────────':>10} {'────────':>10}")

    for sc_name, sc_fn in FILTER_SCENARIOS:
        r = simulate_equity(items, sc_fn)
        r.baseline_capital = baseline_cap
        delta  = r.delta_pct
        marker = "▲" if delta > 2 else ("▼" if delta < -2 else "~")
        pf     = r.pf
        print(
            f"  {sc_name:<38} {r.used:>6} {r.skipped:>5} {r.wr:>6.1f}%"
            f" {pf:>5.2f} ${r.capital:>9,.0f}  {marker}{delta:>+7.1f}%"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Tabla global — los 3 símbolos juntos (capital total = $3000)
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n\n{'═'*72}")
print(f"  GLOBAL — {len(global_items)} trades, {len(SYMBOLS)} símbolos, capital $1000×3 = $3000")
print(f"{'═'*72}")
print(f"\n  {'Escenario':<38} {'Usados':>6} {'Sltd':>5} {'WR':>7} {'PF':>5} {'Total $3k→':>11} {'Δ vs base':>10}")
print(f"  {'─'*38} {'──────':>6} {'────':>5} {'──────':>7} {'────':>5} {'──────────':>11} {'────────':>10}")

# Baseline global (por símbolo separado, luego sumamos capital final)
sym_baselines = {}
for sym in SYMBOLS:
    sym_items = [x for x in global_items if x["symbol"] == sym]
    r = simulate_equity(sym_items, f_baseline)
    sym_baselines[sym] = r.capital

baseline_total = sum(sym_baselines.values())

for sc_name, sc_fn in FILTER_SCENARIOS:
    total_cap = 0
    total_used = total_skip = total_wins = total_losses = 0
    for sym in SYMBOLS:
        sym_items = [x for x in global_items if x["symbol"] == sym]
        r = simulate_equity(sym_items, sc_fn)
        total_cap    += r.capital
        total_used   += r.used
        total_skip   += r.skipped
        total_wins   += r.wins
        total_losses += r.losses

    total_wr = total_wins / total_used * 100 if total_used else 0
    # PF aproximado
    tp_net = config["risk"]["take_profit_pct"] * LEVERAGE / 100 - FEE * 2 / 100
    sl_net = config["risk"]["sl_behind_level_pct"] * LEVERAGE / 100 + FEE * 2 / 100
    pf = (total_wins * tp_net) / (total_losses * sl_net) if total_losses > 0 else 99
    delta = (total_cap - baseline_total) / baseline_total * 100
    marker = "▲" if delta > 2 else ("▼" if delta < -2 else "~")
    print(
        f"  {sc_name:<38} {total_used:>6} {total_skip:>5} {total_wr:>6.1f}%"
        f" {pf:>5.2f} ${total_cap:>10,.0f}  {marker}{delta:>+7.1f}%"
    )

print(f"""
  Leyenda:
  ▲  = mejora >2% respecto al baseline
  ▼  = empeora >2% respecto al baseline
  ~  = sin cambio significativo (<2%)
  Sltd = trades saltados por el filtro
  Capital calculado con $1000 inicial por símbolo (independientes)
""")
