"""
indicators/technical.py
-----------------------
Calcula indicadores técnicos adicionales usados como features
para el modelo XGBoost: RSI, ATR, Bollinger Bands, volumen relativo.
"""

import pandas as pd
import numpy as np
import ta
from loguru import logger


def calcular_rsi(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """RSI con librería ta."""
    return ta.momentum.RSIIndicator(df["close"], window=length).rsi()


def calcular_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Average True Range — medida de volatilidad."""
    return ta.volatility.AverageTrueRange(
        df["high"], df["low"], df["close"], window=length
    ).average_true_range()


def calcular_bollinger(df: pd.DataFrame, length: int = 20, std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bandas de Bollinger — devuelve (upper, mid, lower)."""
    bb = ta.volatility.BollingerBands(df["close"], window=length, window_dev=std)
    return bb.bollinger_hband(), bb.bollinger_mavg(), bb.bollinger_lband()


def calcular_volumen_relativo(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Ratio volumen actual / media de volumen de las últimas N velas."""
    return df["volume"] / df["volume"].rolling(window).mean()


def calcular_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula todas las features técnicas sobre un DataFrame OHLCV.
    Este DataFrame se usará para entrenar y hacer inferencia con XGBoost.

    Features generadas:
    - rsi_14: RSI de 14 períodos
    - atr_pct: ATR de 14 períodos (normalizado por precio)
    - volume_ratio: Volumen relativo a la media de 20 períodos
    - bb_position: Posición relativa dentro de las Bandas de Bollinger (0=lower, 1=upper)
    - body_pct: Tamaño relativo del cuerpo de la vela (%)
    - upper_wick_pct: Mecha superior relativa
    - lower_wick_pct: Mecha inferior relativa
    - price_change_pct: Cambio de precio respecto a la vela anterior (%)
    - hour_of_day: Hora del día (0-23) — importante para cripto
    - day_of_week: Día de la semana (0=lunes)
    """
    df = df.copy()

    # RSI
    df["rsi_14"] = calcular_rsi(df)

    # ATR normalizado por precio
    atr = calcular_atr(df)
    df["atr_pct"] = atr / df["close"] * 100

    # Volumen relativo
    df["volume_ratio"] = calcular_volumen_relativo(df)

    # Bollinger Bands position
    bb_upper, bb_mid, bb_lower = calcular_bollinger(df)
    bb_range = bb_upper - bb_lower
    df["bb_position"] = (df["close"] - bb_lower) / bb_range.replace(0, np.nan)

    # Anatomía de la vela
    body = abs(df["close"] - df["open"])
    candle_range = df["high"] - df["low"]
    df["body_pct"] = body / candle_range.replace(0, np.nan) * 100
    df["upper_wick_pct"] = (df["high"] - df[["close", "open"]].max(axis=1)) / candle_range.replace(0, np.nan) * 100
    df["lower_wick_pct"] = (df[["close", "open"]].min(axis=1) - df["low"]) / candle_range.replace(0, np.nan) * 100
    df["is_bullish"] = (df["close"] > df["open"]).astype(int)

    # Cambio porcentual
    df["price_change_pct"] = df["close"].pct_change() * 100

    # Momento (tiempo) — crucial en cripto
    if isinstance(df.index, pd.DatetimeIndex):
        df["hour_of_day"] = df.index.hour
        df["day_of_week"] = df.index.dayofweek
    else:
        df["hour_of_day"] = 0
        df["day_of_week"] = 0

    return df


# Lista de columnas que se usan como features de entrada al modelo
FEATURE_COLUMNS = [
    "rsi_14",
    "atr_pct",
    "volume_ratio",
    "bb_position",
    "body_pct",
    "upper_wick_pct",
    "lower_wick_pct",
    "is_bullish",
    "price_change_pct",
    "hour_of_day",
    "day_of_week",
]
