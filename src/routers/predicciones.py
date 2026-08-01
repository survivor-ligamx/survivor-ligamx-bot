#!/usr/bin/env python3
"""
routers/predicciones.py — Endpoints de predicciones REALES (ESPN + Poisson).

Expone en la web las predicciones legítimas basadas en datos reales de ESPN
(vía el motor), en lugar de los momios inventados. Read-only, con caché en
memoria (TTL) para no golpear ESPN en cada request.

- GET /predicciones  -> 1X2 / Over-Under / BTTS / marcador por partido próximo.
- GET /survivor      -> mejor equipo "no perder" de la jornada (excluye usados).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, cast

from fastapi import APIRouter, Request
import logging

logger = logging.getLogger(__name__)

from src.rate_limit import limiter

from src import motor_pronosticos as motor

from src import tabla_posiciones as tabla_mod

from src import comparador_mercado as mercado_mod

from src import fuentes_datos as fuentes_mod

from src import analisis_riesgo as riesgo_mod

from src import planificador_survivor as plan_mod

from src import poisson_model as pm

from src import ligamx_api as lmx

router = APIRouter(tags=["Predicciones"])

# Distribución de picks de la comunidad (Playdoit Survivor Fecha 1).
# Sirve para identificar "picks populares" que eliminan a muchos si fallan.
# Fuente: Pick Distribution pública de Playdoit.
# ⚠️ Actualizar cada jornada: estos % cambian. Fecha de captura: 2026-07-11 (J1).
CROWD_DISTRIBUTION: Dict[str, float] = {
    "Monterrey": 27.94,
    "Necaxa": 22.25,
    "FC Juarez": 19.68,
    "Cruz Azul": 8.84,
    "America": 6.92,
    "Leon": 5.97,
    "Atlante": 1.85,
    "Tigres UANL": 1.55,
    "Guadalajara": 1.52,
    "Pumas UNAM": 0.92,
    "Tijuana": 0.85,
    "Atlas": 0.57,
    "Toluca": 0.35,
    "Pachuca": 0.27,
    "Queretaro": 0.17,
    "Atletico de San Luis": 0.17,
    "Puebla": 0.15,
    "Santos": 0.02,
}

# Fecha de captura del snapshot de la distribución de la comunidad.
CROWD_CAPTURED_AT = "2026-07-11"  # J1 Apertura 2026

CROWD_HIGH_THRESHOLD = 15.0  # >15% = pick muy popular (riesgo crowd)
CROWD_MED_THRESHOLD = 5.0  # 5-15% = riesgo medio

# Top 10 crowd picks pre-computado para respuesta rápida
top_crowd = dict(sorted(CROWD_DISTRIBUTION.items(), key=lambda x: x[1], reverse=True)[:10])

_CACHE: Dict[str, Any] = {"data": None, "ts": None}
_CACHE_TABLA: Dict[str, Any] = {"data": None, "ts": None}
_CACHE_RIESGO: Dict[str, Any] = {"data": None, "ts": None}
_CACHE_PLAN: Dict[str, Any] = {"data": None, "ts": None}
_TTL_MIN = 30
_TTL_RIESGO_MIN = 360  # el histórico cambia lento; análisis pesado => caché larga


def _fresco() -> bool:
    return (
        bool(_CACHE["data"])
        and bool(_CACHE["ts"])
        and (datetime.now(timezone.utc) - _CACHE["ts"] < timedelta(minutes=_TTL_MIN))
    )


def _obtener() -> Dict[str, Any]:
    if not _fresco():
        _CACHE["data"] = motor.generar_pronosticos()
        _CACHE["ts"] = datetime.now(timezone.utc)
    return cast(Dict[str, Any], _CACHE["data"])


def _obtener_tabla() -> Dict[str, Any]:
    fresco = (
        bool(_CACHE_TABLA["data"])
        and bool(_CACHE_TABLA["ts"])
        and (datetime.now(timezone.utc) - _CACHE_TABLA["ts"] < timedelta(minutes=_TTL_MIN))
    )
    if not fresco:
        _CACHE_TABLA["data"] = tabla_mod.obtener_tabla()
        _CACHE_TABLA["ts"] = datetime.now(timezone.utc)
    return cast(Dict[str, Any], _CACHE_TABLA["data"])


def _contexto_pick(pick: Dict[str, Any]) -> Dict[str, Any]:
    """
    Dossier compacto de la Liga MX API para un pick de Survivor. Deriva
    local/visitante desde `condicion` y consulta `ligamx_api.resumen_partido`.
    Tolerante: ante cualquier fallo devuelve {} (no rompe /jornada).
    """
    try:
        equipo = pick.get("equipo", "")
        rival = pick.get("rival", "")
        if pick.get("condicion") == "Local":
            home, away = equipo, rival
        else:
            home, away = rival, equipo
        dossier = lmx.resumen_partido(home, away)
        # Análisis de IA (Groq) sobre las noticias reales (opcional; igual que Telegram).
        try:
            from src import analista_ia as ia

            if ia.habilitado() and isinstance(dossier, dict):
                dossier["analisis_ia"] = ia.analizar_noticias(
                    [dossier.get("home", home), dossier.get("away", away)],
                    dossier.get("noticias", []),
                )
        except Exception:  # pragma: no cover - IA nunca debe tumbar la jornada
            logger.debug("Exception silenciada en _contexto_pick", exc_info=True)
        return cast(Dict[str, Any], dossier)
    except Exception:  # pragma: no cover - fallback defensivo de red
        return {}


def _usados_combinados(excluir: str) -> list:
    """
    Combina los equipos usados PERSISTIDOS (BD) con los que lleguen en el
    parámetro `excluir`. Así el pick/plan excluye automáticamente lo ya gastado,
    aunque no se pase nada. Tolerante: si la BD falla, usa solo el parámetro.
    """
    manual = [e.strip() for e in (excluir or "").split(",") if e.strip()]
    persistidos: list = []
    try:
        from src.database import get_equipos_usados

        persistidos = get_equipos_usados()
    except Exception:  # pragma: no cover - BD no disponible
        persistidos = []
    # Dedup preservando orden (persistidos primero).
    vistos, out = set(), []
    for e in persistidos + manual:
        k = e.strip().lower()
        if k and k not in vistos:
            vistos.add(k)
            out.append(e.strip())
    return out


def _partidos_jugados_torneo() -> Optional[int]:
    """Partidos jugados del torneo (para cautela de arranque). None si falla."""
    try:
        est = lmx.estado_temporada()
        fm = est.get("finished_matches")
        return int(fm) if fm is not None else None
    except Exception:  # pragma: no cover - API no disponible
        return None


def _enriquecer_con_crowd(pick: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Añade crowd_pct y crowd_risk al pick si existe el equipo en la distribución."""
    if not pick:
        return pick
    equipo = pick.get("equipo", "")
    crowd_pct = CROWD_DISTRIBUTION.get(equipo, 0.0)
    if crowd_pct >= CROWD_HIGH_THRESHOLD:
        risk = "ALTO"
    elif crowd_pct >= CROWD_MED_THRESHOLD:
        risk = "MEDIO"
    else:
        risk = "BAJO"
    return {**pick, "crowd_pct": round(crowd_pct, 2), "crowd_risk": risk}


