"""
analyze_patterns.py
-------------------
Analiza por qué la estrategia gana o pierde:
  - Win rate por dirección (LONG vs SHORT)
  - Win rate por ratio de volumen
  - Win rate por mes del año
  - Win rate por distancia al nivel mensual
  - Distribución de duración de trades (bars_to_exit)

Uso:
    python analyze_patterns.py
"""

import sys
import yaml
import pandas as pd
from pathlib import Path
from loguru import logger
from collections import defaultdict

# Silenciar logs de DEBUG del motor durante el análisis
logger.remove()
logger.add(sys.stderr, level="WARNING")

from data.fetcher import get_data_exchange, fetch_ohlcv
from backtesting.engine import simular_trades

CONFIG_PATH = Path("config/config.yaml")

# ── Símbolos a analizar ────────────────────────────────────────────────────────
WINNERS = ["ADA/USDT", "LINK/USDT", "EGLD/USDT"]
LOSERS  = ["BTC/USDT", "DOT/USDT", "DOGE/USDT"]
DAYS    = 1095   # 3 años

# ── Cargar config ──────────────────────────────────────────────────────────────
with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)

lev      = config["futures"].get("leverage", 3)
tp       = config["risk"].get("take_profit_pct", 3.0)
sl       = config["risk"].get("sl_behind_level_pct", 1.0)
vol_min  = config["levels"].get("volume_trigger_ratio", 2.0)
vol_max  = config["levels"].get("volume_trigger_ratio_max", 3.0)
fee      = config.get("paper_trading", {}).get("fee_pct", 0.1)
mlookback = config["levels"].get("monthly_lookback", 6)
extra    = mlookback * 30 + 30

# ── Función auxiliar ──────────────────────────────────────────────────────────
def bucket_vol(vr: float) -> str:
    if vr < 2.2:  return "2.0-2.2x"
    if vr < 2.5:  return "2.2-2.5x"
    if vr < 2.8:  return "2.5-2.8x"
    return "2.8-3.0x"

def bucket_dist(d: float) -> str:
    if d < 0.5:  return "<0.5%"
    if d < 1.0:  return "0.5-1%"
    if d < 2.0:  return "1-2%"
    return ">2%"

def bucket_bars(b: int) -> str:
    if b < 60:    return "<1h"
    if b < 240:   return "1-4h"
    if b < 1440:  return "4h-1d"
    return ">1d"

def wr_str(wins, total):
    if total == 0: return "  —  "
    return f"{wins/total*100:5.1f}% ({wins}/{total})"

