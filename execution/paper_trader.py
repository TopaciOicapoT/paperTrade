"""
execution/paper_trader.py
-------------------------
Paper trading en tiempo real usando el Testnet de Binance.

CICLO (cada 10 segundos):
  1. Descargar velas multitemporal (1m, 1h, 1d, 1w)
  2. Calcular suelos/techos mensuales
  3. Detectar si el precio rompió el nivel mensual
  4. Confirmar en semanal + diario + horario (mínimo 2/3)
  5. Verificar spike de volumen (triggers de otros traders)
  6. Preguntar al modelo IA si autoriza
  7. Calcular SL (detrás del nivel) y TP (1-1.5%)
  8. Registrar la orden en paper
  9. Monitorear hasta SL o TP
"""

import json
import time
import yaml
import pandas as pd
from pathlib import Path
from loguru import logger

from data.fetcher import get_exchange, get_data_exchange, fetch_multi_timeframe
from data.news_fetcher import get_news_risk
from indicators.levels import evaluar_estrategia, evaluar_retest_signal, evaluar_bounce_signal
from models.predictor import Predictor
from risk.manager import RiskManager

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
STATE_FILE = Path(__file__).parent.parent / "logs" / "paper_state.json"

logger.add(LOG_DIR / "paper_trading.log", rotation="1 day", retention="30 days")


def cargar_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