def _enriquecer_lista_con_crowd(picks: list) -> list:
    """Enriquece una lista de picks con crowd data."""
    return [_enriquecer_con_crowd(p) for p in picks]


def _totales_jornada(pronosticos: list) -> Dict[str, Any]:
    """Calcula totales de la jornada: partidos, goles esperados, O/U, BTTS."""
    if not pronosticos:
        return {
            "partidos": 0,
            "goles_esperados_total": 0.0,
            "promedio_goles_partido": 0.0,
            "over_25_count": 0,
            "under_25_count": 0,
            "btts_si_count": 0,
            "btts_no_count": 0,
        }
    total_goles = sum(p.get("goles_esperados_local", 0) + p.get("goles_esperados_visitante", 0) for p in pronosticos)
    over_25 = sum(1 for p in pronosticos if p.get("pick_ou") == "Over")
    under_25 = sum(1 for p in pronosticos if p.get("pick_ou") == "Under")
    btts_si = sum(1 for p in pronosticos if p.get("pick_btts") == "Sí")
    btts_no = sum(1 for p in pronosticos if p.get("pick_btts") == "No")
    return {
        "partidos": len(pronosticos),
        "goles_esperados_total": round(total_goles, 1),
        "promedio_goles_partido": round(total_goles / len(pronosticos), 2),
        "over_25_count": over_25,
        "under_25_count": under_25,
        "btts_si_count": btts_si,
        "btts_no_count": btts_no,
    }


