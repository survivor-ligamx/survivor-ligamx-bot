#!/usr/bin/env python3
"""Tareas automáticas de análisis y contexto web del torneo.

El análisis post-jornada se ejecuta semanalmente. La investigación web corre en
un hilo separado cada seis horas y solo consulta cuando una jornada empieza en
48 horas o terminó durante las últimas 24 horas. La caché evita gastar créditos
repetidos. Para apagar todo: SCHEDULER_ENABLED=false. Para apagar únicamente la
investigación: WEB_CONTEXT_ENABLED=false.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - muy viejo
    ZoneInfo = None  # type: ignore


def _habilitado() -> bool:
    return os.getenv("SCHEDULER_ENABLED", "1").strip().lower() not in ("false", "0", "off", "no")


def _contexto_habilitado() -> bool:
    valor = os.getenv("WEB_CONTEXT_ENABLED", "1").strip().lower()
    claves = any(os.getenv(nombre, "").strip() for nombre in ("TAVILY_API_KEY", "GNEWS_API_KEY", "SERPER_API_KEY"))
    return valor not in {"false", "0", "off", "no"} and claves


def _zona():
    if ZoneInfo is not None:
        try:
            return ZoneInfo("America/Mexico_City")
        except Exception:
            logger.debug("Exception silenciada en _zona", exc_info=True)
    return None


def _proximo_disparo() -> float:
    """Segundos hasta el próximo día y hora configurados en CDMX."""
    hora = int(os.getenv("SCHEDULER_HOUR", "23") or "23")
    minuto = int(os.getenv("SCHEDULER_MINUTE", "0") or "0")
    dia = int(os.getenv("SCHEDULER_WEEKDAY", "6") or "6")
    tz = _zona()
    ahora = datetime.now(tz) if tz else datetime.now()
    dias_espera = (dia - ahora.weekday()) % 7
    if dias_espera == 0 and (ahora.hour, ahora.minute, ahora.second) >= (hora, minuto, 0):
        dias_espera = 7
    prox = ahora.replace(hour=hora, minute=minuto, second=0, microsecond=0) + timedelta(days=dias_espera)
    return max(0.0, (prox - ahora).total_seconds())


def _wake_up() -> None:
    import requests

    port = os.getenv("PORT", "8000")
    api_base = os.getenv("API_BASE", "").strip().rstrip("/")
    urls = [f"http://127.0.0.1:{port}/health"]
    if api_base:
        urls.append(f"{api_base}/health")
    for url in urls:
        try:
            requests.get(url, timeout=10)
        except Exception:
            logger.debug("Exception silenciada en _wake_up", exc_info=True)


def _loop() -> None:
    from src.telegram_pronosticos import enviar_analisis_jornada

    wakeup = int(os.getenv("SCHEDULER_WAKEUP_MINUTES", "10") or "10")
    while True:
        espera = _proximo_disparo()
        if espera > wakeup * 60:
            time.sleep(max(0.0, espera - wakeup * 60))
            _wake_up()
            time.sleep(wakeup * 60)
        else:
            time.sleep(espera)
        try:
            enviar_analisis_jornada()
        except Exception:
            logger.debug("Exception silenciada en _loop", exc_info=True)
        time.sleep(120)


def _loop_contexto_web() -> None:
    """Investiga previa/post cada pocas horas; errores externos nunca matan el hilo."""
    from src.contexto_web_partidos import actualizar_contexto_automatico

    time.sleep(max(10, int(os.getenv("WEB_CONTEXT_INITIAL_DELAY_SECONDS", "45") or "45")))
    while True:
        try:
            resultado = actualizar_contexto_automatico()
            logger.info("Actualización de contexto web: %s", resultado)
        except Exception:
            logger.warning("No se pudo actualizar el contexto web", exc_info=True)
        horas = max(1, int(os.getenv("WEB_CONTEXT_INTERVAL_HOURS", "6") or "6"))
        time.sleep(horas * 3600)


def arrancar() -> None:
    """Arranca hilos opcionales; la llamada completa sigue siendo idempotente desde api.py."""
    if not _habilitado():
        return
    threading.Thread(target=_loop, name="analisis-semanal-scheduler", daemon=True).start()
    if _contexto_habilitado():
        threading.Thread(target=_loop_contexto_web, name="contexto-web-scheduler", daemon=True).start()
