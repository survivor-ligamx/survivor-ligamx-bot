"""Tendencias explicables del torneo actual para el plan Survivor.

La señal reciente complementa —no reemplaza— al Poisson histórico. En el
arranque se regulariza con un prior de ocho partidos y cualquier ajuste queda
limitado a pocos puntos porcentuales.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.team_normalizer import canonical_team_key

VENTANAS = (3, 5)
PRIOR_PARTIDOS = 8.0
MAX_AJUSTE = 0.04


def _numero(valor: Any) -> Optional[float]:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _registro(
    equipo: str,
    rival: str,
    gf: float,
    gc: float,
    condicion: str,
    fecha: str,
) -> Dict[str, Any]:
    resultado = "G" if gf > gc else "E" if gf == gc else "P"
    return {
        "equipo": equipo,
        "rival": rival,
        "gf": gf,
        "gc": gc,
        "resultado": resultado,
        "condicion": condicion,
        "fecha": fecha,
    }


def _partidos_por_equipo(
    resultados: Sequence[Mapping[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    partidos: Dict[str, List[Dict[str, Any]]] = {}
    ordenados = sorted(
        resultados,
        key=lambda p: str(p.get("fecha") or p.get("kickoff_utc") or ""),
    )
    for partido in ordenados:
        local = str(partido.get("home_team") or "").strip()
        visita = str(partido.get("away_team") or "").strip()
        gl = _numero(partido.get("home_goals"))
        gv = _numero(partido.get("away_goals"))
        if not local or not visita or gl is None or gv is None:
            continue
        fecha = str(partido.get("fecha") or partido.get("kickoff_utc") or "")
        clave_local = canonical_team_key(local)
        clave_visita = canonical_team_key(visita)
        partidos.setdefault(clave_local, []).append(_registro(local, visita, gl, gv, "Local", fecha))
        partidos.setdefault(clave_visita, []).append(_registro(visita, local, gv, gl, "Visitante", fecha))
    return partidos


def _racha(partidos: Sequence[Mapping[str, Any]], valor_prohibido: str) -> int:
    total = 0
    for partido in reversed(partidos):
        if partido.get("resultado") == valor_prohibido:
            break
        total += 1
    return total


def _metricas(partidos: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    pj = len(partidos)
    if not pj:
        return {
            "pj": 0,
            "pg": 0,
            "pe": 0,
            "pp": 0,
            "gf_pp": 0.0,
            "gc_pp": 0.0,
            "puntos_pp": 0.0,
            "anota_pct": 0.0,
            "recibe_pct": 0.0,
            "porteria_cero_pct": 0.0,
            "btts_pct": 0.0,
            "racha_invicto": 0,
            "racha_sin_ganar": 0,
        }
    pg = sum(p.get("resultado") == "G" for p in partidos)
    pe = sum(p.get("resultado") == "E" for p in partidos)
    pp = pj - pg - pe
    gf = sum(float(p.get("gf") or 0.0) for p in partidos)
    gc = sum(float(p.get("gc") or 0.0) for p in partidos)
    anota = sum(float(p.get("gf") or 0.0) > 0 for p in partidos)
    recibe = sum(float(p.get("gc") or 0.0) > 0 for p in partidos)
    cero = sum(float(p.get("gc") or 0.0) == 0 for p in partidos)
    btts = sum(float(p.get("gf") or 0.0) > 0 and float(p.get("gc") or 0.0) > 0 for p in partidos)
    return {
        "pj": pj,
        "pg": pg,
        "pe": pe,
        "pp": pp,
        "gf": round(gf, 2),
        "gc": round(gc, 2),
        "gf_pp": round(gf / pj, 3),
        "gc_pp": round(gc / pj, 3),
        "diferencia_pp": round((gf - gc) / pj, 3),
        "puntos_pp": round((3 * pg + pe) / pj, 3),
        "anota_pct": round(100.0 * anota / pj, 1),
        "recibe_pct": round(100.0 * recibe / pj, 1),
        "porteria_cero_pct": round(100.0 * cero / pj, 1),
        "btts_pct": round(100.0 * btts / pj, 1),
        "racha_invicto": _racha(partidos, "P"),
        "racha_sin_ganar": _racha(partidos, "G"),
    }


def _fortaleza_base(
    equipo: str,
    fortalezas_base: Optional[Mapping[str, float]],
) -> float:
    if not fortalezas_base:
        return 1.0
    try:
        return float(fortalezas_base.get(equipo, 1.0))
    except (TypeError, ValueError):
        return 1.0


def _etiquetas(
    metricas: Mapping[str, Any],
    fortaleza: float,
) -> Tuple[List[str], List[str]]:
    pj = int(metricas.get("pj") or 0)
    if pj < 2:
        etiquetas_vacias: List[str] = []
        razones_vacias: List[str] = []
        return etiquetas_vacias, razones_vacias
    etiquetas: List[str] = []
    razones: List[str] = []
    gf_pp = float(metricas.get("gf_pp") or 0.0)
    gc_pp = float(metricas.get("gc_pp") or 0.0)
    ppg = float(metricas.get("puntos_pp") or 0.0)
    anota = float(metricas.get("anota_pct") or 0.0)
    recibe = float(metricas.get("recibe_pct") or 0.0)
    cero = float(metricas.get("porteria_cero_pct") or 0.0)
    diferencia = float(metricas.get("diferencia_pp") or 0.0)
    if gf_pp >= 1.5 and anota >= 75.0:
        etiquetas.append("ATAQUE_EN_FORMA")
        razones.append(f"anota {gf_pp:.1f} por partido y marcó en {anota:.0f}%")
    if gc_pp >= 1.5 and recibe >= 75.0:
        etiquetas.append("DEFENSA_VULNERABLE")
        razones.append(f"recibe {gc_pp:.1f} por partido y concedió en {recibe:.0f}%")
    if gc_pp <= 0.75 and cero >= 40.0:
        etiquetas.append("PORTERIA_SOLIDA")
        razones.append(f"recibe {gc_pp:.1f} por partido y dejó su arco en cero en {cero:.0f}%")
    if fortaleza < 1.08 and ppg >= 2.0 and diferencia >= 0.5:
        etiquetas.append("EQUIPO_SORPRESA")
        razones.append(f"rinde por encima de su base: {ppg:.1f} puntos por partido")
    if fortaleza >= 1.08 and ppg <= 1.0:
        etiquetas.append("FAVORITO_EN_BAJA")
        razones.append(f"favorito histórico con solo {ppg:.1f} puntos por partido")
    return etiquetas, razones


def calcular_tendencias(
    resultados: Sequence[Mapping[str, Any]],
    fortalezas_base: Optional[Mapping[str, float]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Calcula ventanas 3/5, local/visita, etiquetas y señal regularizada."""
    salida: Dict[str, Dict[str, Any]] = {}
    for equipo, partidos in _partidos_por_equipo(resultados).items():
        ventanas = {str(n): _metricas(partidos[-n:]) for n in VENTANAS}
        total = _metricas(partidos)
        reciente = ventanas["5"]
        fortaleza = _fortaleza_base(equipo, fortalezas_base)
        etiquetas, razones = _etiquetas(reciente, fortaleza)
        pj = int(reciente.get("pj") or 0)
        peso = pj / (pj + PRIOR_PARTIDOS)
        ppg = float(reciente.get("puntos_pp") or 0.0)
        diferencia = float(reciente.get("diferencia_pp") or 0.0)
        bruto = 0.025 * ((ppg - 1.35) / 1.65) + 0.015 * max(
            -1.5,
            min(1.5, diferencia),
        )
        senal = max(-MAX_AJUSTE, min(MAX_AJUSTE, bruto * peso))
        salida[equipo] = {
            "equipo": partidos[-1]["equipo"],
            "pj_torneo": len(partidos),
            "ventanas": ventanas,
            "total": total,
            "local": _metricas([p for p in partidos if p.get("condicion") == "Local"]),
            "visitante": _metricas([p for p in partidos if p.get("condicion") == "Visitante"]),
            "etiquetas": etiquetas,
            "razones": razones,
            "peso_actual": round(peso, 4),
            "senal": round(senal, 6),
            "muestra_preliminar": pj < 5,
        }
    return salida