@router.get("/predicciones", summary="Predicciones reales (ESPN + Poisson)")
@limiter.limit("30/minute")
def predicciones(request: Request) -> Dict[str, Any]:
    """1X2 / Over-Under / BTTS / marcador por cada partido próximo."""
    return _obtener()


@router.get("/survivor", summary="Mejor pick de Survivor (no perder)")
@limiter.limit("20/minute")
def survivor(request: Request, excluir: str = "") -> Dict[str, Any]:
    """
    Mejor equipo para Survivor (mayor prob. de no perder). `excluir`: equipos
    ya usados, separados por coma (ej. ?excluir=America,Toluca).
    """
    data = _obtener()
    usados = _usados_combinados(excluir)
    est = motor.mejores_picks_estrategico(
        data.get("pronosticos", []),
        usados,
        partidos_jugados_torneo=_partidos_jugados_torneo(),
        n=1,
    )
    pick = _enriquecer_con_crowd(est["picks"][0]) if est.get("picks") else None
    # Top 3 picks crowd para contexto
    top_crowd = sorted(
        [{"equipo": k, "crowd_pct": v} for k, v in CROWD_DISTRIBUTION.items()],
        key=lambda x: -cast(float, x["crowd_pct"]),
    )[:3]
    return {
        "generado_utc": data.get("generado_utc"),
        "fuente_datos": data.get("fuente_datos"),
        "equipos_excluidos": usados,
        "pick_survivor": pick,
        "cautela": est.get("cautela"),
        "advertencia": est.get("advertencia"),
        "totales_jornada": _totales_jornada(data.get("pronosticos", [])),
        "crowd_intelligence": {
            "top_picks_crowd": top_crowd,
            "captured_at": CROWD_CAPTURED_AT,
            "recommendation": "EVITAR picks >15% crowd salvo confianza >85%",
        },
        "decision": data.get("decision"),
    }


@router.get("/jornada", summary="Vista de jornada: predicciones + pick + top-3 + motivación + momios")
@limiter.limit("20/minute")
def jornada(request: Request, excluir: str = "", contexto: bool = False) -> Dict[str, Any]:
    """
    Todo-en-uno para decidir la semana: predicciones, mejor pick de Survivor +
    top-3, motivación de la tabla y comparación vs mercado (si hay momios).

    `contexto=true` adjunta al pick #1 un dossier de la Liga MX API (predicción,
    forma, tarjetas/jugadores en riesgo, h2h). Es una llamada extra a la API
    externa (puede tardar si está dormida); por eso está apagado por defecto.
    """
    data = _obtener()
    pronos = data.get("pronosticos", [])
    comp = mercado_mod.comparar_pronosticos(pronos)  # momios gated (no-op sin key)
    pronos = comp.get("pronosticos", pronos)
    try:
        motivacion = motor.motivacion_por_equipo()
    except Exception:  # pragma: no cover - fallback defensivo de red
        motivacion = {}
    usados = _usados_combinados(excluir)
    est = motor.mejores_picks_estrategico(
        pronos,
        usados,
        motivacion,
        partidos_jugados_torneo=_partidos_jugados_torneo(),
        n=3,
    )
    top = est.get("picks", [])
    pick = _enriquecer_con_crowd(top[0]) if top else None
    top = _enriquecer_lista_con_crowd(top)
    if contexto and pick:
        pick = {**pick, "contexto_api": _contexto_pick(pick)}
    return {
        "generado_utc": data.get("generado_utc"),
        "fuente_datos": data.get("fuente_datos"),
        "equipos_excluidos": usados,
        "pick_survivor": pick,
        "top_picks": top,
        "cautela": est.get("cautela"),
        "advertencia": est.get("advertencia"),
        "mercado_habilitado": comp.get("mercado_habilitado", False),
        "partidos_con_momios": comp.get("partidos_con_momios", 0),
        "pronosticos": pronos,
        "totales_jornada": _totales_jornada(pronos),
        "crowd_intelligence": {
            "top_picks_crowd": top_crowd,
            "captured_at": CROWD_CAPTURED_AT,
            "recommendation": "EVITAR picks >15% crowd salvo confianza >85%",
        },
        "decision": data.get("decision"),
    }


@router.get("/tabla", summary="Tabla Liga MX (ESPN) + motivación por equipo")
@limiter.limit("20/minute")
def tabla(request: Request) -> Dict[str, Any]:
    """Tabla general con zona de clasificación y motivación por equipo."""
    try:
        data = _obtener_tabla()
    except Exception as exc:  # pragma: no cover - fallback defensivo de red
        return {"torneo": "", "tabla": [], "error": str(exc), "decision": "INFORMATIVO / REVISIÓN HUMANA"}
    return {**data, "decision": "INFORMATIVO / REVISIÓN HUMANA"}


