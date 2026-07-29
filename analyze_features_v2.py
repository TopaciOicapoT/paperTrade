"""
analyze_features_v2.py
----------------------
Análisis AMPLIADO de patrones de entrada — dimensiones NO exploradas aún.

Nuevas dimensiones analizadas (todas calculables ANTES de abrir el trade):
  1.  Día de semana (Lun–Dom)
  2.  RSI 14 en el momento de entrada
  3.  Momentum 1h (cambio % últimas 12 velas de 5m = 1 hora)
  4.  Aceleración de volumen (spike aislado vs build-up gradual 3 velas)
  5.  Estructura de mecha adversa (mecha contra la dirección del trade)
  6.  Alineación de la vela de entrada con la dirección del trade
  7.  ATR% relativo al TP (¿es alcanzable el TP dado la volatilidad actual?)
  8.  Racha previa por símbolo (consecutive losses antes de este trade)
  9.  Hora granular dentro de la sesión (resolución por 2h, no 6h)
 10.  Ratio cuerpo/rango en velas previas (¿el mercado tiene convicción?)

Metodología:
  - Recopila TODOS los trades de los 4 símbolos activos (6yr)
  - Para cada feature, muestra WR por bucket/cuartil
  - Simula el impacto en equity de filtrar el peor bucket
  - Propone umbrales específicos para filtros nuevos
"""

import sys
import yaml
import numpy as np
import pandas as pd
from pathlib import Path
from loguru import logger
from collections import defaultdict

logger.remove()
logger.add(sys.stderr, level="WARNING")

from data.fetcher import get_data_exchange, fetch_ohlcv
from backtesting.engine import simular_trades

CONFIG_PATH = Path("config/config.yaml")
with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)

SYMBOLS   = config["symbols"]          # los 4 activos
DAYS      = 2190                       # 6 años — máxima muestra
LEVERAGE  = config["futures"].get("leverage", 3)
FEE       = config.get("paper_trading", {}).get("fee_pct", 0.1)
MLOOKBACK = config["levels"].get("monthly_lookback", 6)
EXTRA     = MLOOKBACK * 30 + 30
VOL_MIN   = config["levels"].get("volume_trigger_ratio", 2.0)
VOL_MAX   = config["levels"].get("volume_trigger_ratio_max", 3.0)
TP_PCT    = config["risk"]["take_profit_pct"]
SL_PCT    = config["risk"]["sl_behind_level_pct"]


# ═══════════════════════════════════════════════════════════════════════════════
# Feature extraction
# ═══════════════════════════════════════════════════════════════════════════════

def calc_rsi(closes: pd.Series, period: int = 14) -> float:
    """RSI clásico de Wilder. Devuelve el RSI en el último punto."""
    if len(closes) < period + 1:
        return 50.0
    delta = closes.diff().dropna()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.rolling(period).mean().iloc[-1]
    avg_l = loss.rolling(period).mean().iloc[-1]
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - (100 / (1 + rs)), 2)


