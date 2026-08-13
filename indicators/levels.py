"""
indicators/levels.py
--------------------
Implementa la estrategia de breakout multi-temporal:

ESTRATEGIA:
  1. Identificar suelos/techos del activo en mensual (niveles primarios)
  2. Esperar que el precio se aproxime a esos límites
  3. Cuando rompe el nivel mensual, verificar confirmación en:
       - Semanal: ¿hay una rotura histórica alineada en esa zona?
       - Diario:  ¿la rotura diaria reciente apoya la dirección?
       - Horario: ¿el precio ya superó el nivel horario más cercano?
  4. Confirmar que los triggers de otros traders están disparándose (volumen spike)
  5. Si todo alineado → señal de entrada
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from loguru import logger


# ─────────────────────────────────────────────────
# Estructuras de datos
# ─────────────────────────────────────────────────

@dataclass
class MonthlyLevels:
    """Suelos y techos mensuales — niveles primarios de la estrategia."""
    resistance: float   # Techo mensual (máx de los últimos N meses)
    support: float      # Suelo mensual (mín de los últimos N meses)
    current_price: float
    lookback_months: int = 6

    @property
    def near_resistance(self) -> bool:
        """Precio dentro del 0.30% del techo mensual."""
        return abs(self.current_price - self.resistance) / self.resistance < 0.003

    @property
    def near_support(self) -> bool:
        """Precio dentro del 0.30% del suelo mensual."""
        return abs(self.current_price - self.support) / self.support < 0.003

    @property
    def broke_resistance(self) -> bool:
        """El precio ya cerró por encima del techo mensual."""
        return self.current_price > self.resistance

    @property
    def broke_support(self) -> bool:
        """El precio ya cerró por debajo del suelo mensual."""
        return self.current_price < self.support


@dataclass
class BreakoutZone:
    """Zona de rotura histórica en un marco temporal."""
    timeframe: str
    level_price: float
    direction: str      # "bullish" o "bearish"
    confirmed: bool     # Si la rotura en este TF alinea con la dirección objetivo


@dataclass
class StrategySignal:
    """Señal completa generada por la estrategia multi-TF."""
    symbol: str
    direction: str                      # "long" o "short"
    entry_price: float
    monthly_level: float                # Nivel mensual que se rompió
    volume_ratio: float                 # Ratio de volumen (triggers disparándose)
    confirmations: list[str] = field(default_factory=list)   # TFs que confirmaron
    confirmation_score: int = 0         # 0-3: cuántos TFs confirmaron (3=máximo)

    @property
    def es_valida(self) -> bool:
        """La señal es válida si al menos 2 de 3 marcos temporales confirman."""
        return self.confirmation_score >= 2


# ─────────────────────────────────────────────────
# Cálculo de niveles mensuales
# ─────────────────────────────────────────────────

def calcular_niveles_mensuales(
    df_diario: pd.DataFrame,
    lookback_months: int = 6,
) -> MonthlyLevels:
    """
    Calcula el suelo y techo mensual (niveles primarios de la estrategia).

    Usa datos diarios con ventana rodante de N meses para encontrar
    el máximo y mínimo que han contenido el precio.

    Args:
        df_diario:       DataFrame OHLCV diario
        lookback_months: Meses hacia atrás (default 6 meses)
    """
    lookback_days = lookback_months * 30
    window = df_diario.tail(lookback_days)

    # El nivel histórico se calcula EXCLUYENDO la vela actual.
    # Así broke_resistance = True cuando el precio actual cierra
    # por ENCIMA del techo histórico de los últimos N meses.
    hist_window = window.iloc[:-1] if len(window) > 1 else window

    resistance = float(hist_window["high"].max())
    support = float(hist_window["low"].min())
    current_price = float(df_diario["close"].iloc[-1])

    logger.debug(
        f"Niveles mensuales ({lookback_months}m) | "
        f"Techo: {resistance:.4f} | Suelo: {support:.4f} | "
        f"Precio: {current_price:.4f}"
    )

    return MonthlyLevels(
        resistance=resistance,
        support=support,
        current_price=current_price,
        lookback_months=lookback_months,
    )


# ─────────────────────────────────────────────────
# Confirmación multi-temporal
# ─────────────────────────────────────────────────

def _ultima_rotura_historica(
    df: pd.DataFrame,
    direction: str,
    lookback: int,
) -> BreakoutZone | None:
    """
    Detecta si hubo una rotura histórica reciente en df que apoye la dirección.

    Lógica: busca el nivel más reciente que fue superado claramente
    (cierre > máximo anterior para long, cierre < mínimo anterior para short).
    Esto representa dónde otros traders tienen stops y triggers acumulados.

    Args:
        df:        DataFrame OHLCV del marco temporal a analizar
        direction: "long" o "short"
        lookback:  Número de velas hacia atrás para buscar

    Returns:
        BreakoutZone si hay una rotura alineada, None si no
    """
    window = df.tail(lookback + 1)
    if len(window) < 5:
        return None

    tf_name = "unknown"

    if direction == "long":
        # Buscamos una rotura alcista reciente: el precio superó un máximo previo
        rolling_high = window["high"].rolling(5).max().shift(1)
        broke_mask = window["close"] > rolling_high
        if broke_mask.any():
            last_break_idx = broke_mask[broke_mask].index[-1]
            level = float(rolling_high.loc[last_break_idx])
            return BreakoutZone(
                timeframe=tf_name,
                level_price=level,
                direction="bullish",
                confirmed=True,
            )
    else:
        # Buscamos una rotura bajista reciente: el precio cayó bajo un mínimo previo
        rolling_low = window["low"].rolling(5).min().shift(1)
        broke_mask = window["close"] < rolling_low
        if broke_mask.any():
            last_break_idx = broke_mask[broke_mask].index[-1]
            level = float(rolling_low.loc[last_break_idx])
            return BreakoutZone(
                timeframe=tf_name,
                level_price=level,
                direction="bearish",
                confirmed=True,
            )

    return None


def confirmar_multi_temporal(
    df_semanal: pd.DataFrame,
    df_diario: pd.DataFrame,
    df_horario: pd.DataFrame,
    direction: str,
    config_levels: dict,
) -> tuple[int, list[str]]:
    """
    Comprueba la confirmación en semanal, diario y horario.

    Estrategia: cuando el nivel mensual se rompe, buscamos roturas
    históricas alineadas en TFs inferiores → indica acumulación de
    triggers de otros traders en esa zona.

    Returns:
        (score: int 0-3, confirmaciones: lista de TFs que confirman)
    """
    score = 0
    confirmations = []

    # --- Confirmación semanal ---
    zona_semanal = _ultima_rotura_historica(
        df_semanal, direction, lookback=config_levels.get("weekly_lookback", 8)
    )
    if zona_semanal and zona_semanal.confirmed:
        score += 1
        confirmations.append("semanal")
        logger.debug(f"Confirmación SEMANAL | Zona rotura: {zona_semanal.level_price:.4f}")

    # --- Confirmación diaria ---
    zona_diaria = _ultima_rotura_historica(
        df_diario, direction, lookback=config_levels.get("daily_lookback", 20)
    )
    if zona_diaria and zona_diaria.confirmed:
        score += 1
        confirmations.append("diario")
        logger.debug(f"Confirmación DIARIA | Zona rotura: {zona_diaria.level_price:.4f}")

    # --- Confirmación horaria ---
    zona_horaria = _ultima_rotura_historica(
        df_horario, direction, lookback=config_levels.get("hourly_lookback", 48)
    )
    if zona_horaria and zona_horaria.confirmed:
        score += 1
        confirmations.append("horario")
        logger.debug(f"Confirmación HORARIA | Zona rotura: {zona_horaria.level_price:.4f}")

    return score, confirmations


# ─────────────────────────────────────────────────
# Detección de triggers de otros traders (volumen spike)
# ─────────────────────────────────────────────────

def detectar_triggers_disparandose(
    df_entry: pd.DataFrame,
    volume_ratio_min: float = 2.0,
    volume_ratio_max: float = 3.0,
) -> tuple[bool, float]:
    """
    Detecta si el volumen actual indica que los stops/triggers de
    otros traders se están disparando en cascada.

    Zona válida: entre volume_ratio_min y volume_ratio_max.
    - Por debajo del mínimo: spike débil, alta tasa de fakeout.
    - Por encima del máximo: trampa de liquidez de ballenas (WR cae a 25%).

    Args:
        df_entry:          DataFrame del marco de entrada (1m o 5m)
        volume_ratio_min:  Ratio mínimo para considerar spike real (default 2.0)
        volume_ratio_max:  Ratio máximo — spikes extremos se descartan (default 3.0)

    Returns:
        (hay_spike: bool, volume_ratio: float)
    """
    if len(df_entry) < 21:
        return False, 0.0

    avg_volume = float(df_entry["volume"].iloc[-21:-1].mean())
    current_volume = float(df_entry["volume"].iloc[-1])
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0

    hay_spike = volume_ratio_min <= volume_ratio <= volume_ratio_max

    if hay_spike:
        logger.info(f"Triggers disparándose | Vol ratio: {volume_ratio:.2f}× (zona: {volume_ratio_min}-{volume_ratio_max}×)")
    elif volume_ratio > volume_ratio_max:
        logger.debug(f"Spike extremo ignorado ({volume_ratio:.2f}×) — posible trampa de liquidez")

    return hay_spike, volume_ratio


def detectar_failed_retest(
    df: pd.DataFrame,
    level: float,
    direction: str,
    lookback: int = 60,
    min_bounce_pct: float = 0.3,
) -> bool:
    """
    Detecta el patrón de failed retest tras rotura de nivel mensual.

    Evita entrar en fakeouts esperando a que el precio intente recuperar
    el nivel roto y fracase — solo confirma la entrada tras ese fracaso.

    Patrón SHORT (simétrico para LONG):
      1. Rotura:  alguna vela reciente cerró por DEBAJO del nivel S
      2. Rebote:  tras la rotura el precio subió ≥ min_bounce_pct% hacia S
                  (compradores waiting que entran justo bajo el soporte roto)
      3. Fracaso: el precio NO volvió a cerrar por ENCIMA de S
                  (S pasa a actuar como resistencia — fakeout confirmado)
      4. Actual:  el precio sigue por debajo de S → bajada confirmada

    Args:
        df:             DataFrame 1m de la ventana de entrada (500 velas)
        level:          Precio del nivel mensual roto
        direction:      "short" o "long"
        lookback:       Velas hacia atrás para buscar el patrón (default 60 = 1h en 1m)
        min_bounce_pct: Rebote mínimo en % para confirmar intento de recuperación

    Returns:
        True si el patrón completo está confirmado y se puede entrar.
    """
    if len(df) < max(10, lookback // 4):
        return False

    window = df.tail(lookback)

    if direction == "short":
        # 1. Buscar primera barra donde el precio cerró bajo el nivel
        below_mask = window["close"] < level
        if not below_mask.any():
            return False
        break_pos = int(below_mask.values.argmax())
        break_close = float(window.iloc[break_pos]["close"])

        # 2. Buscar rebote después de la rotura (compradores waiting)
        after = window.iloc[break_pos + 1:]
        if len(after) < 2:
            return False  # Demasiado reciente — esperar más barras

        bounce_threshold = break_close * (1 + min_bounce_pct / 100)
        if not (after["high"] >= bounce_threshold).any():
            return False  # Rebote aún no ocurrió — esperar

        # 3. Verificar que el rebote NO recuperó el nivel (failed retest)
        if (after["close"] >= level).any():
            return False  # Precio volvió sobre el nivel → no es rotura real

        # 4. Precio actual sigue bajo el nivel → bajada confirmada
        return float(window["close"].iloc[-1]) < level

    else:  # long
        above_mask = window["close"] > level
        if not above_mask.any():
            return False
        break_pos = int(above_mask.values.argmax())
        break_close = float(window.iloc[break_pos]["close"])

        after = window.iloc[break_pos + 1:]
        if len(after) < 2:
            return False

        pullback_threshold = break_close * (1 - min_bounce_pct / 100)
        if not (after["low"] <= pullback_threshold).any():
            return False

        if (after["close"] <= level).any():
            return False

        return float(window["close"].iloc[-1]) > level


def adaptar_failed_retest(
    df: pd.DataFrame,
    level: float,
    direction: str,
    lookback: int = 500,
    min_samples: int = 4,
) -> tuple[bool, float]:
    """
    Auto-calibra el filtro failed retest midiendo el comportamiento reciente
    del símbolo en este nivel. Se llama cuando failed_retest_filter = "auto".

    Lógica:
      1. Detectar las últimas N roturas del nivel en la ventana de datos
      2. Para cada rotura, medir el tamaño del rebote que vino después
      3. Si ≥50% de las roturas tuvieron un rebote ≥0.15%:
             → símbolo está en modo fakeout-prone
             → activar filtro con umbral = 60% del rebote medio
         Si <50%:
             → símbolo rompe limpio (clean breaker)
             → desactivar filtro

    Se actualiza en cada ciclo del paper trader / cada señal del backtest,
    por lo que se adapta automáticamente cuando el mercado cambia de régimen.

    Returns:
        (use_filter, min_bounce_pct):
            use_filter      — True  = símbolo es fakeout-prone → usar filtro
                              False = símbolo rompe limpio     → saltar filtro
            min_bounce_pct  — Umbral calibrado al tamaño real del rebote reciente
    """
    if len(df) < 50:
        return True, 0.3  # fallback seguro cuando hay pocos datos

    window = df.tail(lookback)

    # Detectar transiciones: momento en que el precio cruza el nivel
    if direction == "short":
        crossed = (window["close"] < level).astype(int)
    else:
        crossed = (window["close"] > level).astype(int)

    transitions = crossed.diff().fillna(0)
    break_positions = [i for i, v in enumerate(transitions.values) if v == 1]

    if len(break_positions) < min_samples:
        # Pocas roturas recientes → no hay certeza → activar filtro (más seguro)
        return True, 0.3

    # Medir el rebote post-rotura (máx bounce en los 40 bars siguientes)
    bounce_sizes: list[float] = []
    for pos in break_positions[-(min_samples * 2):]:
        if pos >= len(window) - 5:
            continue
        break_close = float(window.iloc[pos]["close"])
        after = window.iloc[pos + 1: pos + 40]
        if len(after) < 3:
            continue

        if direction == "short":
            # Bounce = precio sube tras romper el soporte
            raw_bounce = (float(after["high"].max()) - break_close) / break_close * 100
        else:
            # Pullback = precio baja tras romper la resistencia
            raw_bounce = (break_close - float(after["low"].min())) / break_close * 100

        bounce_sizes.append(max(0.0, raw_bounce))

    if len(bounce_sizes) < min_samples:
        return True, 0.3

    avg_bounce     = float(np.mean(bounce_sizes))
    # Fracción de roturas con rebote significativo (umbral base: 0.15%)
    bounce_rate    = sum(1 for b in bounce_sizes if b >= 0.15) / len(bounce_sizes)
    # Umbral calibrado: 50% del rebote medio, limitado entre 0.10% y 0.30%
    # Cap en 0.30% para no sobre-filtrar en símbolos con bounces grandes
    calibrated_pct = round(max(0.10, min(0.30, avg_bounce * 0.50)), 2)

    use_filter = bounce_rate >= 0.5
    return use_filter, calibrated_pct


# ─────────────────────────────────────────────────
# Función principal de la estrategia
# ─────────────────────────────────────────────────

def evaluar_estrategia(
    symbol: str,
    df_entry: pd.DataFrame,       # 1m (entrada)
    df_horario: pd.DataFrame,     # 1h
    df_diario: pd.DataFrame,      # 1d
    df_semanal: pd.DataFrame,     # 1w
    config: dict,
) -> StrategySignal | None:
    """
    Evaluación completa de la estrategia en un ciclo.

    FLUJO:
      1. Calcular suelo/techo mensual (nivel primario)
      2. ¿El precio se acercó y ya rompió ese nivel?
      3. Confirmación multi-TF (semanal + diario + horario)
      4. ¿Los triggers de otros traders se están disparando? (volumen spike)
      5. Si todo OK → señal de entrada

    Returns:
        StrategySignal si hay señal válida, None si no.
    """
    levels_config = config.get("levels", {})
    risk_config = config.get("risk", {})

    # Mezclar parámetros globales con overrides específicos del símbolo
    sym_overrides = config.get("symbol_params", {}).get(symbol, {})
    if sym_overrides:
        levels_config = {**levels_config, **sym_overrides}
        logger.debug(f"{symbol} | Usando params personalizados: {sym_overrides}")

    # 1. Niveles mensuales primarios
    monthly = calcular_niveles_mensuales(
        df_diario,
        lookback_months=levels_config.get("monthly_lookback", 6),
    )

    # 2. ¿El precio rompió el nivel mensual?
    if not (monthly.broke_resistance or monthly.broke_support):
        logger.debug(
            f"{symbol} | Sin rotura mensual | "
            f"Precio {monthly.current_price:.4f} entre "
            f"[{monthly.support:.4f}, {monthly.resistance:.4f}]"
        )
        return None

    direction = "long" if monthly.broke_resistance else "short"
    monthly_level = monthly.resistance if direction == "long" else monthly.support

    logger.info(
        f"{symbol} | Rotura mensual detectada | "
        f"Dirección: {direction.upper()} | Nivel: {monthly_level:.4f}"
    )

    # 3. Confirmación multi-temporal
    score, confirmations = confirmar_multi_temporal(
        df_semanal=df_semanal,
        df_diario=df_diario,
        df_horario=df_horario,
        direction=direction,
        config_levels=levels_config,
    )

    if score < 2:
        logger.info(
            f"{symbol} | Confirmación insuficiente: {score}/3 TFs | "
            f"Confirmados: {confirmations}"
        )
        return None

    # 3.5. Failed retest — el nivel roto debe actuar como resistencia/soporte
    # failed_retest_filter puede ser:
    #   "auto"  → auto-detectar el régimen basándose en el comportamiento reciente
    #   True    → siempre activar el filtro (forkout-prone explícito)
    #   False   → nunca activar el filtro (clean breaker explícito)
    fr_setting = levels_config.get("failed_retest_filter", "auto")

    if fr_setting is False:
        use_fr = False
        fr_bounce_pct = levels_config.get("failed_retest_min_bounce_pct", 0.3)
    elif fr_setting is True:
        use_fr = True
        fr_bounce_pct = levels_config.get("failed_retest_min_bounce_pct", 0.3)
    else:  # "auto" o cualquier otro valor → auto-detectar
        auto_lookback = levels_config.get("failed_retest_auto_lookback", 500)
        use_fr, fr_bounce_pct = adaptar_failed_retest(
            df=df_entry,
            level=monthly_level,
            direction=direction,
            lookback=auto_lookback,
        )

    if use_fr:
        retest_ok = detectar_failed_retest(
            df=df_entry,
            level=monthly_level,
            direction=direction,
            lookback=levels_config.get("failed_retest_lookback", 60),
            min_bounce_pct=fr_bounce_pct,
        )
        if not retest_ok:
            logger.debug(
                f"{symbol} | Esperando failed retest del nivel {monthly_level:.4f} "
                f"(umbral={fr_bounce_pct}%) — precio debe rebotar y fallar"
            )
            return None

    # 4. ADX estándar — para otros mercados (acciones, forex). En crypto usar crypto_trend_filter.
    adx_min = levels_config.get("adx_min", 0)
    if adx_min > 0 and df_diario is not None and len(df_diario) >= 30:
        from indicators.technical import calcular_adx_series
        adx_val = float(calcular_adx_series(df_diario).iloc[-1])
        if adx_val < adx_min:
            logger.debug(f"{symbol} | ADX {adx_val:.1f} < {adx_min} — mercado lateral")
            return None

    # 4c. Filtro de tendencia cripto: ADX subiendo O ADX establecido
    if levels_config.get("crypto_trend_filter", False) and df_diario is not None and len(df_diario) >= 35:
        from indicators.technical import calcular_crypto_trend_series
        sw  = levels_config.get("crypto_trend_slope_window", 7)
        ms  = levels_config.get("crypto_trend_min_slope", 1.10)
        ma  = levels_config.get("crypto_trend_min_absolute", 25.0)
        ok_series = calcular_crypto_trend_series(df_diario, slope_window=sw, min_slope=ms, min_absolute=ma)
        if not bool(ok_series.iloc[-1]):
            logger.debug(f"{symbol} | CryptoTrend — ADX no sube ni está establecido")
            return None

    # 4b. Volumen diario — evitar días dormidos (vol < ratio × media 20d)
    daily_vol_min = levels_config.get("daily_vol_min_ratio", 0.0)
    if daily_vol_min > 0 and df_diario is not None and len(df_diario) >= 21:
        vol_mean  = float(df_diario["volume"].iloc[-21:-1].mean())
        vol_today = float(df_diario["volume"].iloc[-1])
        if vol_mean > 0 and vol_today / vol_mean < daily_vol_min:
            logger.debug(f"{symbol} | Vol diario {vol_today/vol_mean:.2f}× < {daily_vol_min}× — mercado dormido")
            return None

    # 5. Triggers de otros traders disparándose (volumen spike)
    hay_spike, volume_ratio = detectar_triggers_disparandose(
        df_entry,
        volume_ratio_min=levels_config.get("volume_trigger_ratio", 2.0),
        volume_ratio_max=levels_config.get("volume_trigger_ratio_max", 3.0),
    )

    if not hay_spike:
        logger.info(
            f"{symbol} | Sin spike de volumen (ratio: {volume_ratio:.2f}×) — "
            f"triggers aún no disparándose, esperar"
        )
        return None

    # 5. Señal válida
    entry_price = float(df_entry["close"].iloc[-1])

    signal = StrategySignal(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        monthly_level=monthly_level,
        volume_ratio=round(volume_ratio, 2),
        confirmations=confirmations,
        confirmation_score=score,
    )

    logger.success(
        f"SEÑAL VÁLIDA | {symbol} {direction.upper()} @ {entry_price:.4f} | "
        f"Nivel mensual: {monthly_level:.4f} | "
        f"Confirmaciones: {confirmations} ({score}/3) | "
        f"Vol spike: {volume_ratio:.2f}×"
    )

    return signal


# ─────────────────────────────────────────────────
# Estrategia 2: Bounce (reversión a la media)
# ─────────────────────────────────────────────────

@dataclass
class BounceSignal:
    """
    Señal de la estrategia de rebote en nivel mensual.

    Lógica:
      - El precio toca la resistencia/soporte mensual y la rechaza.
      - La vela tiene mecha que penetra el nivel pero cierra en el lado correcto.
      - Objetivo: punto medio entre resistencia y soporte (fair value del rango).
    """
    symbol: str
    direction: str          # "long" (rebote en soporte) o "short" (rebote en resistencia)
    entry_price: float
    take_profit: float      # Punto medio del rango mensual
    monthly_level: float    # Nivel que rechazó el precio
    level_type: str         # "resistance" o "support"
    range_pct: float        # Amplitud del rango resistencia-soporte en %
    wick_pct: float         # Profundidad de la mecha que penetró el nivel (%)

    @property
    def es_valida(self) -> bool:
        """El TP debe estar al menos 0.5% lejos del entry para ser interesante."""
        return abs(self.take_profit - self.entry_price) / self.entry_price >= 0.005


def evaluar_bounce(
    symbol: str,
    df_entry: pd.DataFrame,
    df_diario: pd.DataFrame,
    config: dict,
) -> BounceSignal | None:
    """
    Detecta rebotes en niveles mensuales.

    Un rebote válido ocurre cuando:
      1. La mecha de la vela actual penetra el nivel mensual (high > resistance
         o low < support).
      2. El cierre de la vela está en el lado opuesto del nivel (rechazo real).
      3. La mecha de penetración es significativa (>= wick_min_pct del precio).
      4. El rango resistencia-soporte es suficientemente amplio para que el
         punto medio ofrezca un R:R razonable.

    Returns:
        BounceSignal si hay rebote válido, None si no.
    """
    levels_cfg = config.get("levels", {})
    monthly_lookback = levels_cfg.get("monthly_lookback", 6)
    wick_min_pct = levels_cfg.get("bounce_wick_min_pct", 0.10)   # Mecha mínima: 0.10%
    min_range_pct = levels_cfg.get("bounce_min_range_pct", 2.0)  # Rango mínimo útil: 2%

    try:
        monthly = calcular_niveles_mensuales(df_diario, lookback_months=monthly_lookback)
    except Exception:
        return None

    candle = df_entry.iloc[-1]
    high   = float(candle["high"])
    low    = float(candle["low"])
    close  = float(candle["close"])
    open_  = float(candle["open"])

    resistance = monthly.resistance
    support    = monthly.support
    midpoint   = (resistance + support) / 2

    range_pct = (resistance - support) / support * 100
    if range_pct < min_range_pct:
        logger.debug(f"{symbol} | Rango mensual muy estrecho ({range_pct:.2f}%) — bounce descartado")
        return None

    signal: BounceSignal | None = None

    # ── Rebote en resistencia (vela punta arriba, cierra abajo) → SHORT ──
    if high >= resistance and close < resistance:
        wick_pct = (high - resistance) / resistance * 100
        if wick_pct >= wick_min_pct:
            signal = BounceSignal(
                symbol=symbol,
                direction="short",
                entry_price=close,
                take_profit=midpoint,
                monthly_level=resistance,
                level_type="resistance",
                range_pct=round(range_pct, 2),
                wick_pct=round(wick_pct, 3),
            )
            logger.info(
                f"BOUNCE SHORT {symbol} | Entry: {close:.4f} | "
                f"TP (midpoint): {midpoint:.4f} | Mecha: {wick_pct:.2f}% sobre resistencia {resistance:.4f}"
            )

    # ── Rebote en soporte (vela punta abajo, cierra arriba) → LONG ──
    elif low <= support and close > support:
        wick_pct = (support - low) / support * 100
        if wick_pct >= wick_min_pct:
            signal = BounceSignal(
                symbol=symbol,
                direction="long",
                entry_price=close,
                take_profit=midpoint,
                monthly_level=support,
                level_type="support",
                range_pct=round(range_pct, 2),
                wick_pct=round(wick_pct, 3),
            )
            logger.info(
                f"BOUNCE LONG {symbol} | Entry: {close:.4f} | "
                f"TP (midpoint): {midpoint:.4f} | Mecha: {wick_pct:.2f}% bajo soporte {support:.4f}"
            )

    if signal is not None and not signal.es_valida:
        logger.debug(f"{symbol} | Bounce descartado — TP demasiado cercano al entry")
        return None

    return signal


# ─────────────────────────────────────────────────
# Estrategia Bounce para el paper trader en tiempo real
# ─────────────────────────────────────────────────

def evaluar_bounce_signal(
    symbol: str,
    df_entry: "pd.DataFrame",
    df_diario: "pd.DataFrame",
    config: dict,
) -> StrategySignal | None:
    """
    Detecta rebotes en tiempo real y devuelve un StrategySignal compatible
    con el paper trader. Delega en evaluar_bounce() y convierte el resultado.

    El TP especial del bounce (midpoint del rango) se registra en
    signal.confirmations como 'bounce_tp:<precio>' para que el paper trader
    pueda usarlo en lugar del TP fijo del breakout.
    """
    bounce = evaluar_bounce(symbol, df_entry, df_diario, config)
    if bounce is None:
        return None

    return StrategySignal(
        symbol=symbol,
        direction=bounce.direction,
        entry_price=bounce.entry_price,
        monthly_level=bounce.monthly_level,
        volume_ratio=0.0,    # el bounce no requiere spike de volumen
        confirmations=[f"bounce_tp:{bounce.take_profit:.6f}"],
        confirmation_score=2,    # satisface el requisito mínimo de es_valida
    )


# ─────────────────────────────────────────────────
# Compatibilidad con código legado (backtesting)
# ─────────────────────────────────────────────────

def calcular_niveles_clave(
    df_diario: pd.DataFrame,
    df_semanal: pd.DataFrame | None = None,
) -> "KeyLevels":
    """Wrapper de compatibilidad para el motor de backtesting."""
    monthly = calcular_niveles_mensuales(df_diario, lookback_months=3)
    current = float(df_diario["close"].iloc[-1])

    if df_semanal is not None and len(df_semanal) >= 4:
        weekly_high = float(df_semanal["high"].rolling(4).max().iloc[-1])
        weekly_low = float(df_semanal["low"].rolling(4).min().iloc[-1])
    else:
        weekly_high = float(df_diario["high"].rolling(7).max().iloc[-1])
        weekly_low = float(df_diario["low"].rolling(7).min().iloc[-1])

    return KeyLevels(
        monthly_resistance=monthly.resistance,
        monthly_support=monthly.support,
        weekly_high=weekly_high,
        weekly_low=weekly_low,
        daily_high=float(df_diario["high"].iloc[-2]),
        daily_low=float(df_diario["low"].iloc[-2]),
        current_price=current,
    )


@dataclass
class KeyLevels:
    """Estructura de niveles para el motor de backtesting."""
    monthly_resistance: float
    monthly_support: float
    weekly_high: float
    weekly_low: float
    daily_high: float
    daily_low: float
    current_price: float


# ─────────────────────────────────────────────────
# Estrategia 3: Retest post-breakout (paper trader)
# ─────────────────────────────────────────────────

def evaluar_retest_signal(
    symbol: str,
    df_entry: "pd.DataFrame",
    df_diario: "pd.DataFrame",
    config: dict,
    retest_lookback: int = 200,
    retest_min_move_pct: float = 0.5,
    retest_tolerance_pct: float = 0.35,
    retest_pullback_vol_max: float = 1.5,
) -> StrategySignal | None:
    """
    Detecta una oportunidad de retest post-breakout en tiempo real.

    Usa el mismo StrategySignal que la estrategia breakout para
    ser compatible con el resto del paper trader.
    """
    import pandas as pd

    # Leer parámetros del config por símbolo si existen
    sp = config.get("symbol_params", {}).get(symbol, {})
    retest_min_move_pct     = sp.get("retest_min_move_pct",     retest_min_move_pct)
    retest_tolerance_pct    = sp.get("retest_tolerance_pct",    retest_tolerance_pct)
    retest_pullback_vol_max = sp.get("retest_pullback_vol_max", retest_pullback_vol_max)

    levels_cfg = config.get("levels", {})
    monthly_lookback = levels_cfg.get("monthly_lookback", 6)

    # ADX estándar (otros mercados) + filtro cripto de tendencia
    adx_min = levels_cfg.get("adx_min", 0)
    if adx_min > 0 and df_diario is not None and len(df_diario) >= 30:
        from indicators.technical import calcular_adx_series
        adx_val = float(calcular_adx_series(df_diario).iloc[-1])
        if adx_val < adx_min:
            logger.debug(f"{symbol} | [Retest] ADX {adx_val:.1f} < {adx_min} — mercado lateral")
            return None

    if levels_cfg.get("crypto_trend_filter", False) and df_diario is not None and len(df_diario) >= 35:
        from indicators.technical import calcular_crypto_trend_series
        sw = levels_cfg.get("crypto_trend_slope_window", 7)
        ms = levels_cfg.get("crypto_trend_min_slope", 1.10)
        ma = levels_cfg.get("crypto_trend_min_absolute", 25.0)
        ok = calcular_crypto_trend_series(df_diario, slope_window=sw, min_slope=ms, min_absolute=ma)
        if not bool(ok.iloc[-1]):
            logger.debug(f"{symbol} | [Retest] CryptoTrend — ADX no sube ni establecido")
            return None

    daily_vol_min = levels_cfg.get("daily_vol_min_ratio", 0.0)
    if daily_vol_min > 0 and df_diario is not None and len(df_diario) >= 21:
        vol_mean_d = float(df_diario["volume"].iloc[-21:-1].mean())
        vol_today  = float(df_diario["volume"].iloc[-1])
        if vol_mean_d > 0 and vol_today / vol_mean_d < daily_vol_min:
            logger.debug(f"{symbol} | [Retest] Vol diario bajo — mercado dormido")
            return None

    try:
        monthly = calcular_niveles_mensuales(df_diario, lookback_months=monthly_lookback)
    except Exception:
        return None

    current_price = float(df_entry["close"].iloc[-1])
    vol_mean = float((df_entry["volume"] * df_entry["close"]).iloc[-50:].mean()) or 1.0
    pullback_vol = float(df_entry["volume"].iloc[-1]) * float(df_entry["close"].iloc[-1])

    # Volumen del pullback debe ser bajo (profit-taking silencioso)
    if pullback_vol > vol_mean * retest_pullback_vol_max:
        return None

    recent_closes = df_entry["close"].iloc[-retest_lookback:].astype(float)

    for direction, level in [("long", monthly.resistance), ("short", monthly.support)]:
        tol = level * retest_tolerance_pct / 100

        # El precio está cerca del nivel
        if abs(current_price - level) > tol:
            continue

        # La vela cierra en el lado correcto
        if direction == "long" and current_price < level - tol * 0.5:
            continue
        if direction == "short" and current_price > level + tol * 0.5:
            continue

        # En el pasado reciente, el precio se alejó suficiente del nivel
        if direction == "long":
            moved_away = (recent_closes - level).max() >= level * retest_min_move_pct / 100
        else:
            moved_away = (level - recent_closes).max() >= level * retest_min_move_pct / 100

        if not moved_away:
            continue

        logger.info(
            f"[Retest] Señal {direction.upper()} {symbol} @ {current_price:.4f} | "
            f"Nivel: {level:.4f} | Vol pullback: {pullback_vol/vol_mean:.2f}×"
        )

        return StrategySignal(
            symbol=symbol,
            direction=direction,
            entry_price=current_price,
            monthly_level=level,
            volume_ratio=round(pullback_vol / vol_mean, 2),
            confirmations=["retest"],
            confirmation_score=2,   # satisface el requisito mínimo de es_valida
        )

    return None