@router.get("/valor", summary="Predicciones + comparación vs mercado (opcional)")
@limiter.limit("20/minute")
def valor(request: Request) -> Dict[str, Any]:
    """
    Predicciones del modelo anotadas con comparación vs mercado (dónde el modelo
    ve 'valor'). SOLO activa si hay key de momios configurada (ODDS_API_IO_KEY);
    si no, devuelve las predicciones sin comparación (mercado_habilitado=False).
    Informativo: el modelo es la fuente de verdad; no es consejo de apuesta.
    """
    data = _obtener()
    comp = mercado_mod.comparar_pronosticos(data.get("pronosticos", []))
    return {
        "generado_utc": data.get("generado_utc"),
        "fuente_datos": data.get("fuente_datos"),
        **comp,
    }


@router.get("/valor/diagnostico", summary="Diagnóstico de la conexión a momios (debug)")
@limiter.limit("10/minute")
def valor_diagnostico(request: Request) -> Dict[str, Any]:
    """Muestra qué devuelve odds-api.io (eventos/casas/mercados) sin exponer la key."""
    return cast(Dict[str, Any], mercado_mod.diagnostico_mercado())


@router.get("/health/fuentes", summary="Salud de las fuentes de datos (ESPN/TheSportsDB/odds)")
@limiter.limit("10/minute")
def health_fuentes(request: Request) -> Dict[str, Any]:
    """Ping a cada fuente para detectar caídas antes de la jornada."""
    return cast(Dict[str, Any], fuentes_mod.estado_fuentes())


@router.get("/analisis/riesgo", summary="¿Cuándo falla el favorito? (análisis de upsets, datos reales)")
@limiter.limit("10/minute")
def analisis_riesgo(request: Request) -> Dict[str, Any]:
    """
    Mide, sobre el histórico real (walk-forward), cuándo y por qué falla el
    favorito del modelo: por condición (local vs visitante), nivel de confianza
    y partidos cerrados ('under'). Útil para no quemar el Survivor con un
    favorito engañoso. Análisis pesado => caché de 6 horas.
    """
    fresco = (
        bool(_CACHE_RIESGO["data"])
        and bool(_CACHE_RIESGO["ts"])
        and (datetime.now(timezone.utc) - _CACHE_RIESGO["ts"] < timedelta(minutes=_TTL_RIESGO_MIN))
    )
    if not fresco:
        try:
            datos = fuentes_mod.obtener_resultados(meses=18)
            _CACHE_RIESGO["data"] = riesgo_mod.analizar_riesgo_favoritos(datos["resultados"])
            _CACHE_RIESGO["data"]["fuente_datos"] = datos.get("fuente")
        except Exception as exc:  # pragma: no cover - fallback defensivo de red
            return {"partidos_evaluados": 0, "error": str(exc), "decision": "INFORMATIVO / REVISIÓN HUMANA"}
        _CACHE_RIESGO["ts"] = datetime.now(timezone.utc)
    return cast(Dict[str, Any], _CACHE_RIESGO["data"])


