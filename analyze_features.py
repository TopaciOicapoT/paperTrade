"""
analyze_features.py
-------------------
Análisis profundo de las condiciones de ENTRADA para cada trade:
  1. Volumen real en USDT (dinero efectivo moviendose, no solo el ratio)
  2. ATR — volatilidad en el momento de entrada (¿puede el precio llegar al 3%?)
  3. Cuerpo de la vela de entrada — momentum de la rotura
  4. Hora del día / sesión de mercado
  5. Momentum de precio en los 5 minutos previos

Además compara 3 escenarios de TP/SL:
  - Actual:      TP=3.0% / SL=1.0%
  - Intermedio:  TP=1.5% / SL=0.5%
  - Ajustado:    TP=1.0% / SL=0.3%

Objetivo: encontrar el patrón que predice si la rotura llegará a +3%
o se quedará en +1-1.5%, para adaptar TP/SL dinámicamente.
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

SYMBOLS   = ["ADA/USDT", "LINK/USDT", "EGLD/USDT"]
DAYS      = 1095   # 3 años (suficiente muestra estadística, más rápido que 6yr)
LEVERAGE  = config["futures"].get("leverage", 3)
FEE       = config.get("paper_trading", {}).get("fee_pct", 0.1)
MLOOKBACK = config["levels"].get("monthly_lookback", 6)
EXTRA     = MLOOKBACK * 30 + 30
VOL_MIN   = config["levels"].get("volume_trigger_ratio", 2.0)
VOL_MAX   = config["levels"].get("volume_trigger_ratio_max", 3.0)

SCENARIOS = [
    {"tp": 3.0, "sl": 1.0,  "label": "Actual     TP=3.0% / SL=1.0%"},
    {"tp": 1.5, "sl": 0.5,  "label": "Intermedio TP=1.5% / SL=0.5%"},
    {"tp": 1.0, "sl": 0.3,  "label": "Ajustado   TP=1.0% / SL=0.3%"},
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def compute_features(trade, df5m: pd.DataFrame) -> dict:
    """
    Calcula features de entrada para un trade dado.
    Toda la información está disponible ANTES de abrir el trade (sin look-ahead).
    """
    idx = trade.entry_index
    if idx < 20:
        return {}

    candle  = df5m.iloc[idx]
    window  = df5m.iloc[max(0, idx - 50): idx + 1]   # 50 velas previas
    atr_win = df5m.iloc[max(0, idx - 14): idx + 1]   # 14 velas para ATR

    # 1. ATR como % del precio — mide hasta dónde puede moverse el precio
    true_ranges = []
    for j in range(1, len(atr_win)):
        hi  = float(atr_win.iloc[j]["high"])
        lo  = float(atr_win.iloc[j]["low"])
        pc  = float(atr_win.iloc[j - 1]["close"])
        true_ranges.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    atr_pct = (np.mean(true_ranges) / float(candle["close"]) * 100) if true_ranges else 0.0

    # 2. Volumen en USDT (dinero real moviendose en esa vela)
    usdt_vol      = float(candle["volume"]) * float(candle["close"])
    usdt_vol_mean = (window["volume"] * window["close"]).mean()
    usdt_norm     = usdt_vol / usdt_vol_mean if usdt_vol_mean > 0 else 1.0

    # 3. Cuerpo de la vela de entrada (momentum: cuánto cerró respecto al rango)
    wick = float(candle["high"]) - float(candle["low"])
    body = abs(float(candle["close"]) - float(candle["open"]))
    body_ratio = body / wick if wick > 0 else 0.5

    # 4. Hora del día (UTC) — sesión
    hour = df5m.index[idx].hour

    # 5. Momentum de precio: variación en las últimas 5 velas (25 min)
    if idx >= 5:
        prev5 = float(df5m.iloc[idx - 5]["close"])
        momentum_5 = (float(candle["close"]) - prev5) / prev5 * 100
    else:
        momentum_5 = 0.0

    return {
        "atr_pct":     round(atr_pct, 4),
        "usdt_vol_M":  round(usdt_vol / 1e6, 4),     # en millones
        "usdt_norm":   round(usdt_norm, 3),            # normalizado vs media 50 velas
        "body_ratio":  round(body_ratio, 3),
        "hour":        hour,
        "momentum_5":  round(momentum_5, 4),
        "vol_ratio":   trade.volume_ratio,             # ratio ya calculado por el motor
    }


def session(hour: int) -> str:
    if 0  <= hour < 8:  return "Asia    (00-08h)"
    if 8  <= hour < 14: return "Europa  (08-14h)"
    if 14 <= hour < 20: return "America (14-20h)"
    return "Noche   (20-24h)"


def wr_bar(wins, total, width=20) -> str:
    if total == 0:
        return "  —  "
    pct = wins / total * 100
    filled = int(pct / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar}  {pct:5.1f}%  ({wins}/{total})"


def show_feature_quartiles(df: pd.DataFrame, col: str, label: str):
    """Muestra WR por cuartil de un feature continuo."""
    valid = df[df[col].notna() & df[col].ne(0)].copy()
    if len(valid) < 40:
        return
    try:
        valid["_q"], bins = pd.qcut(valid[col], 4, retbins=True, duplicates="drop")
    except ValueError:
        return

    print(f"\n  ── {label}")
    print(f"  {'Cuartil':<28} {'Win Rate':>6}  (barra de 20 chars)")
    for q in valid["_q"].cat.categories:
        bucket = valid[valid["_q"] == q]
        wins   = (bucket["result"] == "win").sum()
        total  = len(bucket)
        lo     = bucket[col].min()
        hi     = bucket[col].max()
        print(f"  {lo:>7.3f}–{hi:<7.3f}         {wr_bar(wins, total)}")


def run_scenario(sym, df5m, df1d, df1w, tp, sl, lev):
    return simular_trades(
        df_entry=df5m,
        df_daily=df1d,
        df_weekly=df1w,
        symbol=sym,
        monthly_lookback=MLOOKBACK,
        tp_pct=tp,
        sl_behind_pct=sl,
        volume_ratio_min=VOL_MIN,
        volume_ratio_max=VOL_MAX,
        fee_pct=FEE,
        ml_threshold=0.0,
        volatility_filter=False,
        leverage=lev,
        initial_capital=1000.0,
        failed_retest_filter=True,
        symbol_params=config.get("symbol_params"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

exchange = get_data_exchange()

all_features   = []   # para análisis global
scenario_table = []   # para tabla de escenarios

print(f"\n{'═'*62}")
print(f"  ANÁLISIS DE FEATURES DE ENTRADA — {DAYS} días ({DAYS//365} años)")
print(f"{'═'*62}")

for sym in SYMBOLS:
    print(f"\n\n{'─'*62}")
    print(f"  {sym}")
    print(f"{'─'*62}")

    df5m = fetch_ohlcv(sym, "5m", limit=DAYS * 24 * 12, exchange=exchange)
    df1d = fetch_ohlcv(sym, "1d", limit=DAYS + EXTRA,   exchange=exchange)
    df1w = fetch_ohlcv(sym, "1w", limit=52,              exchange=exchange)

    # ── 1. Escenarios TP/SL ──────────────────────────────────────────────────
    print(f"\n  ESCENARIOS TP/SL  (mismo capital $1000, mismo símbolo)")
    print(f"  {'Escenario':<38} {'Trades':>6} {'WR':>7} {'PF':>6} {'Capital final':>14}")
    print(f"  {'─'*38} {'──────':>6} {'──────':>7} {'────':>6} {'────────────':>14}")

    base_result = None
    for sc in SCENARIOS:
        res = run_scenario(sym, df5m, df1d, df1w, sc["tp"], sc["sl"], LEVERAGE)
        closed = [t for t in res.trades if t.result != "open"]
        wins   = sum(1 for t in closed if t.result == "win")
        total  = len(closed)
        wr     = wins / total * 100 if total else 0
        cap    = 1000 * (1 + res.total_return_pct / 100)

        print(f"  {sc['label']:<38} {total:>6}  {wr:>6.1f}%  {res.profit_factor:>5.2f}   ${cap:>10,.2f}")
        scenario_table.append({
            "symbol": sym, "label": sc["label"],
            "trades": total, "wr": wr, "pf": res.profit_factor,
            "capital": cap, "tp": sc["tp"], "sl": sc["sl"],
        })
        if sc["tp"] == 3.0:
            base_result = res
            base_df5m   = df5m

    # ── 2. Features de entrada (escenario base TP=3%/SL=1%) ─────────────────
    if base_result is None:
        continue

    rows = []
    for t in base_result.trades:
        if t.result == "open":
            continue
        feats = compute_features(t, base_df5m)
        if not feats:
            continue
        rows.append({**feats, "result": t.result, "symbol": sym,
                     "direction": t.direction, "bars": t.bars_to_exit})
        all_features.append({**feats, "result": t.result, "symbol": sym,
                              "direction": t.direction, "bars": t.bars_to_exit})

    df = pd.DataFrame(rows)
    wins_df   = df[df["result"] == "win"]
    losses_df = df[df["result"] == "loss"]

    print(f"\n  MEDIAS DE FEATURES  (ganadores vs perdedores)")
    print(f"  {'Feature':<22} {'Ganadores':>12} {'Perdedores':>12} {'Diferencia':>12}")
    print(f"  {'─'*22} {'────────':>12} {'──────────':>12} {'──────────':>12}")

    feats_info = [
        ("atr_pct",    "ATR % precio"),
        ("usdt_vol_M", "Volumen USDT (M$)"),
        ("usdt_norm",  "Vol USDT norm."),
        ("body_ratio", "Cuerpo vela (0-1)"),
        ("momentum_5", "Momentum 5 velas"),
        ("vol_ratio",  "Ratio vol (engine)"),
    ]
    for col, lbl in feats_info:
        w_mean = wins_df[col].mean() if col in wins_df.columns else 0
        l_mean = losses_df[col].mean() if col in losses_df.columns else 0
        diff   = w_mean - l_mean
        sign   = "+" if diff >= 0 else ""
        print(f"  {lbl:<22} {w_mean:>12.4f} {l_mean:>12.4f} {sign}{diff:>11.4f}")

    # ── 3. WR por cuartil de cada feature ───────────────────────────────────
    print(f"\n  WIN RATE POR CUARTIL DE CADA FEATURE")

    show_feature_quartiles(df, "atr_pct",    "ATR % precio  (Q1=baja volatilidad → Q4=alta)")
    show_feature_quartiles(df, "usdt_vol_M", "Volumen USDT real  (Q1=pocos $M → Q4=muchos $M)")
    show_feature_quartiles(df, "usdt_norm",  "Volumen USDT normalizado  (Q1=por debajo media → Q4=muy por encima)")
    show_feature_quartiles(df, "body_ratio", "Cuerpo de vela  (Q1=indecisión → Q4=vela sólida)")
    show_feature_quartiles(df, "momentum_5", "Momentum 5 velas  (Q1=bajada → Q4=subida)")

    # ── 4. WR por sesión de mercado ──────────────────────────────────────────
    print(f"\n  ── WR por sesión de mercado (hora UTC)")
    for ses in ["Asia    (00-08h)", "Europa  (08-14h)", "America (14-20h)", "Noche   (20-24h)"]:
        bucket = df[df["hour"].apply(session) == ses]
        wins   = (bucket["result"] == "win").sum()
        total  = len(bucket)
        print(f"  {ses}   {wr_bar(wins, total)}")

    # ── 5. ATR cruzado con TP alcanzable ────────────────────────────────────
    print(f"\n  ── ¿El ATR predice si puede llegar al 3%?")
    print(f"     (ATR = movimiento típico por vela de 5min)")
    df["atr_vs_tp"] = (df["atr_pct"] * 14)   # ATR proyectado a 14 velas (70 min)
    for label, lo, hi in [("<1.5% proyectado", 0, 1.5), ("1.5-3%", 1.5, 3.0), (">3%", 3.0, 99)]:
        bucket = df[(df["atr_vs_tp"] >= lo) & (df["atr_vs_tp"] < hi)]
        wins   = (bucket["result"] == "win").sum()
        total  = len(bucket)
        atr_m  = bucket["atr_pct"].mean() if total else 0
        print(f"  ATR×14 {label:<18} {wr_bar(wins, total)}  (ATR medio: {atr_m:.3f}%/vela)")


# ─────────────────────────────────────────────────────────────────────────────
# Resumen global — todos los símbolos combinados
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n\n{'═'*62}")
print(f"  RESUMEN GLOBAL — {len(all_features)} trades de {len(SYMBOLS)} símbolos")
print(f"{'═'*62}")

df_all = pd.DataFrame(all_features)

# Escenarios totales
print(f"\n  ESCENARIOS TP/SL — capital total acumulado ($1000 × 3 símbolos)")
sc_agg = pd.DataFrame(scenario_table)
for tp_val in [3.0, 1.5, 1.0]:
    grp = sc_agg[sc_agg["tp"] == tp_val]
    total_cap = grp["capital"].sum()
    avg_wr    = grp["wr"].mean()
    avg_pf    = grp["pf"].mean()
    label     = grp["label"].iloc[0]
    print(f"  {label:<38}  WR {avg_wr:.1f}%  PF {avg_pf:.2f}  → $3k→${total_cap:,.0f}")

# Features globales
print(f"\n  FEATURES GLOBALES — ganadores vs perdedores")
wins_all   = df_all[df_all["result"] == "win"]
losses_all = df_all[df_all["result"] == "loss"]

print(f"\n  {'Feature':<22} {'Ganadores':>12} {'Perdedores':>12} {'Diferencia':>12}  {'Señal'}")
print(f"  {'─'*22} {'────────':>12} {'──────────':>12} {'──────────':>12}  {'─────'}")

for col, lbl in feats_info:
    w_mean = wins_all[col].mean()
    l_mean = losses_all[col].mean()
    diff   = w_mean - l_mean
    pct_diff = diff / l_mean * 100 if l_mean != 0 else 0
    sign   = "+" if diff >= 0 else ""
    signal = ("↑ ganadores" if pct_diff > 5 else
              "↓ ganadores" if pct_diff < -5 else "~  igual")
    print(f"  {lbl:<22} {w_mean:>12.4f} {l_mean:>12.4f} {sign}{diff:>11.4f}  {signal} ({sign}{pct_diff:.0f}%)")

# ATR global — el más importante
print(f"\n  ATR GLOBAL (feature más predictivo para TP dinámico):")
show_feature_quartiles(df_all, "atr_pct",   "ATR % precio — todos los símbolos")
show_feature_quartiles(df_all, "usdt_norm", "Volumen USDT normalizado — todos")
show_feature_quartiles(df_all, "body_ratio","Cuerpo de vela — todos")

# Correlaciones (Pearson) con resultado
df_all["win_bin"] = (df_all["result"] == "win").astype(int)
print(f"\n  CORRELACIÓN con resultado (WIN=1 / LOSS=0):")
for col, lbl in feats_info:
    try:
        corr = df_all[col].corr(df_all["win_bin"])
        print(f"    {lbl:<22}  r = {corr:+.4f}")
    except Exception:
        pass

print(f"\n{'═'*62}")
print(f"  CONCLUSIÓN / REGLAS SUGERIDAS")
print(f"{'═'*62}")
print(f"""
  Interpretar los cuartiles de ATR:
  ─ Q1 (ATR bajo)  → precio en modo lateral/bajo rango
                     → 3% TP es poco realista en tiempo razonable
                     → USAR TP=1.0-1.5% / SL=0.3-0.5%

  ─ Q4 (ATR alto)  → precio en movimiento fuerte
                     → 3% TP factible en 1-4h
                     → MANTENER TP=3% / SL=1%

  Volumen USDT normalizado (usdt_norm):
  ─ Q1 (< media)   → rotura con poco dinero real → fakeout probable
  ─ Q4 (> 3× media) → institucionales/ballenas entrando → seguimiento probable

  Regla práctica sugerida (validar con backtest tras implementar):
    if atr_pct < umbral_bajo AND usdt_norm < 1.5:
        tp = 1.0%, sl = 0.3%   ← tomar ganancia pequeña segura
    elif atr_pct > umbral_alto OR usdt_norm > 3.0:
        tp = 3.0%, sl = 1.0%   ← dejar correr
    else:
        tp = 1.5%, sl = 0.5%   ← intermedio
""")