def compute_features(trade, df5m: pd.DataFrame, all_trades_sym: list, trade_idx_in_list: int) -> dict:
    """
    Calcula el vector de features NUEVO para un trade dado.
    Solo usa información disponible antes de abrir la posición.
    """
    idx = trade.entry_index
    if idx < 30:
        return None

    candle = df5m.iloc[idx]
    o = float(candle["open"])
    h = float(candle["high"])
    l = float(candle["low"])
    c = float(candle["close"])
    rng = h - l

    # ── 1. Día de semana ───────────────────────────────────────────────────────
    dow = df5m.index[idx].dayofweek   # 0=Lun, 4=Vie, 5/6=fin de semana (futuros 24/7)

    # ── 2. RSI 14 ──────────────────────────────────────────────────────────────
    closes_rsi = df5m.iloc[max(0, idx - 30): idx + 1]["close"].astype(float)
    rsi_val = calc_rsi(closes_rsi, 14)

    # ── 3. Momentum 1h (últimas 12 velas 5m = 60 min) ─────────────────────────
    prev12 = float(df5m.iloc[idx - 12]["close"]) if idx >= 12 else float(df5m.iloc[0]["close"])
    momentum_1h = (c - prev12) / prev12 * 100

    # ── 4. Aceleración de volumen ─────────────────────────────────────────────
    # vol_current vs promedio de las 3 velas previas
    v_curr = float(candle["volume"])
    v_prev3_mean = (
        float(df5m.iloc[idx - 1]["volume"]) +
        float(df5m.iloc[idx - 2]["volume"]) +
        float(df5m.iloc[idx - 3]["volume"])
    ) / 3.0
    vol_accel = v_curr / v_prev3_mean if v_prev3_mean > 0 else 1.0

    # ── 5. Mecha adversa (contra la dirección del trade) ──────────────────────
    # Para LONG: mecha superior alta = rechazo del alza
    # Para SHORT: mecha inferior alta = rechazo de la baja
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body       = abs(c - o)

    if trade.direction == "long":
        adverse_wick = upper_wick / rng if rng > 0 else 0.0  # cuánto del rango es mecha adversa
    else:
        adverse_wick = lower_wick / rng if rng > 0 else 0.0

    # ── 6. Alineación vela de entrada con dirección del trade ─────────────────
    # Para LONG: vela verde (c > o) = alineada; roja = contra
    # Para SHORT: vela roja (c < o) = alineada; verde = contra
    if trade.direction == "long":
        candle_aligned = 1 if c > o else 0
    else:
        candle_aligned = 1 if c < o else 0

    # ── 7. ATR% vs TP reachable ────────────────────────────────────────────────
    # ¿El rango medio de volatilidad permite alcanzar el TP?
    atr_win = df5m.iloc[max(0, idx - 14): idx + 1]
    true_ranges = []
    for j in range(1, len(atr_win)):
        hi_ = float(atr_win.iloc[j]["high"])
        lo_ = float(atr_win.iloc[j]["low"])
        pc_ = float(atr_win.iloc[j - 1]["close"])
        true_ranges.append(max(hi_ - lo_, abs(hi_ - pc_), abs(lo_ - pc_)))
    atr_pct = (np.mean(true_ranges) / c * 100) if true_ranges else 0.5

    # ratio: ATR / (TP que necesita alcanzar sin leverage)
    # si ATR% * N_velas_tp < TP_pct → el TP es difícil de alcanzar
    tp_needed_pct = TP_PCT / LEVERAGE   # el TP ajustado al precio spot (sin apalancamiento)
    atr_vs_tp = atr_pct / tp_needed_pct if tp_needed_pct > 0 else 1.0

    # ── 8. Racha previa (consecutive losses before this trade) ────────────────
    streak_losses = 0
    for prev_t in reversed(all_trades_sym[:trade_idx_in_list]):
        if prev_t.result == "loss":
            streak_losses += 1
        elif prev_t.result == "win":
            break  # la racha se rompe
    # (maximo 5 para evitar outliers)
    streak_losses = min(streak_losses, 5)

    # ── 9. Hora granular (resolución 2h) ─────────────────────────────────────
    hour = df5m.index[idx].hour
    hour_bucket_2h = (hour // 2) * 2  # 0,2,4,6,...,22

    # ── 10. Ratio cuerpo/rango de las 3 velas previas (¿mercado con convicción?) ──
    body_ratios_prev = []
    for k in range(1, 4):
        prev_c = df5m.iloc[idx - k]
        pw, pb = (float(prev_c["high"]) - float(prev_c["low"])), abs(float(prev_c["close"]) - float(prev_c["open"]))
        body_ratios_prev.append(pb / pw if pw > 0 else 0.5)
    prev_body_ratio_mean = np.mean(body_ratios_prev)

    # ── Feature de la propia vela de entrada (complemento) ────────────────────
    body_ratio_entry = body / rng if rng > 0 else 0.5

    return {
        "dow":                dow,
        "rsi":                rsi_val,
        "momentum_1h":        round(momentum_1h, 4),
        "vol_accel":          round(vol_accel, 3),
        "adverse_wick":       round(adverse_wick, 3),
        "candle_aligned":     candle_aligned,
        "atr_vs_tp":          round(atr_vs_tp, 3),
        "streak_losses":      streak_losses,
        "hour_2h":            hour_bucket_2h,
        "prev_body_ratio":    round(prev_body_ratio_mean, 3),
        "body_ratio_entry":   round(body_ratio_entry, 3),
        # -- referencia para diagnóstico --
        "direction":          trade.direction,
        "result":             trade.result,
        "symbol":             trade.symbol if hasattr(trade, "symbol") else "?",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Display helpers
# ═══════════════════════════════════════════════════════════════════════════════

def wr_bar(wins, total, width=18) -> str:
    if total == 0:
        return "  —  "
    pct = wins / total * 100
    filled = int(pct / 100 * width)
    return f"{'█'*filled}{'░'*(width-filled)}  {pct:5.1f}%  ({wins}/{total})"


def simulate_equity(items, filter_fn, initial=1000.0):
    """Simula equity aplicando un filtro post-hoc sobre los items (ya tienen result/pnl)."""
    capital = initial
    wins = used = 0
    for it in items:
        if not filter_fn(it):
            continue
        # pnl_pct ya viene del trade; recreamos multiplicador con leverage
        pnl_raw = it["pnl_pct"]
        capital *= (1 + pnl_raw / 100.0)
        used += 1
        if it["result"] == "win":
            wins += 1
    losses = used - wins
    if losses > 0 and wins > 0:
        w_gain = wins * (TP_PCT * LEVERAGE / 100 - FEE * 2 / 100)
        l_loss = losses * (SL_PCT * LEVERAGE / 100 + FEE * 2 / 100)
        pf = w_gain / l_loss
    else:
        pf = 99.0 if losses == 0 else 0.0
    wr = wins / used * 100 if used else 0
    return {"capital": capital, "used": used, "wins": wins, "losses": losses, "wr": wr, "pf": pf}


def show_buckets(items, col, buckets_def, label, base_cap=1000.0):
    """
    buckets_def: list of (label_str, filter_fn) tuples
    """
    print(f"\n  ╔═ {label}")
    print(f"  {'Bucket':<32} {'Trades':>6}  {'WR':>7}  {'Capital':>10}  {'Δbase':>8}")
    print(f"  {'─'*32} {'──────':>6}  {'──────':>7}  {'──────────':>10}  {'──────':>8}")

    bucket_results = []
    for blabel, bfn in buckets_def:
        bucket = [it for it in items if it.get(col) is not None and bfn(it[col])]
        if not bucket:
            continue
        w = sum(1 for it in bucket if it["result"] == "win")
        n = len(bucket)
        pct = w / n * 100
        # simulate equity
        r = simulate_equity(bucket, lambda x: True)
        d = (r["capital"] - base_cap) / base_cap * 100
        marker = "▲" if d > 5 else ("▼" if d < -5 else "~")
        print(f"  {blabel:<32} {n:>6}  {wr_bar(w, n, 14)}  ${r['capital']:>9,.0f}  {marker}{d:>+6.1f}%")
        bucket_results.append((blabel, pct, n, r["capital"]))
    return bucket_results


def show_quartiles_v2(items, col, label, n_q=4, base_cap=1000.0):
    """Cuartiles automáticos para features continuas."""
    vals = [it[col] for it in items if it.get(col) is not None and not np.isnan(it[col])]
    if len(vals) < 40:
        return

    quantiles = np.quantile(vals, np.linspace(0, 1, n_q + 1))
    quantiles = sorted(set(round(q, 4) for q in quantiles))
    if len(quantiles) < 3:
        return

    print(f"\n  ╔═ {label}  (cuartiles automáticos)")
    print(f"  {'Rango':<32} {'Trades':>6}  {'WR':>7}  {'Capital':>10}  {'Δbase':>8}")
    print(f"  {'─'*32} {'──────':>6}  {'──────':>7}  {'──────────':>10}  {'──────':>8}")

    for i in range(len(quantiles) - 1):
        lo, hi = quantiles[i], quantiles[i + 1]
        if i == len(quantiles) - 2:
            bucket = [it for it in items if lo <= it.get(col, float("nan")) <= hi]
        else:
            bucket = [it for it in items if lo <= it.get(col, float("nan")) < hi]
        if len(bucket) < 5:
            continue
        w = sum(1 for it in bucket if it["result"] == "win")
        n = len(bucket)
        r = simulate_equity(bucket, lambda x: True)
        d = (r["capital"] - base_cap) / base_cap * 100
        marker = "▲" if d > 5 else ("▼" if d < -5 else "~")
        print(f"  {lo:>7.3f} – {hi:<7.3f}          {n:>6}  {wr_bar(w, n, 14)}  ${r['capital']:>9,.0f}  {marker}{d:>+6.1f}%")


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

exchange = get_data_exchange()
all_items = []   # todos los trades con sus features

print(f"\n{'═'*70}")
print(f"  ANÁLISIS PROFUNDO DE FEATURES v2 — {DAYS // 365} años × {len(SYMBOLS)} símbolos")
print(f"  {'═'*66}")

for sym in SYMBOLS:
    print(f"  Cargando {sym}...", end=" ", flush=True)
    df5m = fetch_ohlcv(sym, "5m", limit=DAYS * 24 * 12, exchange=exchange)
    df1d = fetch_ohlcv(sym, "1d", limit=DAYS + EXTRA,   exchange=exchange)
    df1w = fetch_ohlcv(sym, "1w", limit=52,              exchange=exchange)

    res = simular_trades(
        df_entry=df5m, df_daily=df1d, df_weekly=df1w,
        symbol=sym, monthly_lookback=MLOOKBACK,
        tp_pct=TP_PCT, sl_behind_pct=SL_PCT,
        volume_ratio_min=VOL_MIN, volume_ratio_max=VOL_MAX,
        fee_pct=FEE, ml_threshold=0.0, volatility_filter=False,
        leverage=LEVERAGE, initial_capital=1000.0,
        failed_retest_filter=True,
        symbol_params=config.get("symbol_params"),
    )
    closed = [t for t in res.trades if t.result != "open"]
    print(f"{len(closed)} trades | WR {sum(1 for t in closed if t.result=='win')/len(closed)*100:.1f}%")

    for i, trade in enumerate(closed):
        feat = compute_features(trade, df5m, closed, i)
        if feat is None:
            continue
        feat["pnl_pct"]  = trade.pnl_pct
        feat["result"]   = trade.result
        feat["symbol"]   = sym
        all_items.append(feat)

# Filtrar items con features válidas
all_items = [it for it in all_items if it]
base_cap = 1000.0
total_w = sum(1 for it in all_items if it["result"] == "win")
total_n = len(all_items)
base_result = simulate_equity(all_items, lambda x: True)

print(f"\n  Total trades analizados: {total_n}")
print(f"  WR global: {total_w/total_n*100:.1f}%  |  Capital baseline: ${base_result['capital']:,.0f}")

# ─────────────────────────────────────────────────────────────────────────────
# 1. DÍA DE SEMANA
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'═'*70}")
print("  1. DÍA DE SEMANA")
print(f"{'═'*70}")

dow_names = {0: "Lunes", 1: "Martes", 2: "Miércoles", 3: "Jueves", 4: "Viernes", 5: "Sábado", 6: "Domingo"}
buckets_dow = [(dow_names[d], lambda x, d=d: x == d) for d in range(7)]
show_buckets(all_items, "dow", buckets_dow, "WR por día de semana", base_cap)

# ─────────────────────────────────────────────────────────────────────────────
# 2. RSI 14
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'═'*70}")
print("  2. RSI 14 AL MOMENTO DE ENTRADA")
print(f"{'═'*70}")

