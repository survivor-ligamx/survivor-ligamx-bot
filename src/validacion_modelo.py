#!/usr/bin/env python3
"""
validacion_modelo.py — ¿Qué tan bueno es el modelo? (backtesting honesto).

Valida el modelo Poisson contra resultados REALES de ESPN, sin trampas:
- Ordena los partidos por fecha.
- Entrena la fuerza de equipos con la parte ANTIGUA (train).
- Predice la parte RECIENTE (test) y compara con lo que de verdad pasó.

Métricas:
- accuracy: % de aciertos del pick 1X2.
- brier_promedio: calibración de probabilidades (menor = mejor; 0 = perfecto).
- baseline_local: accuracy de "siempre gana local" (para comparar si el modelo
  aporta sobre lo trivial).

Sin red propia (recibe resultados) ni momios. Informativo.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence
import logging

logger = logging.getLogger(__name__)

try:
    import poisson_model as pm
    from backtesting import brier_score
except ImportError:  # pragma: no cover
    from src import poisson_model as pm  # type: ignore
    from src.backtesting import brier_score  # type: ignore


def _resultado_1x2(home_goals: int, away_goals: int) -> int:
    if home_goals > away_goals:
        return 1
    if home_goals == away_goals:
        return 2
    return 3


def evaluar_modelo(
    resultados: Sequence[Dict[str, Any]],
    fraccion_test: float = 0.3,
) -> Dict[str, Any]:
    """
    Entrena con los partidos antiguos y evalúa con los recientes.
    Requiere suficientes partidos; si no, devuelve n_evaluados=0.
    """
    ordenados = sorted(resultados, key=lambda r: str(r.get("fecha", "")))
    n = len(ordenados)
    if n < 10:
        return {"n_evaluados": 0, "mensaje": "Datos insuficientes para validar."}

    corte = int(n * (1 - fraccion_test))
    train, test = ordenados[:corte], ordenados[corte:]
    if not train or not test:
        return {"n_evaluados": 0, "mensaje": "Partición vacía."}

    try:
        fuerzas = pm.calcular_fuerzas(train)
    except ValueError:
        return {"n_evaluados": 0, "mensaje": "No se pudieron estimar fuerzas."}

    eq = fuerzas.get("equipos", {})
    aciertos = 0
    aciertos_local = 0
    n_eval = 0
    brier_total = 0.0

    for m in test:
        home, away = m.get("home_team", ""), m.get("away_team", "")
        if pm._norm(home) not in eq or pm._norm(away) not in eq:
            continue
        try:
            hg, ag = int(m["home_goals"]), int(m["away_goals"])
        except (KeyError, TypeError, ValueError):
            continue
        actual = _resultado_1x2(hg, ag)
        pron = pm.pronostico(home, away, fuerzas)
        probs = [
            pron["prob_local_pct"] / 100.0,
            pron["prob_empate_pct"] / 100.0,
            pron["prob_visitante_pct"] / 100.0,
        ]
        pick = max(range(3), key=lambda i: probs[i]) + 1
        if pick == actual:
            aciertos += 1
        if actual == 1:
            aciertos_local += 1
        brier_total += brier_score(probs, actual)
        n_eval += 1

    if n_eval == 0:
        return {"n_evaluados": 0, "mensaje": "Sin partidos evaluables en test."}

    return {
        "n_train": len(train),
        "n_evaluados": n_eval,
        "accuracy": round(aciertos / n_eval, 4),
        "brier_promedio": round(brier_total / n_eval, 4),
        "baseline_local": round(aciertos_local / n_eval, 4),
        "mejor_que_baseline": (aciertos / n_eval) > (aciertos_local / n_eval),
        "decision": "INFORMATIVO / REVISIÓN HUMANA",
    }


def metricas_rendimiento() -> dict:
    """
    Métricas de negocio del modelo predictivo.

    La fuente de verdad es `pronosticos_historial`: una fila por partido
    pronosticado, con las columnas `acierto_1x2` y `acierto_marcador` ya
    resueltas contra el resultado real por `settle_pronosticos()`.

    Antes esto leia la tabla `picks`, que es otra cosa: son apuestas de valor
    (mercado, momio, EV, Kelly) y su columna `result` es el rendimiento de la
    apuesta, no si el 1X2 se acerto. Encima contaba las filas sin liquidar,
    que arrancan en result=0.0, asi que cada pick pendiente entraba al
    denominador como si fuera un fallo.

    Returns:
        dict con accuracy_1x2, accuracy_marcador, brier_score,
        accuracy_por_jornada, latencia_espn_promedio_ms,
        total_predicciones, pendientes, ultima_actualizacion
    """
    import os
    import json
    from datetime import datetime, timezone

    metrics: Dict[str, Any] = {
        "accuracy_1x2": None,
        "accuracy_marcador": None,
        "brier_score": None,
        "accuracy_por_jornada": [],
        "latencia_espn_promedio_ms": None,
        "total_predicciones": 0,
        "pendientes": 0,
        "ultima_actualizacion": None,
    }

    # Intentar cargar desde cache de métricas si existe
    cache_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "metricas_cache.json"
    )
    try:
        with open(cache_path, "r") as f:
            cached = json.load(f)
            metrics.update(cached)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.debug("Exception silenciada en metricas_rendimiento", exc_info=True)

    # Si no hay cache, calcular desde el track-record de pronósticos resueltos.
    if metrics["total_predicciones"] == 0:
        try:
            from src.database import rentabilidad_pronosticos

            track = rentabilidad_pronosticos()
            resueltos = int(track.get("resueltos") or 0)
            metrics["pendientes"] = int(track.get("pendientes") or 0)
            if resueltos > 0:
                aciertos_1x2 = int(track.get("aciertos_1x2") or 0)
                aciertos_marcador = int(track.get("aciertos_marcador_exacto") or 0)
                metrics["accuracy_1x2"] = round(aciertos_1x2 / resueltos, 4)
                metrics["accuracy_marcador"] = round(aciertos_marcador / resueltos, 4)
                metrics["total_predicciones"] = resueltos
                metrics["ultima_actualizacion"] = datetime.now(timezone.utc).isoformat()
        except Exception:
            logger.debug("Exception silenciada en metricas_rendimiento", exc_info=True)

    return metrics


def main() -> int:
    from src import fuentes_datos

    print("📏 Validando modelo contra resultados reales de ESPN...")
    datos = fuentes_datos.obtener_resultados(meses=18)
    r = evaluar_modelo(datos["resultados"])
    if r.get("n_evaluados", 0) == 0:
        print(f"⚠️ {r.get('mensaje')}")
        return 1
    print(f"Fuente: {datos['fuente']} | train: {r['n_train']} | test: {r['n_evaluados']}")
    print(f"Accuracy 1X2: {r['accuracy'] * 100:.1f}%  (baseline 'siempre local': {r['baseline_local'] * 100:.1f}%)")
    print(f"Brier promedio: {r['brier_promedio']}  (menor = mejor)")
    print(f"¿Mejor que baseline?: {'SÍ' if r['mejor_que_baseline'] else 'NO'}")
    return 0


# El guard va al final a proposito: antes estaba a media altura del archivo y
# `metricas_rendimiento` quedaba definida DESPUES, asi que al correr el modulo
# como script esa funcion nunca llegaba a existir.
if __name__ == "__main__":
    raise SystemExit(main())
