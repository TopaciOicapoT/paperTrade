import threading
import time
import uuid
from typing import Any

_jobs: dict[str, dict[str, Any]] = {}
_cancel_events: dict[str, threading.Event] = {}
_lock = threading.Lock()
_TTL = 10800  # 3 horas — simulaciones largas (8 símbolos × 10 años) pueden tardar >30 min sin caché


def _prune():
    now = time.time()
    with _lock:
        expired = [jid for jid, j in _jobs.items() if now - j["created_at"] > _TTL]
        for jid in expired:
            del _jobs[jid]
            _cancel_events.pop(jid, None)


def create_job() -> str:
    _prune()
    job_id = str(uuid.uuid4())
    with _lock:
        _jobs[job_id] = {
            "status":     "pending",
            "progress":   [],
            "result":     None,
            "error":      None,
            "created_at": time.time(),
        }
        _cancel_events[job_id] = threading.Event()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _lock:
        return dict(_jobs[job_id]) if job_id in _jobs else None


def cancel_job(job_id: str) -> bool:
    with _lock:
        if job_id not in _jobs:
            return False
        if _jobs[job_id]["status"] not in ("pending", "running"):
            return False
        _jobs[job_id]["status"] = "cancelled"
        _jobs[job_id]["error"] = "Simulación cancelada por el usuario."
    if job_id in _cancel_events:
        _cancel_events[job_id].set()
    return True


def _set_progress(job_id: str, msg: str):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["progress"].append(msg)


def _set_done(job_id: str, result: dict):
    with _lock:
        if job_id in _jobs and _jobs[job_id]["status"] != "cancelled":
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["result"] = result


def _set_error(job_id: str, err: str):
    with _lock:
        if job_id in _jobs and _jobs[job_id]["status"] != "cancelled":
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = err


def start_simulation(
    job_id: str,
    symbols: list[str],
    capital: float,
    max_positions: int,
    days: int,
    leverage: int,
    config: dict,
    symbol_params_override: dict | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    strategy_entries: list[dict] | None = None,
    levels_override: dict | None = None,
    include_filter_analysis: bool = False,
):
    if strategy_entries is None and symbol_params_override is not None:
        config = {**config, "symbol_params": symbol_params_override}
    if levels_override:
        config = {**config, "levels": {**config.get("levels", {}), **levels_override}}

    with _lock:
        _jobs[job_id]["status"] = "running"

    cancel_event = _cancel_events.get(job_id, threading.Event())

    def _progress(msg: str):
        if cancel_event.is_set():
            raise InterruptedError("Cancelado")
        _set_progress(job_id, msg)

    def _run():
        try:
            from backtesting.portfolio import run_simulation
            from concurrent.futures import ThreadPoolExecutor, as_completed

            sym_list = ', '.join(symbols)
            yrs = round(days / 365, 1)
            period_label = f"{date_from} → {date_to}" if date_from else f"{yrs} años"
            _progress(f"Iniciando análisis: {sym_list} · {period_label} · capital ${capital}")

            # ── Pass 1 y Pass 2 en paralelo ──────────────────────────────────
            # Las dos pasadas son completamente independientes; ThreadPoolExecutor
            # permite solaparlas y aprovechar que pandas/numpy liberan el GIL.
            _progress("PASS:1")
            _progress("PASS:2")
            _progress("Analizando base (sin filtros) y con filtros en paralelo…")

            common = dict(
                symbols=symbols, capital=capital, max_positions=max_positions,
                days=days, leverage=leverage, config=config,
                date_from=date_from, date_to=date_to, strategy_entries=strategy_entries,
            )

            def _sym_progress(msg):
                if cancel_event.is_set():
                    raise InterruptedError("Cancelado")
                _set_progress(job_id, msg)

            with ThreadPoolExecutor(max_workers=2) as pool:
                f_sin = pool.submit(run_simulation, **common, apply_filters=False, progress_cb=_sym_progress)
                f_con = pool.submit(run_simulation, **common, apply_filters=True,  progress_cb=None)
                sin = f_sin.result()
                con = f_con.result()

            _progress("DONE")

            # ── Análisis de filtros (opcional) ────────────────────────────────
            filter_analysis: dict = {}
            if include_filter_analysis:
                _progress("FILTERPASS")
                _progress("Analizando el impacto individual de cada filtro por criptomoneda…")
                from backtesting.portfolio import run_filter_analysis

                filter_syms = list(dict.fromkeys(e["symbol"] for e in strategy_entries)) if strategy_entries else symbols

                # Ejecutar análisis de filtros por símbolo en paralelo
                def _fa_one(sym):
                    _set_progress(job_id, f"FILTERANAL:{sym}")
                    from backtesting.portfolio import run_filter_analysis as _rfa
                    return _rfa(
                        symbols=[sym], days=days, leverage=leverage, config=config,
                        date_from=date_from, date_to=date_to,
                        strategy_entries=[e for e in (strategy_entries or []) if e["symbol"] == sym] or None,
                    )

                with ThreadPoolExecutor(max_workers=min(len(filter_syms), 4)) as pool:
                    futures = {pool.submit(_fa_one, s): s for s in filter_syms}
                    for fut in as_completed(futures):
                        filter_analysis.update(fut.result())

            _por_con = con.get("por_simbolo") or []
            _por_sin = sin.get("por_simbolo") or []
            _por_any = _por_con if _por_con else _por_sin
            result_syms = list(dict.fromkeys(s["symbol"] for s in _por_any)) if _por_any else symbols

            _set_done(job_id, {
                "params": {
                    "symbols": result_syms, "capital": capital,
                    "max_positions": max_positions, "days": days,
                    "leverage": leverage,
                    "requested_years": round(days / 365, 1),
                    "actual_years": sin.get("actual_years", round(days / 365, 1)),
                    "symbol_params": config.get("symbol_params", {}),
                },
                "sin_filtros": sin,
                "con_filtros": con,
                "filter_analysis": filter_analysis,
            })
        except InterruptedError:
            pass
        except Exception as e:
            _set_error(job_id, str(e))

    t = threading.Thread(target=_run, daemon=True, name=f"sim-{job_id[:8]}")
    t.start()