buckets_rsi = [
    ("RSI < 30  (sobreventa profunda)",     lambda x: x < 30),
    ("RSI 30–40 (sobreventa moderada)",     lambda x: 30 <= x < 40),
    ("RSI 40–50 (zona neutral-baja)",       lambda x: 40 <= x < 50),
    ("RSI 50–60 (zona neutral-alta)",       lambda x: 50 <= x < 60),
    ("RSI 60–70 (sobrecompra moderada)",    lambda x: 60 <= x < 70),
    ("RSI > 70  (sobrecompra profunda)",    lambda x: x >= 70),
]
show_buckets(all_items, "rsi", buckets_rsi, "WR por RSI 14", base_cap)
show_quartiles_v2(all_items, "rsi", "RSI 14 — cuartiles")

# ─────────────────────────────────────────────────────────────────────────────
# 3. MOMENTUM 1H
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'═'*70}")
print("  3. MOMENTUM 1H (cambio % en 60 min previos)")
print(f"{'═'*70}")

buckets_mom1h = [
    ("Caída fuerte   < -1%",    lambda x: x < -1.0),
    ("Caída leve    -1%..0%",   lambda x: -1.0 <= x < 0),
    ("Subida leve    0%..+1%",  lambda x: 0 <= x < 1.0),
    ("Subida fuerte  > +1%",    lambda x: x >= 1.0),
]
show_buckets(all_items, "momentum_1h", buckets_mom1h, "WR por momentum 1h", base_cap)
show_quartiles_v2(all_items, "momentum_1h", "Momentum 1h — cuartiles")

