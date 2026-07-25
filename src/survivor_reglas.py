from __future__ import annotations

from typing import Any, Dict, Sequence


def evaluar_temporada(picks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Aplica las reglas oficiales a picks persistidos, sin estado duplicado.

    La primera igualdad consume la única vida; la segunda igualdad o cualquier
    derrota eliminan. Los picks confirmados/bloqueados sin resultado permanecen
    pendientes, lo que también cubre partidos aplazados.
    """
    ordenados = sorted(picks, key=lambda pick: int(pick.get("jornada") or 0))
    victorias = 0
    empates = 0
    racha = 0
    vida_consumida = False
    vida_jornada = None
    eliminado_en = None
    fecha_eliminacion = None

    for pick in ordenados:
        if pick.get("estado") != "resuelto":
            continue
        resultado = str(pick.get("resultado") or "").lower()
        if eliminado_en is not None:
            continue
        if resultado == "gano":
            victorias += 1
            racha += 1
        elif resultado == "empate":
            empates += 1
            if vida_consumida:
                eliminado_en = pick.get("jornada")
                fecha_eliminacion = pick.get("resuelto_at") or pick.get("fecha")
            else:
                vida_consumida = True
                vida_jornada = pick.get("jornada")
                racha += 1
        elif resultado == "perdio":
            eliminado_en = pick.get("jornada")
            fecha_eliminacion = pick.get("resuelto_at") or pick.get("fecha")

    pendientes = [
        pick
        for pick in ordenados
        if pick.get("estado") in {"confirmado", "bloqueado"}
    ]
    return {
        "sigue_vivo": eliminado_en is None,
        "racha": racha,
        "victorias": victorias,
        "empates": empates,
        "vida_empate_consumida": vida_consumida,
        "vida_empate_jornada": vida_jornada,
        "eliminado_en": eliminado_en,
        "fecha_eliminacion": fecha_eliminacion,
        "picks_pendientes": pendientes,
        "pendientes": len(pendientes),
        "aviso_finalista": (
            "El bot registra tu jornada de eliminación, pero necesita datos de los demás "
            "participantes para saber si quedaste entre los últimos eliminados."
            if eliminado_en is not None
            else None
        ),
    }


def metricas_candidato(
    candidato: Dict[str, Any], vida_empate_consumida: bool
) -> Dict[str, float]:
    """Calcula supervivencia y score respetando el valor único del empate."""
    ganar = float(candidato.get("prob_victoria_pct") or 0.0)
    empatar = float(candidato.get("prob_empate_pct") or 0.0)
    supervivencia = ganar if vida_empate_consumida else ganar + empatar
    # Antes de gastarla, el empate ayuda a sobrevivir pero vale solo 35% en el
    # ranking porque consume un recurso único y no suma una victoria.
    score = ganar if vida_empate_consumida else ganar + 0.35 * empatar
    return {
        "supervivencia_pct": round(supervivencia, 2),
        "score_oficial": round(score, 2),
    }