class PaperTrader:
    """
    Motor de paper trading para la estrategia de breakout mensual multi-TF.
    """

    def __init__(self, config: dict):
        self.config = config
        self.exchange = get_exchange(testnet=config["exchange"]["testnet"])  # Para órdenes
        self.data_exchange = get_data_exchange()  # Para datos OHLCV (siempre real)
        self.predictor = Predictor(threshold=config["model"]["probability_threshold"])
        risk_cfg = config["risk"]
        self.risk_manager = RiskManager(
            max_risk_pct=risk_cfg["max_risk_per_trade_pct"],
            tp_pct=risk_cfg["take_profit_pct"],
            tp_min_pct=risk_cfg["tp_min_pct"],
            tp_max_pct=risk_cfg["tp_max_pct"],
            sl_behind_pct=risk_cfg["sl_behind_level_pct"],
            max_positions=risk_cfg["max_open_positions"],
        )
        self.balance_usdt = config["paper_trading"]["initial_balance_usdt"]
        self.fee_pct = config["paper_trading"].get("fee_pct", 0.1) / 100  # → fracción
        self.symbols = config["symbols"]
        self.tf = config["timeframes"]

        # Futuros / apalancamiento
        futures_cfg = config.get("futures", {})
        self.futures_enabled = futures_cfg.get("enabled", False)
        self.leverage        = futures_cfg.get("leverage", 1)
        if self.futures_enabled:
            # En futuros, la fee de Binance es menor (taker 0.04%)
            self.fee_pct = 0.0004
            logger.info(f"Modo FUTUROS activado | Apalancamiento: {self.leverage}x | Fee: 0.04%")

        guardrails = config.get("guardrails", {})
        self.max_daily_loss_pct  = guardrails.get("max_daily_loss_pct", 3.0) / 100
        self.max_drawdown_pct    = guardrails.get("max_drawdown_pct", 10.0) / 100

        self.trade_log: list[dict] = []
        self.open_orders: dict[str, dict] = {}
        self._balance_peak: float = self.balance_usdt
        self._balance_day_open: float = self.balance_usdt
        self._trading_halted: bool = False
        self._stop_requested: bool = False  # señal de parada limpia desde la API

        # News circuit breaker
        news_cfg = config.get("news", {})
        self._news_enabled       = news_cfg.get("enabled", False)
        self._news_pause_hours   = news_cfg.get("pause_hours", 4)
        self._news_check_interval = news_cfg.get("check_interval_minutes", 15) * 60
        self._news_paused_until: float = 0.0
        self._news_last_check: float = 0.0

        # Cooldown post-SL: (symbol, direction, level_rounded) → timestamp_unix_expiry
        self._loss_cooldown: dict[tuple, float] = {}
        self._last_scan_event: dict[str, float] = {}

        self._cargar_estado()

        logger.info(
            f"PaperTrader iniciado | "
            f"Balance: {self.balance_usdt} USDT | "
            f"Símbolos: {self.symbols}"
        )

    def _cargar_estado(self):
        """Recupera balance, posiciones abiertas y log de trades desde disco."""
        if not STATE_FILE.exists():
            return
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.balance_usdt        = state.get("balance_usdt", self.balance_usdt)
            self.open_orders          = state.get("open_orders", {})
            self.trade_log            = state.get("trade_log", [])
            self._balance_peak        = state.get("balance_peak", self.balance_usdt)
            self._balance_day_open    = state.get("balance_day_open", self.balance_usdt)
            self._trading_halted      = state.get("trading_halted", False)
            # Resincronizar posiciones abiertas en el risk manager
            for symbol in self.open_orders:
                self.risk_manager.registrar_apertura(symbol)
            logger.info(
                f"Estado recuperado desde {STATE_FILE} | "
                f"Balance: {self.balance_usdt:.2f} USDT | "
                f"Posiciones abiertas: {list(self.open_orders.keys())}"
            )
        except Exception as e:
            logger.warning(f"No se pudo cargar el estado guardado: {e}")

    def _guardar_estado(self):
        """Persiste balance, posiciones abiertas y log de trades en disco (escritura atómica)."""
        state = {
            "balance_usdt":     self.balance_usdt,
            "open_orders":      self.open_orders,
            "trade_log":        self.trade_log,
            "balance_peak":     self._balance_peak,
            "balance_day_open": self._balance_day_open,
            "trading_halted":   self._trading_halted,
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        tmp.replace(STATE_FILE)  # operación atómica — nunca deja el archivo a medias

    def _verificar_guardrails(self) -> bool:
        """
        Comprueba circuit breaker diario y drawdown máximo.
        Devuelve True si el trading debe continuar, False si hay que parar.
        """
        if self._trading_halted:
            return False

        # ── Reset diario ──────────────────────────────────────────────────────
        # Detecta cambio de día UTC y reinicia el balance de referencia diaria
        hoy = pd.Timestamp.now(tz="UTC").date()
        if not hasattr(self, "_balance_day_date") or self._balance_day_date != hoy:
            self._balance_day_date = hoy
            self._balance_day_open = self.balance_usdt

        # ── Circuit breaker diario ────────────────────────────────────────────
        daily_loss = (self._balance_day_open - self.balance_usdt) / self._balance_day_open
        if daily_loss >= self.max_daily_loss_pct:
            logger.warning(
                f"CIRCUIT BREAKER — Pérdida diaria {daily_loss:.1%} supera el límite "
                f"{self.max_daily_loss_pct:.1%}. Trading pausado hasta mañana UTC."
            )
            self._trading_halted = True
            self._guardar_estado()
            return False

        # ── Max drawdown desde el pico ────────────────────────────────────────
        if self.balance_usdt > self._balance_peak:
            self._balance_peak = self.balance_usdt

        drawdown = (self._balance_peak - self.balance_usdt) / self._balance_peak
        if drawdown >= self.max_drawdown_pct:
            logger.error(
                f"MAX DRAWDOWN — Caída {drawdown:.1%} desde el pico "
                f"({self._balance_peak:.2f} USDT). Trading detenido."
            )
            self._trading_halted = True
            self._guardar_estado()
            return False

        return True

    def _verificar_noticias(self) -> bool:
        """
        Consulta Fear & Greed Index (Alternative.me) + RSS de medios crypto y pausa
        nuevas entradas si se detecta un evento de alto riesgo macro.
        Solo hace llamadas reales a la red cada `news_check_interval` segundos.

        Devuelve True si el trading puede continuar, False si hay que pausar.
        """
        if not self._news_enabled:
            return True

        now = time.time()

        # Verificar si la pausa por noticias sigue activa
        if now < self._news_paused_until:
            remaining = (self._news_paused_until - now) / 3600
            logger.info(f"[News] Trading pausado por evento macro — {remaining:.1f}h restantes")
            return False

        # Respetar el intervalo mínimo entre consultas a la API
        if now - self._news_last_check < self._news_check_interval:
            return True

        self._news_last_check = now

        try:
            report = get_news_risk()
        except Exception as exc:
            logger.warning(f"[News] Error en verificación de noticias: {exc} — continuando")
            return True

        if report.should_pause:
            self._news_paused_until = now + self._news_pause_hours * 3600
            logger.warning(
                f"[News] ⛔ PAUSA activada por riesgo {report.level.upper()} "
                f"— nuevas entradas suspendidas {self._news_pause_hours}h "
                f"(score={report.score}, keywords={report.triggered_keywords[:5]})"
            )
            return False

        return True

    def _obtener_datos(self, symbol: str) -> dict[str, pd.DataFrame]:
        """Descarga datos multitemporal para un símbolo desde Binance real."""
        tfs = [
            self.tf["entry"],
            self.tf["hourly"],
            self.tf["daily"],
            self.tf["weekly"],
        ]
        return fetch_multi_timeframe(
            symbol=symbol,
            timeframes=tfs,
            exchange=self.data_exchange,  # Datos reales siempre
        )

    def _verificar_ordenes_abiertas(self, symbol: str, data: dict):
        """Verifica si alguna orden abierta alcanzó su SL o TP."""
        if symbol not in self.open_orders:
            return

        order = self.open_orders[symbol]
        df_entry = data[self.tf["entry"]]
        current_high = float(df_entry["high"].iloc[-1])
        current_low = float(df_entry["low"].iloc[-1])

        hit_tp = hit_sl = False

        if order["direction"] == "long":
            hit_tp = current_high >= order["take_profit"]
            hit_sl = current_low <= order["stop_loss"]
        else:
            hit_tp = current_low <= order["take_profit"]
            hit_sl = current_high >= order["stop_loss"]

        if hit_tp or hit_sl:
            exit_type = "TP" if hit_tp else "SL"
            exit_price = order["take_profit"] if hit_tp else order["stop_loss"]

            pnl = (exit_price - order["entry_price"]) / order["entry_price"]
            if order["direction"] == "short":
                pnl = -pnl

            # Aplicar apalancamiento (en spot leverage=1, en futuros leverage=N)
            pnl *= self.leverage

            pnl_usdt = order["risk_usdt"] * (order["tp_pct"] / order["sl_pct"] if hit_tp else -1)
            pnl_usdt *= self.leverage

            # Comisión de apertura + cierre (ambos lados del trade)
            fee_usdt = order["entry_price"] * order["quantity"] * self.fee_pct * 2
            pnl_usdt -= fee_usdt

            self.balance_usdt += pnl_usdt

            log_entry = {
                **order,
                "exit_price": exit_price,
                "exit_type": exit_type,
                "pnl_pct": round(pnl * 100, 4),
                "pnl_usdt": round(pnl_usdt, 2),
                "fee_usdt": round(fee_usdt, 4),
                "balance_after": round(self.balance_usdt, 2),
            }
            self.trade_log.append(log_entry)

            logger.info(
                f"[{exit_type}] {symbol} {order['direction'].upper()} cerrado | "
                f"PnL: {pnl_usdt:+.2f} USDT ({pnl*100:+.2f}%) | "
                f"Balance: {self.balance_usdt:.2f} USDT"
            )

            del self.open_orders[symbol]
            self.risk_manager.registrar_cierre(symbol)

            # Registrar cooldown si fue un SL: no re-entrar en el mismo nivel por N horas
            if exit_type == "SL":
                cooldown_bars = self.config.get("levels", {}).get("breakout_loss_cooldown_bars", 0)
                if cooldown_bars > 0:
                    # En el live bot el TF de entrada es 1m; cooldown_bars está en 5m → ×5
                    cooldown_secs = cooldown_bars * 5 * 60
                    level_rounded = round(order.get("monthly_level", exit_price), 4)
                    key = (symbol, order["direction"], level_rounded)
                    self._loss_cooldown[key] = time.time() + cooldown_secs
                    logger.debug(f"[Cooldown] {symbol} {order['direction']} nivel {level_rounded} — bloqueado {cooldown_bars*5/60:.0f}h")

            self._guardar_estado()

    def _log_event(
        self, symbol: str, strategy: str, event_type: str,
        direction: str | None = None, price: float | None = None,
        level_name: str | None = None, volume_ratio: float | None = None,
        rejection_reason: str | None = None, details: dict | None = None,
    ):
        """Persiste un evento de señal en la DB de forma no-bloqueante."""
        import json as _json
        try:
            from api.db.database import SessionLocal
            from api.db.models import SignalEvent
            from datetime import datetime
            db = SessionLocal()
            try:
                ev = SignalEvent(
                    timestamp=datetime.utcnow(),
                    symbol=symbol, strategy=strategy, event_type=event_type,
                    direction=direction, price=price, level_name=level_name,
                    volume_ratio=volume_ratio, rejection_reason=rejection_reason,
                    details=_json.dumps(details) if details else None,
                )
                db.add(ev)
                db.commit()
            finally:
                db.close()
        except Exception as _e:
            logger.debug(f"[EventLog] No se pudo guardar evento: {_e}")

    def _procesar_simbolo(self, symbol: str):
        """Ciclo completo de análisis y decisión para un símbolo."""
        try:
            data = self._obtener_datos(symbol)

            df_entry = data[self.tf["entry"]]
            df_horario = data[self.tf["hourly"]]
            df_diario = data[self.tf["daily"]]
            df_semanal = data[self.tf["weekly"]]

            # Una muestra cada cinco minutos confirma que el símbolo está vivo sin saturar la DB.
            now = time.time()
            if now - self._last_scan_event.get(symbol, 0) >= 300:
                self._log_event(
                    symbol=symbol,
                    strategy="scan",
                    event_type="SCAN_HEARTBEAT",
                    price=float(df_entry["close"].iloc[-1]),
                    details={
                        "entry_timeframe": self.tf["entry"],
                        "last_candle": str(df_entry.index[-1]),
                        "candles": len(df_entry),
                    },
                )
                self._last_scan_event[symbol] = now

            # 1. Verificar órdenes abiertas
            self._verificar_ordenes_abiertas(symbol, data)

            sym_params = self.config.get("symbol_params", {}).get(symbol, {})
            global_strategies = self.config.get("strategies", ["breakout"])
            active_strategies  = sym_params.get("strategies", global_strategies)

            # 2. Evaluar señales PRIMERO (antes de capacity/news) para poder loggear todo
            signal = None
            strategy_used = None

            if "breakout" in active_strategies:
                s = evaluar_estrategia(
                    symbol=symbol, df_entry=df_entry, df_horario=df_horario,
                    df_diario=df_diario, df_semanal=df_semanal, config=self.config,
                )
                if s and s.es_valida:
                    signal, strategy_used = s, "breakout"

            if signal is None and "retest" in active_strategies:
                s = evaluar_retest_signal(
                    symbol=symbol, df_entry=df_entry, df_diario=df_diario, config=self.config,
                )
                if s and s.es_valida:
                    signal, strategy_used = s, "retest"

            if signal is None and "bounce" in active_strategies:
                s = evaluar_bounce_signal(
                    symbol=symbol, df_entry=df_entry, df_diario=df_diario, config=self.config,
                )
                if s and s.es_valida:
                    signal, strategy_used = s, "bounce"

            # Sin señal → salir silenciosamente (no loggear, demasiado frecuente)
            if signal is None:
                return

            current_price = float(df_entry["close"].iloc[-1])
            _log_base = dict(
                symbol=symbol, strategy=strategy_used,
                direction=signal.direction, price=current_price,
                level_name=getattr(signal, "level_name", None),
                volume_ratio=signal.volume_ratio,
            )

            # Cooldown post-SL: bloquear re-entrada en el mismo nivel
            if self._loss_cooldown:
                level_rounded = round(signal.monthly_level, 4)
                cd_key = (symbol, signal.direction, level_rounded)
                if cd_key in self._loss_cooldown:
                    if time.time() < self._loss_cooldown[cd_key]:
                        self._log_event(
                            **_log_base, event_type="REJECTED_RISK",
                            rejection_reason="Cooldown post-SL activo para este nivel",
                        )
                        return
                    else:
                        del self._loss_cooldown[cd_key]

            # ── Comprobaciones post-señal con logging ─────────────────────────

            # Capacity
            if not self.risk_manager.puede_abrir_posicion(symbol):
                self._log_event(
                    **_log_base, event_type="REJECTED_CAPACITY",
                    rejection_reason=f"Posiciones abiertas: {len(self.open_orders)}/{self.risk_manager.max_positions}",
                )
                return

            # News circuit breaker
            if not self._verificar_noticias():
                self._log_event(**_log_base, event_type="REJECTED_NEWS",
                                rejection_reason="News circuit breaker activo")
                return

            # F2b: Sesión UTC
            session_block = sym_params.get("session_block_hours")
            if session_block:
                lo_h, hi_h = session_block
                now_hour = pd.Timestamp.now(tz="UTC").hour
                if lo_h <= now_hour < hi_h:
                    self._log_event(
                        **_log_base, event_type="REJECTED_SESSION",
                        rejection_reason=f"Sesión bloqueada {lo_h}-{hi_h}h UTC (hora actual: {now_hour}h)",
                    )
                    return

            # F3: Volumen USDT normalizado
            usdt_block = sym_params.get("usdt_norm_block_range")
            if usdt_block:
                lo_v, hi_v = usdt_block
                last_vol  = float(df_entry["volume"].iloc[-1]) * float(df_entry["close"].iloc[-1])
                mean_vol  = (df_entry["volume"] * df_entry["close"]).iloc[-50:].mean()
                usdt_norm = last_vol / mean_vol if mean_vol > 0 else 1.0
                if lo_v <= usdt_norm <= hi_v:
                    self._log_event(
                        **_log_base, event_type="REJECTED_VOLUME",
                        rejection_reason=f"Vol USDT norm {usdt_norm:.2f}× en zona trampa [{lo_v}-{hi_v}×]",
                        details={"usdt_norm": round(usdt_norm, 3)},
                    )
                    return

            # F1: Momentum 5 velas
            mom_block = sym_params.get("momentum_q3_block")
            if mom_block and len(df_entry) >= 6:
                lo_m, hi_m = mom_block
                prev5      = float(df_entry["close"].iloc[-6])
                curr_close = float(df_entry["close"].iloc[-1])
                momentum_5 = (curr_close - prev5) / prev5 * 100
                if lo_m <= momentum_5 <= hi_m:
                    self._log_event(
                        **_log_base, event_type="REJECTED_MOMENTUM",
                        rejection_reason=f"Momentum {momentum_5:.2f}% en zona trampa [{lo_m}-{hi_m}%]",
                        details={"momentum_5v": round(momentum_5, 3)},
                    )
                    return

            # F4: RSI14 sobrecompra
            import numpy as _np
            rsi_block = sym_params.get("rsi_overbought_block", self.config.get("rsi_overbought_block"))
            rsi14: float | None = None
            if rsi_block is not None and len(df_entry) >= 14:
                closes14 = df_entry["close"].iloc[-14:].astype(float).values
                deltas   = _np.diff(closes14)
                avg_gain = _np.where(deltas > 0, deltas, 0.0).mean()
                avg_loss = _np.where(deltas < 0, -deltas, 0.0).mean()
                rsi14    = (100.0 - 100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss > 0 else 100.0
                if rsi14 >= rsi_block:
                    self._log_event(
                        **_log_base, event_type="REJECTED_RSI",
                        rejection_reason=f"RSI14 {rsi14:.1f} ≥ {rsi_block} (sobrecompra)",
                        details={"rsi14": round(rsi14, 1)},
                    )
                    return

            # Filtro IA
            autorizado, prob = self.predictor.autorizar_entrada(df_entry)
            if not autorizado:
                self._log_event(
                    **_log_base, event_type="REJECTED_AI",
                    rejection_reason=f"Modelo IA rechazó (prob={prob:.2f} < umbral)",
                    details={"ai_prob": round(prob, 4), "rsi14": round(rsi14, 1) if rsi14 else None},
                )
                return

            # Calcular parámetros de riesgo
            orden = self.risk_manager.calcular_orden(
                symbol=symbol, direction=signal.direction,
                entry_price=signal.entry_price, monthly_level=signal.monthly_level,
                balance_usdt=self.balance_usdt, volume_ratio=signal.volume_ratio,
            )
            if orden is None:
                self._log_event(
                    **_log_base, event_type="REJECTED_RISK",
                    rejection_reason="RiskManager rechazó el tamaño de orden (capital insuficiente o SL inválido)",
                )
                return

            # Override TP para bounce
            bounce_tp_tag = next((c for c in signal.confirmations if c.startswith("bounce_tp:")), None)
            if bounce_tp_tag:
                bounce_tp_price = float(bounce_tp_tag.split(":")[1])
                orden = orden.__class__(
                    **{**vars(orden), "take_profit": round(bounce_tp_price, 8),
                       "tp_pct": abs(bounce_tp_price - orden.entry_price) / orden.entry_price * 100}
                )

            # Verificación de liquidación en futuros
            if self.futures_enabled and self.leverage > 1:
                margin_rate = 1 / self.leverage
                if orden.direction == "long":
                    liq_price = orden.entry_price * (1 - margin_rate * 0.9)
                    safe = orden.stop_loss > liq_price
                else:
                    liq_price = orden.entry_price * (1 + margin_rate * 0.9)
                    safe = orden.stop_loss < liq_price
                if not safe:
                    self._log_event(
                        **_log_base, event_type="REJECTED_RISK",
                        rejection_reason=f"SL {orden.stop_loss:.4f} más allá de liquidación {liq_price:.4f} con {self.leverage}x",
                        details={"sl": orden.stop_loss, "liq_price": liq_price, "leverage": self.leverage},
                    )
                    return
                logger.debug(
                    f"[Futuros] Liquidación en {liq_price:.4f} | "
                    f"SL en {orden.stop_loss:.4f} — margen seguro OK"
                )

            # 6. Registrar la orden (paper) y loggear el evento
            order_record = {
                "symbol": symbol,
                "direction": orden.direction,
                "entry_price": orden.entry_price,
                "stop_loss": orden.stop_loss,
                "take_profit": orden.take_profit,
                "quantity": orden.quantity,
                "risk_usdt": orden.risk_usdt,
                "reward_usdt": orden.reward_usdt,
                "tp_pct": orden.tp_pct,
                "sl_pct": orden.sl_pct,
                "leverage": self.leverage,
                "futures": self.futures_enabled,
                "ai_probability": round(prob, 4),
                "monthly_level": signal.monthly_level,
                "volume_ratio": signal.volume_ratio,
                "confirmations": signal.confirmations,
                "confirmation_score": signal.confirmation_score,
                "entry_time": pd.Timestamp.now(tz="UTC").isoformat(),
            }

            self.open_orders[symbol] = order_record
            self.risk_manager.registrar_apertura(symbol)
            self._guardar_estado()
            self._log_event(
                symbol=symbol, strategy=strategy_used, event_type="TRADE_OPENED",
                direction=orden.direction, price=orden.entry_price,
                level_name=getattr(signal, "level_name", None),
                volume_ratio=signal.volume_ratio,
                details={"sl": orden.stop_loss, "tp": orden.take_profit,
                         "ai_prob": round(prob, 4), "leverage": self.leverage},
            )
            logger.success(f"Orden PAPER abierta: {orden.resumen()}")

        except Exception as e:
            logger.error(f"Error inesperado procesando {symbol}: {e}", exc_info=True)

    # ── API de control (usada por el dashboard web) ──────────────────────────

    def reload_config(self, new_config: dict):
        """Aplica un config actualizado en caliente sin reiniciar el bot."""
        self.config = new_config

        risk_cfg = new_config["risk"]
        # Recrea el RiskManager preservando las posiciones abiertas actuales
        open_syms = list(self.open_orders.keys())
        self.risk_manager = RiskManager(
            max_risk_pct=risk_cfg["max_risk_per_trade_pct"],
            tp_pct=risk_cfg["take_profit_pct"],
            tp_min_pct=risk_cfg["tp_min_pct"],
            tp_max_pct=risk_cfg["tp_max_pct"],
            sl_behind_pct=risk_cfg["sl_behind_level_pct"],
            max_positions=risk_cfg["max_open_positions"],
        )
        for sym in open_syms:
            self.risk_manager.registrar_apertura(sym)

        self.symbols = new_config["symbols"]

        futures_cfg = new_config.get("futures", {})
        self.futures_enabled = futures_cfg.get("enabled", False)
        self.leverage        = futures_cfg.get("leverage", 1)
        self.fee_pct = 0.0004 if self.futures_enabled else new_config["paper_trading"].get("fee_pct", 0.1) / 100

        guardrails = new_config.get("guardrails", {})
        self.max_daily_loss_pct = guardrails.get("max_daily_loss_pct", 3.0) / 100
        self.max_drawdown_pct   = guardrails.get("max_drawdown_pct", 10.0) / 100

        news_cfg = new_config.get("news", {})
        self._news_enabled        = news_cfg.get("enabled", False)
        self._news_pause_hours    = news_cfg.get("pause_hours", 4)
        self._news_check_interval = news_cfg.get("check_interval_minutes", 15) * 60

        logger.info(
            f"Config recargada en caliente | "
            f"Símbolos: {self.symbols} | "
            f"Posiciones: {risk_cfg['max_open_positions']} | "
            f"Leverage: {self.leverage}×"
        )

    def get_snapshot(self) -> dict:
        """Devuelve el estado completo del bot para la API REST / WebSocket."""
        wins   = sum(1 for t in self.trade_log if t.get("exit_type") == "TP")
        losses = sum(1 for t in self.trade_log if t.get("exit_type") == "SL")
        manual = sum(1 for t in self.trade_log if t.get("exit_type") == "MANUAL")
        total  = wins + losses + manual
        initial = self.config["paper_trading"]["initial_balance_usdt"]
        drawdown = (
            (self._balance_peak - self.balance_usdt) / self._balance_peak * 100
            if self._balance_peak > 0 else 0.0
        )
        return {
            "balance_usdt":    round(self.balance_usdt, 2),
            "balance_peak":    round(self._balance_peak, 2),
            "initial_balance": initial,
            "pnl_usdt":        round(self.balance_usdt - initial, 2),
            "pnl_pct":         round((self.balance_usdt - initial) / initial * 100, 2),
            "drawdown_pct":    round(drawdown, 2),
            "trading_halted":  self._trading_halted,
            "stop_requested":  self._stop_requested,
            "news_paused":     time.time() < self._news_paused_until,
            "news_paused_until": self._news_paused_until if time.time() < self._news_paused_until else None,
            "open_positions":  len(self.open_orders),
            "total_trades":    total,
            "wins":            wins,
            "losses":          losses,
            "manual_closes":   manual,
            "win_rate":        round(wins / total * 100, 1) if total > 0 else 0.0,
            "leverage":        self.leverage,
            "futures_enabled": self.futures_enabled,
            "mode":            f"futures {self.leverage}x" if self.futures_enabled else "spot",
            "symbols":         self.symbols,
            "open_orders":     self.open_orders,
        }

    def cerrar_posicion_manual(self, symbol: str) -> dict | None:
        """Cierra una posición abierta a precio de mercado actual. Devuelve el log entry."""
        if symbol not in self.open_orders:
            return None
        try:
            ticker = self.data_exchange.fetch_ticker(symbol)
            current_price = float(ticker["last"])
        except Exception as e:
            logger.error(f"[ManualClose] No se pudo obtener precio de {symbol}: {e}")
            return None

        order = self.open_orders[symbol]
        gross_pnl = order["quantity"] * (current_price - order["entry_price"])
        if order["direction"] == "short":
            gross_pnl = -gross_pnl
        gross_pnl *= self.leverage
        fee_usdt = order["entry_price"] * order["quantity"] * self.fee_pct * 2
        pnl_usdt = gross_pnl - fee_usdt
        pnl_pct  = (current_price - order["entry_price"]) / order["entry_price"]
        if order["direction"] == "short":
            pnl_pct = -pnl_pct
        pnl_pct *= self.leverage

        self.balance_usdt += pnl_usdt

        log_entry = {
            **order,
            "exit_price":    current_price,
            "exit_type":     "MANUAL",
            "pnl_pct":       round(pnl_pct * 100, 4),
            "pnl_usdt":      round(pnl_usdt, 2),
            "fee_usdt":      round(fee_usdt, 4),
            "balance_after": round(self.balance_usdt, 2),
        }
        self.trade_log.append(log_entry)
        del self.open_orders[symbol]
        self.risk_manager.registrar_cierre(symbol)
        self._guardar_estado()
        logger.warning(
            f"[MANUAL] {symbol} cerrado manualmente | "
            f"PnL: {pnl_usdt:+.2f} USDT | Balance: {self.balance_usdt:.2f} USDT"
        )
        return log_entry

    def cerrar_todo_y_parar(self) -> list[dict]:
        """Cierra todas las posiciones abiertas y activa el halt. Botón de emergencia."""
        closed = []
        for symbol in list(self.open_orders.keys()):
            result = self.cerrar_posicion_manual(symbol)
            if result:
                closed.append(result)
        self._trading_halted = True
        self._guardar_estado()
        logger.error(f"[EMERGENCY] Todas las posiciones cerradas y trading detenido.")
        return closed

    def reanudar(self):
        """Reactiva el trading tras un halt manual o de guardrails."""
        self._trading_halted = False
        self._guardar_estado()
        logger.info("[API] Trading reanudado manualmente.")

    def ejecutar(self, interval_seconds: int = 10):
        """
        Inicia el loop principal del paper trader.

        Args:
            interval_seconds: Segundos entre cada ciclo (default 10s)
                             La vela de 1m cierra cada 60s, pero ciclos más cortos
                             permiten detectar hits de SL/TP más rápido.
        """
        logger.info(f"Iniciando paper trader | Ciclo: cada {interval_seconds}s")

        while not self._stop_requested:
            if not self._verificar_guardrails():
                logger.info("Trading pausado por guardrail. Esperando siguiente ciclo...")
                time.sleep(interval_seconds)
                continue

            for symbol in self.symbols:
                self._procesar_simbolo(symbol)

            wins = sum(1 for t in self.trade_log if t.get("exit_type") == "TP")
            losses = sum(1 for t in self.trade_log if t.get("exit_type") == "SL")

            news_status = "🔕 paused" if time.time() < self._news_paused_until else ("enabled" if self._news_enabled else "disabled")
            mode = f"futures {self.leverage}x" if self.futures_enabled else "spot"
            logger.info(
                f"Balance: {self.balance_usdt:.2f} USDT | "
                f"Modo: {mode} | "
                f"Abiertas: {self.risk_manager.posiciones_abiertas} | "
                f"Wins/Losses: {wins}/{losses} | "
                f"News: {news_status}"
            )

            time.sleep(interval_seconds)
