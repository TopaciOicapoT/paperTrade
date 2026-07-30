"""
api/bot_runner.py
-----------------
Ejecuta el PaperTrader en un thread separado para que conviva con FastAPI.
El bot es síncrono (time.sleep); uvicorn/FastAPI es async.
Correr el bot en un daemon thread es la forma más limpia de integración.
"""

import threading
from execution.paper_trader import PaperTrader


class BotRunner:
    def __init__(self, trader: PaperTrader, interval_seconds: int = 10):
        self.trader = trader
        self.interval = interval_seconds
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(
            target=self.trader.ejecutar,
            args=(self.interval,),
            daemon=True,
            name="paper-trader-loop",
        )
        self._thread.start()

    def stop(self):
        self.trader._stop_requested = True

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