def analizar_trades(trades, label):
    closed = [t for t in trades if t.result != "open"]
    if not closed:
        print(f"  {label}: sin trades")
        return

    total = len(closed)
    wins  = sum(1 for t in closed if t.result == "win")
    wr    = wins / total * 100

    # ── Por dirección ──
    longs  = [t for t in closed if t.direction == "long"]
    shorts = [t for t in closed if t.direction == "short"]
    lw = sum(1 for t in longs  if t.result == "win")
    sw = sum(1 for t in shorts if t.result == "win")

    # ── Por volumen ──
    vol_buckets = defaultdict(lambda: [0, 0])
    for t in closed:
        b = bucket_vol(t.volume_ratio)
        vol_buckets[b][1] += 1
        if t.result == "win": vol_buckets[b][0] += 1

    # ── Por mes ──
    month_buckets = defaultdict(lambda: [0, 0])
    for t in closed:
        if hasattr(t, "_entry_date"):
            m = f"M{t._entry_date.month:02d}"
        else:
            m = "??"
        month_buckets[m][1] += 1
        if t.result == "win": month_buckets[m][0] += 1

    # ── Por distancia al nivel ──
    dist_buckets = defaultdict(lambda: [0, 0])
    for t in closed:
        b = bucket_dist(t.level_distance_pct)
        dist_buckets[b][1] += 1
        if t.result == "win": dist_buckets[b][0] += 1

    # ── Por duración ──
    bar_buckets = defaultdict(lambda: [0, 0])
    for t in closed:
        b = bucket_bars(t.bars_to_exit)
        bar_buckets[b][1] += 1
        if t.result == "win": bar_buckets[b][0] += 1

    print(f"\n{'='*56}")
    print(f"  {label}  |  Trades: {total}  |  WR: {wr:.1f}%  |  LONG:{len(longs)} SHORT:{len(shorts)}")
    print(f"{'='*56}")

    print(f"\n  DIRECCIÓN:")
    print(f"    LONG   {wr_str(lw, len(longs))}")
    print(f"    SHORT  {wr_str(sw, len(shorts))}")

    print(f"\n  VOLUMEN (ratio spike):")
    for b in ["2.0-2.2x","2.2-2.5x","2.5-2.8x","2.8-3.0x"]:
        w,n = vol_buckets.get(b,[0,0])
        print(f"    {b}  {wr_str(w,n)}")

    print(f"\n  DISTANCIA al nivel mensual:")
    for b in ["<0.5%","0.5-1%","1-2%",">2%"]:
        w,n = dist_buckets.get(b,[0,0])
        print(f"    {b:8}  {wr_str(w,n)}")

    print(f"\n  DURACIÓN del trade (velas 1m):")
    for b in ["<1h","1-4h","4h-1d",">1d"]:
        w,n = bar_buckets.get(b,[0,0])
        print(f"    {b:8}  {wr_str(w,n)}")

    # ── Datos brutos para correlación ──
    vols  = [t.volume_ratio      for t in closed]
    dists = [t.level_distance_pct for t in closed]
    bars  = [t.bars_to_exit       for t in closed]
    res   = [1 if t.result=="win" else 0 for t in closed]

    df = pd.DataFrame({"vol":vols,"dist":dists,"bars":bars,"win":res})
    print(f"\n  CORRELACIÓN con resultado:")
    for col in ["vol","dist","bars"]:
        c = df["win"].corr(df[col])
        print(f"    {col:6}: r={c:+.3f}  {'↑ más→mejor' if c>0.05 else ('↓ más→peor' if c<-0.05 else '≈ sin efecto')}")

# ── Correr análisis ────────────────────────────────────────────────────────────
exchange = get_data_exchange()
symbol_params = config.get("symbol_params", {})

print("\n" + "█"*56)
print("  ANÁLISIS DE PATRONES — 3 AÑOS  |  Futuros 3x")
print("█"*56)

all_groups = [("GANADORES (3yr+)", WINNERS), ("PERDEDORES (3yr-)", LOSERS)]

for group_label, symbols in all_groups:
    print(f"\n\n{'▓'*56}")
    print(f"  {group_label}")
    print(f"{'▓'*56}")

    all_trades_group = []

    for symbol in symbols:
        print(f"\n  Cargando {symbol}...", end=" ", flush=True)
        import time as _time
        t0 = _time.time()

        df_5m  = fetch_ohlcv(symbol, "5m", limit=DAYS * 24 * 12, exchange=exchange)
        df_1d  = fetch_ohlcv(symbol, "1d", limit=DAYS + extra,    exchange=exchange)
        df_1w  = fetch_ohlcv(symbol, "1w", limit=156,             exchange=exchange)

        result = simular_trades(
            df_entry=df_5m,
            df_daily=df_1d,
            df_weekly=df_1w,
            symbol=symbol,
            monthly_lookback=mlookback,
            leverage=lev,
            tp_pct=tp,
            sl_behind_pct=sl,
            volume_ratio_min=vol_min,
            volume_ratio_max=vol_max,
            fee_pct=fee,
            initial_capital=1000.0,
            failed_retest_filter=True,
            symbol_params=config.get("symbol_params", {}),
        )

        # Inyectar fecha de entrada en cada trade desde df_5m
        for t in result.trades:
            if 0 <= t.entry_index < len(df_5m):
                t._entry_date = df_5m.index[t.entry_index].date()

        print(f"{len(result.trades)} trades ({_time.time()-t0:.1f}s)")
        analizar_trades(result.trades, symbol)
        all_trades_group.extend(result.trades)

    if len(all_trades_group) > 1:
        print(f"\n{'─'*56}")
        analizar_trades(all_trades_group, f"AGREGADO — {group_label}")

print("\n\nAnálisis completado.\n")