# ─────────────────────────────────────────────────────────────────────────────
# 4. ACELERACIÓN DE VOLUMEN (spike aislado vs build-up)
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'═'*70}")
print("  4. ACELERACIÓN DE VOLUMEN (vol_actual / media_3_previas)")
print(f"{'═'*70}")
print("  Bajo ratio = build-up gradual | Alto ratio = spike aislado")

buckets_accel = [
    ("< 2×   (build-up gradual)",    lambda x: x < 2.0),
    ("2–4×   (aceleración moderada)", lambda x: 2.0 <= x < 4.0),
    ("4–8×   (spike notable)",        lambda x: 4.0 <= x < 8.0),
    ("> 8×   (spike aislado)",        lambda x: x >= 8.0),
]
show_buckets(all_items, "vol_accel", buckets_accel, "WR por aceleración de volumen", base_cap)
show_quartiles_v2(all_items, "vol_accel", "Aceleración de volumen — cuartiles")

# ─────────────────────────────────────────────────────────────────────────────
# 5. MECHA ADVERSA
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'═'*70}")
print("  5. MECHA ADVERSA (contra la dirección del trade)")
print(f"{'═'*70}")
print("  LONG: mecha superior / rango  |  SHORT: mecha inferior / rango")

buckets_wick = [
    ("< 0.10  (mecha mínima)",     lambda x: x < 0.10),
    ("0.10–0.25 (mecha pequeña)",  lambda x: 0.10 <= x < 0.25),
    ("0.25–0.40 (mecha moderada)", lambda x: 0.25 <= x < 0.40),
    ("> 0.40  (mecha grande)",     lambda x: x >= 0.40),
]
show_buckets(all_items, "adverse_wick", buckets_wick, "WR por mecha adversa", base_cap)

