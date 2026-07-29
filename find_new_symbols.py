"""
find_new_symbols.py
-------------------
Evalúa si símbolos descartados pueden ser rescatados con los filtros validados.

Para cada símbolo candidato:
  1. Backtest baseline (3yr, sin filtros)
  2. Auto-descubre la sesión UTC con peor WR y la bloquea (F2-auto)
  3. Bloquea volumen Q3 [2.1-2.7×] (F3)
  4. Bloquea momentum Q3 [+0.3,+1.6%] (F1)
  5. Combinado óptimo (F2-auto + F3 + F1 si mejora)
  6. Criterio de aceptación: WR ≥ 30%, PF ≥ 1.20, capital_final > baseline

Candidatos elegidos:
  - ICP/USDT  → el único símbolo descartado que era positivo a 3yr (PF 1.13)
  - NEAR/USDT → 133 trades (muestra grande), cerca del breakeven (PF 1.02)
  - BTC/USDT  → mercado institucional, patrones de sesión posiblemente claros
"""

import sys
import yaml
import pandas as pd
import numpy as np
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

CANDIDATES = ["FIL/USDT", "HBAR/USDT", "INJ/USDT"]   # Segunda tanda — nunca probados
DAYS       = 2190   # 6 años
LEVERAGE   = config["futures"].get("leverage", 3)
FEE        = config.get("paper_trading", {}).get("fee_pct", 0.1)
MLOOKBACK  = config["levels"].get("monthly_lookback", 6)
EXTRA      = MLOOKBACK * 30 + 30
VOL_MIN    = config["levels"].get("volume_trigger_ratio", 2.0)
VOL_MAX    = config["levels"].get("volume_trigger_ratio_max", 3.0)

SESSION_LABELS = {
    "Asia    (00-08h)": (0, 8),
    "Europa  (08-14h)": (8, 14),
    "America (14-20h)": (14, 20),
    "Noche   (20-24h)": (20, 24),
}


# ─────────────────────────────────────────────────────────────────────────────
# Feature extraction
# ─────────────────────────────────────────────────────────────────────────────

