"""
models/predictor.py
-------------------
Usa el modelo XGBoost entrenado para evaluar si un breakout detectado
tiene alta probabilidad de ser real (no un fakeout).

Flujo:
  señal de breakout → features de la vela actual → XGBoost → probabilidad
  Si probabilidad >= umbral → emitir señal de entrada
"""

import pandas as pd
import numpy as np
from loguru import logger

from models.trainer import cargar_modelo
from indicators.technical import calcular_features, FEATURE_COLUMNS


class Predictor:
    """
    Wrapper del modelo XGBoost para inferencia en tiempo real.

    Usage:
        predictor = Predictor()
        if predictor.model_disponible():
            prob = predictor.predecir(df_scalp)
            if prob >= 0.72:
                # autorizar entrada
    """

    def __init__(self, threshold: float = 0.72):
        self.threshold = threshold
        self.model, self.scaler = cargar_modelo()

    def model_disponible(self) -> bool:
        return self.model is not None and self.scaler is not None

    def predecir(self, df: pd.DataFrame) -> float:
        """
        Calcula la probabilidad de que el último breakout sea exitoso.

        Args:
            df: DataFrame OHLCV del marco de scalping con al menos 50 velas

        Returns:
            Probabilidad entre 0 y 1 (float). Retorna 0.0 si el modelo
            no está disponible o faltan datos.
        """
        if not self.model_disponible():
            logger.warning("Modelo no disponible — se omite filtro IA")
            return 0.0

        if len(df) < 30:
            logger.warning("Insuficientes velas para calcular features")
            return 0.0

        df_features = calcular_features(df)
        last_row = df_features[FEATURE_COLUMNS].iloc[-1]

        if last_row.isna().any():
            logger.warning("Features con NaN en la última vela")
            return 0.0

        X = pd.DataFrame([last_row])
        X_scaled = self.scaler.transform(X)
        prob = float(self.model.predict_proba(X_scaled)[0][1])

        logger.info(f"Probabilidad de éxito del breakout: {prob:.2%}")
        return prob

    def autorizar_entrada(self, df: pd.DataFrame) -> tuple[bool, float]:
        """
        Determina si se debe autorizar una entrada.

        Returns:
            (autorizado: bool, probabilidad: float)
        """
        prob = self.predecir(df)
        autorizado = prob >= self.threshold

        if autorizado:
            logger.success(f"Entrada autorizada por IA (prob={prob:.2%} >= {self.threshold:.2%})")
        else:
            logger.info(f"Entrada rechazada por IA (prob={prob:.2%} < {self.threshold:.2%})")

        return autorizado, prob

    def importancia_features(self) -> dict[str, float] | None:
        """Retorna la importancia de cada feature del modelo (útil para debugging)."""
        if not self.model_disponible():
            return None
        scores = self.model.feature_importances_
        return dict(sorted(zip(FEATURE_COLUMNS, scores), key=lambda x: x[1], reverse=True))