@router.get("/plan-survivor", summary="Estrategia de temporada: qué equipo usar en cada jornada")
@limiter.limit("10/minute")
def plan_survivor(
    request: Request,
    excluir: str = "",
    peso_victoria: float = 0.5,
    usar_momios: bool = True,
    vida_empate_consumida: bool = False,
) -> Dict[str, Any]:
    """
    Plan ÓPTIMO de Survivor para toda la temporada (PlayDoit): asigna 1 equipo por
    jornada, sin repetir, maximizando supervivencia (no perder) y victorias.

    Requiere `data/calendario.json` con el calendario completo de las 17 jornadas
    (se publica cerca del arranque). Sin él, responde `calendario_incompleto`.
    `excluir`: equipos ya gastados (coma). `peso_victoria`: 0 = solo sobrevivir.
    `usar_momios`: mezcla momios reales (odds-api.io) si hay key y cobertura.
    Análisis pesado => caché de 6 horas (con filtros por defecto).
    """
    usados = _usados_combinados(excluir)
    usar_cache = not usados and abs(peso_victoria - 0.5) < 1e-9 and usar_momios
    if usar_cache:
        fresco = (
            bool(_CACHE_PLAN["data"])
            and bool(_CACHE_PLAN["ts"])
            and (datetime.now(timezone.utc) - _CACHE_PLAN["ts"] < timedelta(minutes=_TTL_RIESGO_MIN))
        )
        if fresco:
            return cast(Dict[str, Any], _CACHE_PLAN["data"])

    calendario = plan_mod.cargar_calendario()
    if not calendario:
        return {
            "plan": [],
            "calendario_incompleto": True,
            "mensaje": "Falta data/calendario.json con las 17 jornadas. El calendario "
            "del Apertura 2026 se publica cerca del 17-jul; guárdalo y reintenta.",
            "decision": "INFORMATIVO / REVISIÓN HUMANA",
        }
    try:
        datos = fuentes_mod.obtener_resultados(meses=18)
        fuerzas = pm.calcular_fuerzas(datos["resultados"])
        odds = plan_mod.construir_odds_por_partido(calendario) if usar_momios else None
        calibracion = plan_mod.preparar_calibracion_segura(datos["resultados"])
        resultado = plan_mod.planificar(
            calendario,
            fuerzas,
            equipos_usados=usados,
            peso_victoria=peso_victoria,
            odds_por_partido=odds,
            vida_empate_consumida=vida_empate_consumida,
            calibracion=calibracion.get("parametros_planificador"),
        )
        resultado["fuente_datos"] = datos.get("fuente")
        resultado["momios_integrados"] = len(odds) if odds else 0
        resultado["calibracion"] = {
            "aplicada": bool(calibracion.get("aplicada")),
            "alpha": float(calibracion.get("alpha") or 0.0),
            "base": calibracion.get("base"),
            "criterio": calibracion.get("criterio"),
            "fallback": calibracion.get("fallback"),
            "motivo": calibracion.get("motivo"),
        }
    except Exception as exc:  # pragma: no cover - fallback defensivo
        return {"plan": [], "error": str(exc), "decision": "INFORMATIVO / REVISIÓN HUMANA"}
    if usar_cache:
        _CACHE_PLAN["data"] = resultado
        _CACHE_PLAN["ts"] = datetime.now(timezone.utc)
    return cast(Dict[str, Any], resultado)


@router.get("/analisis-partido", summary="Dossier de un partido (Liga MX API): predicción + forma + tarjetas + h2h")
@limiter.limit("20/minute")
def analisis_partido(request: Request, home: str, away: str, prediccion: bool = True) -> Dict[str, Any]:
    """
    Dossier enriquecido de un partido usando la Liga MX API (proyecto hermano):
    predictor de la API, forma reciente, disciplina/tarjetas (jugadores en riesgo
    de suspensión), rachas y resumen head-to-head. Por NOMBRE de equipo
    (ej. ?home=America&away=Toluca).

    Tolerante: cada señal que la API aún no tenga (pretemporada) llega en null.
    Informativo; el modelo local (ESPN + Poisson) sigue siendo la fuente de verdad
    del pick.
    """
    if not home or not away:
        return {"error": "Faltan parámetros 'home' y 'away'.", "decision": "INFORMATIVO / REVISIÓN HUMANA"}
    try:
        return cast(Dict[str, Any], lmx.analisis_partido(home, away, incluir_prediccion=prediccion))
    except Exception as exc:  # pragma: no cover - fallback defensivo de red
        return {"home": home, "away": away, "error": str(exc), "decision": "INFORMATIVO / REVISIÓN HUMANA"}


@router.get("/jugadores-riesgo", summary="Jugadores en riesgo de suspensión (Liga MX API)")
@limiter.limit("20/minute")
def jugadores_riesgo(request: Request, limit: int = 20) -> Dict[str, Any]:
    """
    Jugadores de toda la liga en riesgo de suspensión por acumulación de tarjetas
    (vía Liga MX API /players/discipline). Contexto de riesgo para el pick.
    En pretemporada viene vacío. Informativo.
    """
    try:
        return {**lmx.jugadores_en_riesgo_liga(limit=limit), "decision": "INFORMATIVO / REVISIÓN HUMANA"}
    except Exception as exc:  # pragma: no cover - fallback defensivo de red
        return {"count": 0, "jugadores": [], "error": str(exc), "decision": "INFORMATIVO / REVISIÓN HUMANA"}


