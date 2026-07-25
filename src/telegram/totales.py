from __future__ import annotations

import math
from typing import Any, Dict, Sequence

_PARTIDOS_JORNADA = 9


def _goles_esperados(partido: Dict[str, Any]) -> float | None:
    """Devuelve el xG total solo cuando ambos valores son válidos."""
    try:
        local = float(partido["goles_esperados_local"])
        visitante = float(partido["goles_esperados_visitante"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(local) or not math.isfinite(visitante):
        return None
    if local < 0 or visitante < 0:
        return None
    return local + visitante


def calcular_totales_jornada(
    pronosticos: Sequence[Dict[str, Any]],
    partidos_esperados: int = _PARTIDOS_JORNADA,
) -> Dict[str, Any]:
    """Calcula cobertura y goles sin convertir partidos sin modelo en ceros."""
    esperados = max(0, int(partidos_esperados))
    considerados = list(pronosticos)[:esperados] if esperados else []
    goles_validos = [goles for p in considerados if (goles := _goles_esperados(p)) is not None]
    total_goles = sum(goles_validos)
    con_modelo = len(goles_validos)
    cobertura_completa = esperados > 0 and con_modelo == esperados

    return {
        "partidos": len(considerados),
        "partidos_esperados": esperados,
        "partidos_con_xg": con_modelo,
        "partidos_sin_xg": max(0, esperados - con_modelo),
        "cobertura_completa": cobertura_completa,
        "goles_desempate": math.floor(total_goles + 0.5) if cobertura_completa else None,
        "goles_esperados_total": round(total_goles, 1),
        "promedio_goles_partido": round(total_goles / con_modelo, 2) if con_modelo else 0.0,
        "over_25_count": sum(1 for p in considerados if p.get("pick_ou") == "Over"),
        "under_25_count": sum(1 for p in considerados if p.get("pick_ou") == "Under"),
        "btts_si_count": sum(1 for p in considerados if p.get("pick_btts") == "Sí"),
        "btts_no_count": sum(1 for p in considerados if p.get("pick_btts") == "No"),
    }