# ─────────────────────────────────────────────────────────────────────────────
# 6. ALINEACIÓN DE LA VELA DE ENTRADA
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'═'*70}")
print("  6. ALINEACIÓN VELA DE ENTRADA vs DIRECCIÓN DEL TRADE")
print(f"{'═'*70}")
print("  Alineada = vela verde para LONG / vela roja para SHORT")

buckets_align = [
    ("Alineada     (vela confirma)", lambda x: x == 1),
    ("Contra-trend (vela opuesta)",  lambda x: x == 0),
]
show_buckets(all_items, "candle_aligned", buckets_align, "WR por alineación vela", base_cap)

# ─────────────────────────────────────────────────────────────────────────────
# 7. ATR vs TP REACHABLE
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'═'*70}")
print(f"  7. ATR vs TP REACHABLE  (ATR% / TP_spot%)  [TP={TP_PCT}% × {LEVERAGE}× lev]")
print(f"{'═'*70}")
print("  < 0.5 = volatilidad muy baja (TP difícil de alcanzar)")
print("  1.0   = ATR = exactamente el TP spot necesario")
print("  > 2.0 = alta volatilidad (puede llegar fácil, pero también al SL)")

buckets_atr = [
    ("< 0.5   (muy baja volatilidad)",  lambda x: x < 0.5),
    ("0.5–1.0 (volatilidad baja)",      lambda x: 0.5 <= x < 1.0),
    ("1.0–1.5 (volatilidad media)",     lambda x: 1.0 <= x < 1.5),
    ("1.5–2.5 (volatilidad alta)",      lambda x: 1.5 <= x < 2.5),
    ("> 2.5   (volatilidad muy alta)",  lambda x: x >= 2.5),
]
show_buckets(all_items, "atr_vs_tp", buckets_atr, "WR por ATR vs TP", base_cap)
show_quartiles_v2(all_items, "atr_vs_tp", "ATR vs TP — cuartiles")