@router.get("/noticias", summary="Noticias Liga MX (fichajes/lesiones/bajas) vía Liga MX API")
@limiter.limit("20/minute")
def noticias(request: Request, limit: int = 10) -> Dict[str, Any]:
    """
    Noticias recientes de Liga MX (365Scores + Google News) tomadas de la Liga MX
    API: fichajes, lesiones, bajas y boletines. Compacto (título, fuente, fecha,
    link). Informativo; útil como contexto de riesgo para el pick.
    """
    try:
        items = lmx.noticias_recientes(limit=limit)
        return {"total": len(items), "noticias": items, "decision": "INFORMATIVO / REVISIÓN HUMANA"}
    except Exception as exc:  # pragma: no cover - fallback defensivo de red
        return {"total": 0, "noticias": [], "error": str(exc), "decision": "INFORMATIVO / REVISIÓN HUMANA"}


@router.get("/alineacion", summary="Alineación confirmada de un partido (365Scores, ~1h antes)")
@limiter.limit("20/minute")
def alineacion(request: Request, home: str, away: str) -> Dict[str, Any]:
    """
    Alineación confirmada de un partido por nombre (ej. ?home=America&away=Toluca).
    365Scores publica el XI ~1h antes del inicio; antes de eso `disponible=false`.
    Sirve para detectar si un favorito sale con SUPLENTES antes de hacer el pick.
    Informativo.
    """
    if not home or not away:
        return {"disponible": False, "error": "Faltan 'home' y 'away'.", "decision": "INFORMATIVO / REVISIÓN HUMANA"}
    try:
        return {**lmx.alineacion_de_partido(home, away), "decision": "INFORMATIVO / REVISIÓN HUMANA"}
    except Exception as exc:  # pragma: no cover - fallback defensivo de red
        return {"disponible": False, "equipos": [], "error": str(exc), "decision": "INFORMATIVO / REVISIÓN HUMANA"}


@router.get("/historial/pronosticos", summary="Track-record de pronósticos (marcador + aciertos)")
@limiter.limit("20/minute")
def historial_pronosticos(request: Request, limit: int = 50, solo_resueltos: bool = False) -> Dict[str, Any]:
    """
    Historial de pronósticos con marcador predicho vs real y si acertó (1X2 y
    marcador exacto). Se llena solo (cada envío guarda; el cron diario resuelve).
    """
    try:
        from src.database import historial_pronosticos as _hist

        filas = _hist(limit=limit, solo_resueltos=solo_resueltos)
        return {"total": len(filas), "pronosticos": filas, "decision": "INFORMATIVO / REVISIÓN HUMANA"}
    except Exception as exc:  # pragma: no cover - fallback defensivo
        return {"total": 0, "pronosticos": [], "error": str(exc), "decision": "INFORMATIVO / REVISIÓN HUMANA"}


@router.get("/historial/rentabilidad", summary="Rentabilidad/precisión del modelo (aciertos)")
@limiter.limit("20/minute")
def historial_rentabilidad(request: Request) -> Dict[str, Any]:
    """% de aciertos 1X2 y de marcador exacto sobre los pronósticos ya resueltos."""
    try:
        from src.database import rentabilidad_pronosticos as _rent

        return {**_rent(), "decision": "INFORMATIVO / REVISIÓN HUMANA"}
    except Exception as exc:  # pragma: no cover - fallback defensivo
        return {"resueltos": 0, "error": str(exc), "decision": "INFORMATIVO / REVISIÓN HUMANA"}


@router.get("/analisis-ia", summary="Análisis de riesgo por IA (Groq) sobre noticias reales")
@limiter.limit("10/minute")
def analisis_ia(request: Request, home: str, away: str) -> Dict[str, Any]:
    """
    Usa IA (Groq) para EXTRAER señales de riesgo (lesión/suspensión/duda/rotación)
    de las noticias reales de ambos equipos, citando el titular fuente. Opcional:
    requiere GROQ_API_KEY; sin ella responde `disponible: false`. No inventa datos.
    """
    if not home or not away:
        return {"disponible": False, "error": "Faltan 'home' y 'away'.", "decision": "INFORMATIVO / REVISIÓN HUMANA"}
    try:
        from src import analista_ia as ia

        return {**ia.analizar_partido(home, away), "decision": "INFORMATIVO / REVISIÓN HUMANA"}
    except Exception as exc:  # pragma: no cover - fallback defensivo
        return {"disponible": False, "error": str(exc), "decision": "INFORMATIVO / REVISIÓN HUMANA"}
