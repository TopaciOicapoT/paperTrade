"""
risk/manager.py
---------------
Gestiona el riesgo por operación adaptado a la estrategia:

ESTRATEGIA DE SALIDA:
  - TP objetivo: 1-1.5% sobre el precio de entrada
  - Si la rotura tiene mucho impulso → apuntar al 1.5%
  - Si la rotura es justa → salir en el 1%
  - SL: detrás del nivel mensual roto (0.20% más allá del nivel)

REGLA DE ORO: SL y TP se colocan como órdenes fijas en el broker,
nunca esperando una decisión en tiempo real.
"""

from dataclasses import dataclass
from loguru import logger


@dataclass
class OrdenParams:
    """Parámetros calculados para una orden de entrada."""
    symbol: str
    direction: str          # "long" o "short"
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float         # Cantidad en base asset (ej. BTC)
    risk_usdt: float        # Riesgo en USDT para esta operación
    reward_usdt: float      # Ganancia potencial en USDT
    tp_pct: float           # % de take-profit real
    sl_pct: float           # % de stop-loss real

    def resumen(self) -> str:
        ratio = self.tp_pct / self.sl_pct if self.sl_pct > 0 else 0
        return (
            f"{self.direction.upper()} {self.symbol} | "
            f"Entry: {self.entry_price:.4f} | "
            f"SL: {self.stop_loss:.4f} (-{self.sl_pct:.2f}%) | "
            f"TP: {self.take_profit:.4f} (+{self.tp_pct:.2f}%) | "
            f"Ratio 1:{ratio:.1f} | "
            f"Qty: {self.quantity:.6f} | "
            f"Riesgo: {self.risk_usdt:.2f} USDT"
        )


class RiskManager:
    """
    Calcula los parámetros de riesgo adaptados a la estrategia de breakout mensual.

    TP fijo entre 1% y 1.5% — no basado en ATR sino en el objetivo de la estrategia.
    SL detrás del nivel mensual roto (0.20% más allá del nivel).

    Args:
        max_risk_pct:    Máximo % del capital a arriesgar por operación (default 1%)
        tp_pct:          % de take-profit objetivo (default 1.25%)
        tp_min_pct:      TP mínimo (default 1.0%)
        tp_max_pct:      TP máximo si hay mucho impulso (default 1.5%)
        sl_behind_pct:   % de stop-loss más allá del nivel roto (default 0.20%)
        max_positions:   Posiciones simultáneas máximas (default 2)
    """

    def __init__(
        self,
        max_risk_pct: float = 1.0,
        tp_pct: float = 1.25,
        tp_min_pct: float = 1.0,
        tp_max_pct: float = 1.5,
        sl_behind_pct: float = 0.20,
        max_positions: int = 2,
    ):
        self.max_risk_pct = max_risk_pct
        self.tp_pct = tp_pct
        self.tp_min_pct = tp_min_pct
        self.tp_max_pct = tp_max_pct
        self.sl_behind_pct = sl_behind_pct
        self.max_positions = max_positions
        self._open_positions: list[str] = []

    def calcular_orden(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        monthly_level: float,
        balance_usdt: float,
        volume_ratio: float = 1.8,
    ) -> OrdenParams | None:
        """
        Calcula los parámetros de la orden.

        Stop-loss: detrás del nivel mensual roto (0.20% más allá).
        Take-profit: entre 1% y 1.5% según el impulso del volumen.
          - Vol ratio >= 2.5× → TP al 1.5% (mucho impulso)
          - Vol ratio entre 1.8-2.5× → TP al 1.25%
          - Vol ratio < 1.8× → TP al 1.0% (impulso justo)

        Args:
            symbol:        Par de trading
            direction:     "long" o "short"
            entry_price:   Precio de entrada
            monthly_level: Precio del nivel mensual roto (para colocar el SL)
            balance_usdt:  Balance disponible en USDT
            volume_ratio:  Ratio de volumen del spike (para ajustar TP)

        Returns:
            OrdenParams si todo es válido, None si el SL queda por encima del entry.
        """
        # ── Stop-loss: % fijo desde el precio de entrada ──
        # (anclado a entry, no al nivel mensual, para controlar pérdida máxima)
        sl_offset = entry_price * (self.sl_behind_pct / 100)

        if direction == "long":
            stop_loss = entry_price - sl_offset
        else:
            stop_loss = entry_price + sl_offset

        sl_distance = sl_offset  # = entry_price * sl_behind_pct / 100

        # ── Take-profit: ajustado por impulso de volumen ──
        if volume_ratio >= 2.5:
            tp_pct = self.tp_max_pct    # 1.5% — rotura con mucho impulso
        elif volume_ratio >= 2.0:
            tp_pct = self.tp_pct        # 1.25% — impulso normal
        else:
            tp_pct = self.tp_min_pct    # 1.0% — impulso justo

        tp_distance = entry_price * (tp_pct / 100)

        if direction == "long":
            take_profit = entry_price + tp_distance
        else:
            take_profit = entry_price - tp_distance

        sl_pct = (sl_distance / entry_price) * 100

        # ── Tamaño de posición: riesgo fijo en USDT ──
        # El capital se divide entre los slots máximos para que N posiciones
        # simultáneas nunca superen el presupuesto total.
        # Ej: balance=1000, max_positions=2 → capital_slot=500 por posición (50%).
        # Para usar 100% por trade: poner max_open_positions=1 en config.yaml.
        capital_slot = balance_usdt / self.max_positions
        risk_usdt = capital_slot * (self.max_risk_pct / 100)
        quantity = risk_usdt / sl_distance if sl_distance > 0 else 0
        reward_usdt = quantity * tp_distance

        params = OrdenParams(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            quantity=round(quantity, 6),
            risk_usdt=round(risk_usdt, 2),
            reward_usdt=round(reward_usdt, 2),
            tp_pct=round(tp_pct, 2),
            sl_pct=round(sl_pct, 4),
        )

        logger.info(f"Orden calculada: {params.resumen()}")
        return params

    def puede_abrir_posicion(self, symbol: str) -> bool:
        """Verifica que no se supere el límite de posiciones simultáneas."""
        if symbol in self._open_positions:
            logger.warning(f"Ya hay una posición abierta en {symbol}")
            return False
        if len(self._open_positions) >= self.max_positions:
            logger.warning(
                f"Límite de posiciones alcanzado ({self.max_positions}) — "
                f"no se abre nueva operación"
            )
            return False
        return True

    def registrar_apertura(self, symbol: str):
        if symbol not in self._open_positions:
            self._open_positions.append(symbol)
            logger.info(f"Posición registrada: {symbol} | Abiertas: {self._open_positions}")

    def registrar_cierre(self, symbol: str):
        if symbol in self._open_positions:
            self._open_positions.remove(symbol)
            logger.info(f"Posición cerrada: {symbol} | Abiertas: {self._open_positions}")

    @property
    def posiciones_abiertas(self) -> list[str]:
        return list(self._open_positions)
