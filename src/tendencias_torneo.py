"""Tendencias explicables del torneo actual para el plan Survivor.

La señal reciente complementa —no reemplaza— al Poisson histórico. En el
arranque se regulariza con un prior de ocho partidos y cualquier ajuste queda
limitado a pocos puntos porcentuales.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.team_normalizer import canonical_team_key

VENTANAS = (3, 5)
PRIOR_PARTIDOS = 8.0
MAX_AJUSTE = 0.04
MAX_AJUSTE_APRENDIZAJE = 0.015
VENTANA_APRENDIZAJE_DIAS = 28


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
    if anota == 0.0:
        etiquetas.append("NO_HA_MARCADO")
        razones.append(f"no marcó en sus últimos {pj} partidos")
    elif anota == 100.0:
        etiquetas.append("MARCA_EN_TODOS")
        razones.append(f"marcó en sus últimos {pj} partidos")
    if recibe == 100.0:
        etiquetas.append("RECIBE_EN_TODOS")
        razones.append(f"recibió gol en sus últimos {pj} partidos")
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


def _factor_recencia_aprendizaje(fecha: Any) -> float:
    """Decae linealmente el shock; la memoria histórica permanece persistida."""
    try:
        fecha_partido = date.fromisoformat(str(fecha or "")[:10])
    except ValueError:
        return 0.0
    antiguedad = max(0, (date.today() - fecha_partido).days)
    return max(0.0, 1.0 - antiguedad / VENTANA_APRENDIZAJE_DIAS)


def _enriquecer_con_aprendizajes(
    tendencias: Dict[str, Dict[str, Any]],
    aprendizajes: Sequence[Mapping[str, Any]],
) -> None:
    """Añade shocks pequeños y trazables; un batacazo nunca sustituye la forma.

    El marcador ya participa en la forma reciente. Este segundo componente es
    deliberadamente ortogonal: mide desviación frente a la expectativa previa,
    decae en 28 días y queda separado en ``senal_aprendizaje`` y limitado a
    ±1.5 puntos antes del límite total de ±4 puntos.
    """
    ajustes: Dict[str, float] = {}
    etiquetas: Dict[str, List[str]] = {}
    razones: Dict[str, List[str]] = {}

    def sumar(equipo: str, delta: float, etiqueta: str, razon: str) -> None:
        clave = canonical_team_key(equipo)
        if not clave or clave not in tendencias:
            return
        ajustes[clave] = ajustes.get(clave, 0.0) + delta
        etiquetas.setdefault(clave, []).append(etiqueta)
        razones.setdefault(clave, []).append(razon)

    for aprendizaje in aprendizajes:
        factor_recencia = _factor_recencia_aprendizaje(aprendizaje.get("fecha"))
        if factor_recencia <= 0.0:
            continue
        tipo = str(aprendizaje.get("tipo") or "")
        ganador = str(aprendizaje.get("ganador") or "")
        perdedor = str(aprendizaje.get("perdedor") or "")
        favorito = str(aprendizaje.get("favorito") or "")
        resumen = str(aprendizaje.get("resumen_interno") or "resultado prepartido contrastado")
        if tipo == "BATACAZO_GRANDE":
            sumar(ganador, 0.012 * factor_recencia, "BATACAZO_RECIENTE", resumen)
            sumar(perdedor, -0.012 * factor_recencia, "FAVORITO_VULNERABLE", resumen)
        elif tipo == "SORPRESA":
            sumar(ganador, 0.007 * factor_recencia, "SORPRESA_RECIENTE", resumen)
            sumar(perdedor, -0.007 * factor_recencia, "FAVORITO_VULNERABLE", resumen)
        elif tipo == "RESULTADO_CONTRA_PRONOSTICO":
            sumar(ganador, 0.004 * factor_recencia, "VICTORIA_CONTRA_PRONOSTICO", resumen)
            sumar(perdedor, -0.004 * factor_recencia, "DERROTA_CONTRA_PRONOSTICO", resumen)
        elif tipo == "FAVORITO_FRENADO":
            sumar(favorito, -0.005 * factor_recencia, "FAVORITO_FRENADO", resumen)

    for clave, tendencia in tendencias.items():
        forma = float(tendencia.get("senal") or 0.0)
        ajuste_aprendizaje = max(
            -MAX_AJUSTE_APRENDIZAJE,
            min(MAX_AJUSTE_APRENDIZAJE, ajustes.get(clave, 0.0)),
        )
        tendencia["senal_forma"] = round(forma, 6)
        tendencia["senal_aprendizaje"] = round(ajuste_aprendizaje, 6)
        tendencia["senal"] = round(max(-MAX_AJUSTE, min(MAX_AJUSTE, forma + ajuste_aprendizaje)), 6)
        for etiqueta in etiquetas.get(clave, []):
            if etiqueta not in tendencia["etiquetas"]:
                tendencia["etiquetas"].append(etiqueta)
        for razon in razones.get(clave, []):
            if razon not in tendencia["razones"]:
                tendencia["razones"].append(razon)


def calcular_tendencias(
    resultados: Sequence[Mapping[str, Any]],
    fortalezas_base: Optional[Mapping[str, float]] = None,
    aprendizajes: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Calcula forma y añade aprendizajes postpartido limitados y auditables."""
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
    if aprendizajes:
        _enriquecer_con_aprendizajes(salida, aprendizajes)
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