def ajustar_probabilidades(
    probabilidades: Sequence[float],
    tendencia_local: Optional[Mapping[str, Any]],
    tendencia_visita: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Aplica señales limitadas a 1X2 y renormaliza; devuelve trazabilidad."""
    if len(probabilidades) != 3:
        raise ValueError("Se requieren probabilidades local, empate y visita")
    base: List[float] = [max(0.0, float(p)) for p in probabilidades]
    total_base = sum(base)
    if total_base <= 0:
        raise ValueError("Las probabilidades deben sumar más de cero")
    base = [p / total_base for p in base]
    sl = float((tendencia_local or {}).get("senal") or 0.0)
    sv = float((tendencia_visita or {}).get("senal") or 0.0)
    delta = max(-MAX_AJUSTE, min(MAX_AJUSTE, sl - sv))
    ajustadas: List[float] = [
        base[0] * (1.0 + delta),
        base[1],
        base[2] * (1.0 - delta),
    ]
    total_ajustado = sum(ajustadas)
    ajustadas = [p / total_ajustado for p in ajustadas]
    razones: List[str] = [str(razon) for razon in ((tendencia_local or {}).get("razones") or [])]
    razones.extend(f"rival: {razon}" for razon in ((tendencia_visita or {}).get("razones") or []))
    return {
        "base": base,
        "ajustadas": ajustadas,
        "cambio_local_pp": round(100.0 * (ajustadas[0] - base[0]), 2),
        "cambio_visita_pp": round(100.0 * (ajustadas[2] - base[2]), 2),
        "razones": razones,
    }


def ajustar_fuerzas(
    fuerzas: Mapping[str, Any],
    tendencias: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Devuelve copia de fuerzas Poisson enriquecida sin mutar el histórico."""
    ajustadas = deepcopy(dict(fuerzas))
    equipos = ajustadas.get("equipos")
    if not isinstance(equipos, dict):
        return ajustadas
    for clave, valores in equipos.items():
        if not isinstance(valores, dict):
            continue
        tendencia = tendencias.get(canonical_team_key(str(clave)))
        if not tendencia:
            continue
        senal = max(
            -MAX_AJUSTE,
            min(MAX_AJUSTE, float(tendencia.get("senal") or 0.0)),
        )
        for campo in ("ataque_local", "ataque_visita"):
            if campo in valores:
                valores[campo] = max(
                    0.1,
                    float(valores[campo]) * (1.0 + senal),
                )
        for campo in ("defensa_local", "defensa_visita"):
            if campo in valores:
                valores[campo] = max(
                    0.1,
                    float(valores[campo]) * (1.0 - senal),
                )
    return ajustadas


def cargar_resultados_torneo_actual(
    fecha_inicio: Optional[str] = None,
) -> Dict[str, Any]:
    """Liga MX API primero; ESPN/fuentes_datos filtrado como respaldo seguro."""
    try:
        from src import ligamx_api

        estado = ligamx_api.estado_temporada()
        temporada = str(estado.get("tournament_now") or "") or None
        resultados_api = ligamx_api.resultados_historicos(season=temporada)
        if resultados_api:
            return {
                "fuente": "LigaMX-API",
                "temporada": temporada,
                "resultados": resultados_api,
            }
    except Exception:
        pass
    try:
        from src import fuentes_datos

        datos = fuentes_datos.obtener_resultados(meses=6)
        resultados_respaldo = datos.get("resultados") if isinstance(datos, dict) else []
        if not isinstance(resultados_respaldo, list):
            resultados_respaldo = []
        if fecha_inicio:
            resultados_respaldo = [
                resultado
                for resultado in resultados_respaldo
                if isinstance(resultado, Mapping) and str(resultado.get("fecha") or "")[:10] >= fecha_inicio[:10]
            ]
        fuente = datos.get("fuente", "respaldo") if isinstance(datos, dict) else "respaldo"
        return {
            "fuente": fuente,
            "temporada": None,
            "resultados": resultados_respaldo,
        }
    except Exception:
        return {
            "fuente": "no_disponible",
            "temporada": None,
            "resultados": [],
        }