def compute_features(trade, df5m):
    idx = trade.entry_index
    if idx < 20:
        return None
    candle   = df5m.iloc[idx]
    window   = df5m.iloc[max(0, idx - 50): idx + 1]
    usdt_vol = float(candle["volume"]) * float(candle["close"])
    usdt_mean = (window["volume"] * window["close"]).mean()
    usdt_norm = usdt_vol / usdt_mean if usdt_mean > 0 else 1.0
    momentum_5 = 0.0
    if idx >= 5:
        prev5 = float(df5m.iloc[idx - 5]["close"])
        momentum_5 = (float(candle["close"]) - prev5) / prev5 * 100
    hour = df5m.index[idx].hour
    # RSI 14 (simplificado, no suavizado)
    rsi14 = 50.0
    if idx >= 14:
        closes14 = df5m["close"].iloc[idx - 13: idx + 1].astype(float).values
        deltas = np.diff(closes14)
        avg_gain = np.where(deltas > 0, deltas, 0.0).mean()
        avg_loss = np.where(deltas < 0, -deltas, 0.0).mean()
        rsi14 = (100.0 - 100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss > 0 else 100.0
    return {"usdt_norm": round(usdt_norm, 4),
            "momentum_5": round(momentum_5, 4),
            "hour": hour,
            "rsi14": round(rsi14, 1)}


def run_backtest(sym, df5m, df1d, df1w):
    return simular_trades(
        df_entry=df5m, df_daily=df1d, df_weekly=df1w,
        symbol=sym, monthly_lookback=MLOOKBACK,
        tp_pct=config["risk"]["take_profit_pct"],
        sl_behind_pct=config["risk"]["sl_behind_level_pct"],
        volume_ratio_min=VOL_MIN, volume_ratio_max=VOL_MAX,
        fee_pct=FEE, ml_threshold=0.0, volatility_filter=False,
        leverage=LEVERAGE, initial_capital=1000.0,
        failed_retest_filter=True,
        symbol_params=None,   # Sin filtros pre-configurados: análisis puro
        rsi_overbought_block=None,
    )


def simulate_equity(items, filter_fn, initial=1000.0):
    capital = initial
    wins = used = skipped = 0
    for item in items:
        if item["feat"] is None or not filter_fn(item["feat"]):
            skipped += 1
            continue
        capital *= (1 + item["trade"].pnl_pct / 100.0)
        used += 1
        if item["trade"].result == "win":
            wins += 1
    losses = used - wins
    pf_val = (
        (wins * (config["risk"]["take_profit_pct"] * LEVERAGE / 100 - FEE * 2 / 100))
        / (losses * (config["risk"]["sl_behind_level_pct"] * LEVERAGE / 100 + FEE * 2 / 100))
        if losses > 0 else 99.0
    )
    wr = wins / used * 100 if used else 0
    return {"capital": capital, "used": used, "skipped": skipped,
            "wins": wins, "losses": losses, "wr": wr, "pf": pf_val}


def wr_bar(wins, total, width=16):
    if not total:
        return "   —  "
    pct = wins / total * 100
    filled = int(pct / 100 * width)
    return f"{'█'*filled}{'░'*(width-filled)} {pct:5.1f}% ({wins}/{total})"


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

exchange  = get_data_exchange()
accepted  = []   # símbolos que pasan el criterio

print(f"\n{'═'*68}")
print(f"  BÚSQUEDA DE NUEVOS SÍMBOLOS — filtros auto-descubiertos")
print(f"  Período: {DAYS} días ({DAYS//365} años) | 3× leverage | $1000 capital")
print(f"  Criterio: WR ≥ 30% AND PF ≥ 1.20 AND capital > baseline")
print(f"{'═'*68}")

for sym in CANDIDATES:
    print(f"\n\n{'─'*68}")
    print(f"  {sym}")
    print(f"{'─'*68}")

    df5m = fetch_ohlcv(sym, "5m", limit=DAYS * 24 * 12, exchange=exchange)
    df1d = fetch_ohlcv(sym, "1d", limit=DAYS + EXTRA,   exchange=exchange)
    df1w = fetch_ohlcv(sym, "1w", limit=52,              exchange=exchange)

    res = run_backtest(sym, df5m, df1d, df1w)
    closed = [t for t in res.trades if t.result != "open"]

    if not closed:
        print("  Sin trades — saltando")
        continue

    wins_all  = sum(1 for t in closed if t.result == "win")
    base_cap  = 1000 * (1 + res.total_return_pct / 100)
    base_wr   = wins_all / len(closed) * 100

    print(f"\n  Baseline: {len(closed)} trades | WR {base_wr:.1f}% | "
          f"PF {res.profit_factor:.2f} | ${base_cap:,.0f} ({res.total_return_pct:+.1f}%)")

    # Construir items con features
    items = []
    for t in closed:
        feat = compute_features(t, df5m)
        items.append({"trade": t, "feat": feat})

    # ── 1. WR por sesión (auto-descubrimiento) ─────────────────────────────
    print(f"\n  WR por sesión UTC:")
    session_wr = {}
    for ses_label, (lo, hi) in SESSION_LABELS.items():
        bucket = [it for it in items if it["feat"] and lo <= it["feat"]["hour"] < hi]
        w = sum(1 for it in bucket if it["trade"].result == "win")
        n = len(bucket)
        session_wr[ses_label] = (w, n, (lo, hi))
        print(f"    {ses_label}  {wr_bar(w, n)}")

    # Auto-elegir la peor sesión a bloquear (la de menor WR con ≥ 10 trades)
    worst_session = min(
        ((lbl, w/n, rng) for lbl, (w, n, rng) in session_wr.items() if n >= 10),
        key=lambda x: x[1],
        default=None,
    )
    if worst_session:
        ws_label, ws_wr, ws_range = worst_session
        lo_h, hi_h = ws_range
        print(f"\n  → Peor sesión: {ws_label} (WR {ws_wr*100:.1f}%) — candidata a bloquear")

    # ── 2. WR por volumen normalizado ──────────────────────────────────────
    print(f"\n  WR por volumen normalizado (Q3 trampa = 2.1-2.7×):")
    vol_buckets = [
        ("< 1.0×  (bajo media)",    lambda n: n < 1.0),
        ("1.0–1.7× (ligeramente)",  lambda n: 1.0 <= n < 1.7),
        ("1.7–2.1× (moderado)",     lambda n: 1.7 <= n < 2.1),
        ("2.1–2.7× (Q3 trampa)",    lambda n: 2.1 <= n < 2.7),
        ("> 2.7×  (muy alto)",      lambda n: n >= 2.7),
    ]
    for lbl, fn in vol_buckets:
        bucket = [it for it in items if it["feat"] and fn(it["feat"]["usdt_norm"])]
        w = sum(1 for it in bucket if it["trade"].result == "win")
        n = len(bucket)
        if n:
            print(f"    {lbl:<28}  {wr_bar(w, n)}")

    # ── 3. WR por momentum ─────────────────────────────────────────────────
    print(f"\n  WR por momentum 5 velas:")
    mom_buckets = [
        ("bajada fuerte  (< -1%)",     lambda m: m < -1.0),
        ("neutral       (-1%..+0.3%)", lambda m: -1.0 <= m < 0.3),
        ("moderado       +0.3..+1.6%", lambda m: 0.3 <= m <= 1.6),
        ("subida fuerte  (> +1.6%)",   lambda m: m > 1.6),
    ]
    for lbl, fn in mom_buckets:
        bucket = [it for it in items if it["feat"] and fn(it["feat"]["momentum_5"])]
        w = sum(1 for it in bucket if it["trade"].result == "win")
        n = len(bucket)
        if n:
            print(f"    {lbl:<30}  {wr_bar(w, n)}")

    # ── 4. Simulación de escenarios ────────────────────────────────────────
    print(f"\n  Escenarios de filtros:")
    print(f"  {'Escenario':<38} {'Usados':>6} {'Sltd':>5} {'WR':>7} {'PF':>5} "
          f"{'Capital':>10} {'Δ':>8}")
    print(f"  {'─'*38} {'──────':>6} {'────':>5} {'──────':>7} {'────':>5} "
          f"{'────────':>10} {'──────':>8}")

    def show(label, fn):
        r = simulate_equity(items, fn)
        d = (r["capital"] - base_cap) / base_cap * 100
        mk = "▲" if d > 2 else ("▼" if d < -2 else "~")
        print(f"  {label:<38} {r['used']:>6} {r['skipped']:>5} {r['wr']:>6.1f}%"
              f" {r['pf']:>5.2f} ${r['capital']:>9,.0f}  {mk}{d:>+6.1f}%")
        return r

    show("Baseline",                           lambda f: True)

    # F2-auto: bloquear la peor sesión del símbolo
    r_ses = None
    if worst_session:
        lo_h, hi_h = ws_range
        r_ses = show(f"F2-auto: bloquear {ws_label[:14]}",
                     lambda f, l=lo_h, h=hi_h: not (l <= f["hour"] < h))

    # F3: volumen Q3
    r_vol = show("F3: Vol Q3 block [2.1-2.7×]",
                 lambda f: not (2.1 <= f["usdt_norm"] <= 2.7))

    # F1: momentum Q3
    r_mom = show("F1: Momentum Q3 [+0.3,+1.6%]",
                 lambda f: not (0.3 <= f["momentum_5"] <= 1.6))

    # F4: RSI14 sobrecompra > 70
    r_rsi = show("F4: RSI14 > 70 block",
                 lambda f: f.get("rsi14", 50) < 70)

    # Combinado F2-auto + F3
    if worst_session:
        lo_h, hi_h = ws_range
        r_comb = show("F2-auto + F3",
                      lambda f, l=lo_h, h=hi_h: (
                          not (l <= f["hour"] < h) and
                          not (2.1 <= f["usdt_norm"] <= 2.7)
                      ))

    # Combinado F2-auto + F3 + F1
    if worst_session:
        lo_h, hi_h = ws_range
        r_all = show("F2-auto + F3 + F1",
                     lambda f, l=lo_h, h=hi_h: (
                         not (l <= f["hour"] < h) and
                         not (2.1 <= f["usdt_norm"] <= 2.7) and
                         not (0.3 <= f["momentum_5"] <= 1.6)
                     ))

    # Combinado F2-auto + F3 + F1 + F4
    if worst_session:
        lo_h, hi_h = ws_range
        r_all4 = show("F2+F3+F1+F4(RSI)",
                      lambda f, l=lo_h, h=hi_h: (
                          not (l <= f["hour"] < h) and
                          not (2.1 <= f["usdt_norm"] <= 2.7) and
                          not (0.3 <= f["momentum_5"] <= 1.6) and
                          f.get("rsi14", 50) < 70
                      ))

    # ── 5. Criterio de aceptación ──────────────────────────────────────────
    # Tomar el mejor resultado de los combinados
    candidates_res = []
    if worst_session:
        candidates_res.append(("F2+F3+F1+F4", r_all4))
        candidates_res.append(("F2-auto+F3+F1", r_all))
        candidates_res.append(("F2-auto+F3",    r_comb))
    candidates_res.append(("F4-RSI",   r_rsi))
    candidates_res.append(("F3-only",   r_vol))

    best_label, best_r = max(candidates_res, key=lambda x: x[1]["capital"])
    passes = (best_r["wr"] >= 30.0 and best_r["pf"] >= 1.20
              and best_r["capital"] > base_cap)

    print(f"\n  {'─'*68}")
    if passes:
        print(f"  ✅ ACEPTADO — {sym} supera el criterio con {best_label}")
        print(f"     WR {best_r['wr']:.1f}%  PF {best_r['pf']:.2f}  "
              f"${best_r['capital']:,.0f} (base ${base_cap:,.0f})")
        if worst_session:
            print(f"     Bloquear sesión: {ws_label} ({lo_h}–{hi_h}h UTC)")
        accepted.append({
            "sym": sym,
            "combo": best_label,
            "wr": best_r["wr"],
            "pf": best_r["pf"],
            "capital": best_r["capital"],
            "base_capital": base_cap,
            "session_block": (lo_h, hi_h) if worst_session else None,
            "session_label": ws_label if worst_session else None,
        })
    else:
        print(f"  ❌ RECHAZADO — {sym} no supera WR≥30% y PF≥1.20 con ningún filtro")
        print(f"     Mejor resultado: {best_label} → WR {best_r['wr']:.1f}% "
              f"PF {best_r['pf']:.2f} ${best_r['capital']:,.0f}")


# ─────────────────────────────────────────────────────────────────────────────
# Resumen final
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n\n{'═'*68}")
print(f"  RESUMEN FINAL")
print(f"{'═'*68}")

if accepted:
    print(f"\n  Símbolos que pasan el filtro de aceptación:")
    for a in accepted:
        delta = (a["capital"] / a["base_capital"] - 1) * 100
        print(f"\n  ✅ {a['sym']}")
        print(f"     Combo:    {a['combo']}")
        print(f"     WR:       {a['wr']:.1f}%  |  PF: {a['pf']:.2f}")
        print(f"     Capital:  ${a['capital']:,.0f}  (base ${a['base_capital']:,.0f}, "
              f"mejora {delta:+.1f}%)")
        if a["session_block"]:
            lo_h, hi_h = a["session_block"]
            print(f"     Config:   session_block_hours: [{lo_h}, {hi_h}]  # {a['session_label']}")
else:
    print(f"\n  Ninguno de los 3 candidatos supera el criterio.")
    print(f"  La cartera actual (ADA + LINK + EGLD) sigue siendo la óptima.")