def cargar_aprendizajes_internos(fecha_inicio: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lee memoria persistida; si la tabla aún no existe, devuelve vacío."""
    try:
        from src import database as db

        return db.aprendizajes_partidos(limit=500, desde=fecha_inicio)
    except Exception:
        return []


def _resultados_desde_aprendizajes(aprendizajes: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    salida: List[Dict[str, Any]] = []
    for item in aprendizajes:
        home_goals_raw = item.get("home_goals")
        away_goals_raw = item.get("away_goals")
        if home_goals_raw is None or away_goals_raw is None:
            continue
        try:
            hg = int(home_goals_raw)
            ag = int(away_goals_raw)
        except (TypeError, ValueError):
            continue
        local = str(item.get("local") or "")
        visitante = str(item.get("visitante") or "")
        if not local or not visitante:
            continue
        salida.append(
            {
                "fecha": str(item.get("fecha") or "")[:10],
                "home_team": local,
                "away_team": visitante,
                "home_goals": hg,
                "away_goals": ag,
                "fuente": "memoria_postpartido",
            }
        )
    return salida


def _fusionar_resultados(*grupos: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    unicos: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    for grupo in grupos:
        for resultado in grupo:
            local = str(resultado.get("home_team") or "")
            visitante = str(resultado.get("away_team") or "")
            fecha = str(resultado.get("fecha") or resultado.get("match_date") or "")[:10]
            if not local or not visitante or not fecha:
                continue
            clave = (canonical_team_key(local), canonical_team_key(visitante), fecha)
            unicos[clave] = dict(resultado)
    return sorted(unicos.values(), key=lambda item: str(item.get("fecha") or item.get("match_date") or ""))


def cargar_resultados_torneo_actual(
    fecha_inicio: Optional[str] = None,
) -> Dict[str, Any]:
    """Combina API/ESPN con la memoria postpartido que alimenta el plan."""
    aprendizajes = cargar_aprendizajes_internos(fecha_inicio)
    internos = _resultados_desde_aprendizajes(aprendizajes)
    try:
        from src import ligamx_api

        estado = ligamx_api.estado_temporada()
        temporada = str(estado.get("tournament_now") or "") or None
        resultados_api = ligamx_api.resultados_historicos(season=temporada)
        combinados = _fusionar_resultados(resultados_api, internos)
        if combinados:
            return {
                "fuente": "LigaMX-API + memoria interna" if internos else "LigaMX-API",
                "temporada": temporada,
                "resultados": combinados,
                "aprendizajes": aprendizajes,
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
        combinados = _fusionar_resultados(resultados_respaldo, internos)
        fuente = datos.get("fuente", "respaldo") if isinstance(datos, dict) else "respaldo"
        if internos:
            fuente = f"{fuente} + memoria interna"
        return {
            "fuente": fuente,
            "temporada": None,
            "resultados": combinados,
            "aprendizajes": aprendizajes,
        }
    except Exception:
        return {
            "fuente": "memoria interna" if internos else "no_disponible",
            "temporada": None,
            "resultados": internos,
            "aprendizajes": aprendizajes,
        }