# ─────────────────────────────────────────────────────────────────────────────
# 8. RACHA PREVIA DE PÉRDIDAS
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'═'*70}")
print("  8. RACHA DE PÉRDIDAS PREVIAS (consecutive losses antes de este trade)")
print(f"{'═'*70}")

buckets_streak = [
    ("0 pérdidas previas (tras ganancia)", lambda x: x == 0),
    ("1 pérdida previa",                  lambda x: x == 1),
    ("2 pérdidas previas",                lambda x: x == 2),
    ("3+ pérdidas previas",               lambda x: x >= 3),
]
show_buckets(all_items, "streak_losses", buckets_streak, "WR por racha previa", base_cap)

# ─────────────────────────────────────────────────────────────────────────────
# 9. HORA GRANULAR (buckets de 2h)
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'═'*70}")
print("  9. HORA UTC GRANULAR (bloques de 2h)")
print(f"{'═'*70}")

buckets_hour2h = [(f"{h:02d}h–{h+2:02d}h UTC", lambda x, h=h: x == h) for h in range(0, 24, 2)]
show_buckets(all_items, "hour_2h", buckets_hour2h, "WR por hora UTC (2h)", base_cap)

# ─────────────────────────────────────────────────────────────────────────────
# 10. CONVICCIÓN DE LAS VELAS PREVIAS (body ratio)
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'═'*70}")
print("  10. CONVICCIÓN PREVIA (body/rango promedio últimas 3 velas)")
print(f"{'═'*70}")
print("  Bajo = velas doji/indecisas  |  Alto = velas con cuerpo claro")

buckets_body = [
    ("< 0.25  (doji / indecisión)",   lambda x: x < 0.25),
    ("0.25–0.45 (moderado)",          lambda x: 0.25 <= x < 0.45),
    ("0.45–0.65 (buena convicción)",  lambda x: 0.45 <= x < 0.65),
    ("> 0.65  (velas muy claras)",    lambda x: x >= 0.65),
]
show_buckets(all_items, "prev_body_ratio", buckets_body, "WR por convicción previa", base_cap)
show_quartiles_v2(all_items, "prev_body_ratio", "Convicción previa — cuartiles")

# ─────────────────────────────────────────────────────────────────────────────
# RESUMEN — top mejores y peores zonas
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'═'*70}")
print("  RESUMEN — ZONAS CON MAYOR IMPACTO EN EQUITY")
print(f"{'═'*70}")
print("  (simulación post-hoc: filtrar solo ese bucket vs todo el universo)")
print()

# Calcular impacto de filtrar cada zona mala (WR < 25%)
from itertools import product as iterproduct

filter_candidates = [
    # (descripción, campo, función de filtro para EXCLUIR)
    ("RSI > 70 (sobrecompra profunda)",      "rsi",         lambda x: x >= 70),
    ("RSI < 30 (sobreventa profunda)",       "rsi",         lambda x: x < 30),
    ("Mecha adversa > 40%",                 "adverse_wick", lambda x: x >= 0.40),
    ("Spike aislado vol_accel > 8×",        "vol_accel",   lambda x: x >= 8.0),
    ("ATR vs TP < 0.5 (TP inalcanzable)",   "atr_vs_tp",   lambda x: x < 0.5),
    ("Lunes",                               "dow",          lambda x: x == 0),
    ("Domingo",                             "dow",          lambda x: x == 6),
    ("Vela contra-trend",                   "candle_aligned", lambda x: x == 0),
    ("RSI 60–70 (sobrecompra mod.)",        "rsi",         lambda x: 60 <= x < 70),
    ("RSI 30–40 (sobreventa mod.)",         "rsi",         lambda x: 30 <= x < 40),
    ("Momentum 1h > +1% fuerte subida",     "momentum_1h", lambda x: x >= 1.0),
    ("Momentum 1h < -1% fuerte bajada",     "momentum_1h", lambda x: x < -1.0),
    ("Vol accel < 2× (build-up lento)",     "vol_accel",   lambda x: x < 2.0),
    ("Racha 3+ pérdidas previas",           "streak_losses", lambda x: x >= 3),
]

print(f"  {'Filtro eliminado':<40} {'Trades quitados':>14}  {'WR restante':>11}  {'Capital':>10}  {'Mejora':>8}")
print(f"  {'─'*40} {'─'*14}  {'─'*11}  {'─'*10}  {'─'*8}")

interesting = []
for desc, col, bad_fn in filter_candidates:
    bad_items   = [it for it in all_items if it.get(col) is not None and bad_fn(it[col])]
    good_items  = [it for it in all_items if it.get(col) is None or not bad_fn(it[col])]
    if not bad_items or not good_items:
        continue
    r_bad   = simulate_equity(bad_items,  lambda x: True)
    r_good  = simulate_equity(good_items, lambda x: True)
    delta_pct = (r_good["capital"] - base_result["capital"]) / base_result["capital"] * 100
    marker = "▲" if delta_pct > 2 else ("▼" if delta_pct < -2 else "~")
    removed_pct = len(bad_items) / len(all_items) * 100
    print(f"  {desc:<40} {len(bad_items):>5} ({removed_pct:4.1f}%)  "
          f"WR {r_good['wr']:5.1f}%  ${r_good['capital']:>9,.0f}  {marker}{delta_pct:>+6.1f}%")
    if delta_pct > 3:
        interesting.append((desc, col, bad_fn, delta_pct, r_good))

# ─────────────────────────────────────────────────────────────────────────────
# COMBINACIONES de los 2 mejores filtros nuevos
# ─────────────────────────────────────────────────────────────────────────────
if len(interesting) >= 2:
    print(f"\n  ── COMBINACIONES (top filtros nuevos) ──")
    top2 = interesting[:3]
    for i in range(len(top2)):
        for j in range(i + 1, len(top2)):
            d1, c1, f1, _, _ = top2[i]
            d2, c2, f2, _, _ = top2[j]
            good = [it for it in all_items
                    if (it.get(c1) is None or not f1(it[c1])) and
                       (it.get(c2) is None or not f2(it[c2]))]
            bad  = len(all_items) - len(good)
            if not good:
                continue
            r = simulate_equity(good, lambda x: True)
            d = (r["capital"] - base_result["capital"]) / base_result["capital"] * 100
            print(f"  Excluir [{d1[:20]}] + [{d2[:20]}]")
            print(f"    Quitados: {bad} ({bad/len(all_items)*100:.1f}%)  "
                  f"WR: {r['wr']:.1f}%  Capital: ${r['capital']:,.0f}  Δ: {d:+.1f}%\n")

print(f"\n{'═'*70}")
print("  FIN DEL ANÁLISIS v2")
print(f"{'═'*70}\n")
