#!/usr/bin/env python3
"""
suspensiones.py — Bajas por SANCIÓN (tarjetas) como señal de ajuste del pronóstico.

Por qué existe: el ajuste por alineaciones (`ajuste_pronostico`) solo despierta
cuando 365Scores publica el XI, y eso pasa ~1 hora antes del partido. Durante
toda la semana previa —justo cuando hay que elegir el pick del Survivor— el
motor va ciego, aunque el dato de quién NO puede jugar ya exista y sea firme.

Una suspensión no es un rumor ni una probabilidad: es una baja segura, conocida
desde el silbatazo final de la jornada anterior. La Liga MX API ya la publica en
`/players/discipline?unavailable=true` (roja directa o ciclo de amarillas), pero
nadie la estaba leyendo desde el motor.

Este módulo la traduce al MISMO contrato que ya consume `ajuste_pronostico`:

    {"disponible": bool,
     "fuente": "suspensiones",
     "equipos": {equipo: {"fuerza_xi_pct": float, "ausentes_clave": [...]}}}

Así no hay que inventar una segunda vía de ajuste: se reusa la existente, con su
tope de 15% y su idempotencia.

Calibración (deliberadamente conservadora): cada sancionado descuenta 4 puntos
de `fuerza_xi_pct`, con un piso de 88% (tope de 12 puntos de déficit). Con
K_LINEUP=0.6, dos sancionados recortan ~4.8% de los goles esperados del equipo.
Es un empujón, no un vuelco: no distingue titular de suplente, así que no debe
pesar como un XI real observado.

Emparejamiento de equipos: se usa `canonical_team_key` (igualdad exacta de
clave), NO `teams_match`. `teams_match` compara por contención de substrings y
eso produce falsos positivos brutales con nombres cortos: "A" casa con
"Monterrey" porque la letra `a` está dentro. Colgarle a un equipo la roja de
otro es peor que no ajustar nada.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

try:
    import ligamx_api as lmx
except ImportError:  # pragma: no cover - ruta alterna de import
    from src import ligamx_api as lmx  # type: ignore

try:
    from team_normalizer import DISPLAY, canonical_team_key, display_team_name
except ImportError:  # pragma: no cover - ruta alterna de import
    from src.team_normalizer import DISPLAY, canonical_team_key, display_team_name  # type: ignore

logger = logging.getLogger(__name__)

# Cada baja por sanción descuenta esto de la fuerza del XI...
PESO_SUSPENDIDO_PCT = 4.0
# ...pero el déficit total nunca pasa de aquí (piso de fuerza: 88%).
MAX_DEFICIT_PCT = 12.0
LIMITE_CONSULTA = 50


def suspendidos_liga(limit: int = LIMITE_CONSULTA, season: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Jugadores INHABILITADOS para la próxima jornada (no los que están en riesgo).

    OJO con el `limit`: el endpoint reporta `count` sobre el total filtrado pero
    trunca `players` al límite, y su default es 20. Se pide 50 explícitamente
    para no perder bajas (y de paso se esquiva la caché de la respuesta sin
    parámetros, que ya dio un falso negativo antes).
    """
    params: Dict[str, Any] = {"unavailable": "true", "limit": max(1, min(int(limit), 100))}
    if season:
        params["season"] = season
    data = lmx._get("/players/discipline", params)
    players = data.get("players") if isinstance(data, dict) else None
    return [p for p in (players or []) if isinstance(p, dict) and p.get("suspended_next_match")]


def suspendidos_por_equipo(
    limit: int = LIMITE_CONSULTA, season: Optional[str] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Mapa {equipo_display: [{nombre, motivo}]}. Tolerante: {} si la API falla o
    duerme (Render free). NUNCA rompe el pipeline de pronósticos.
    """
    try:
        filas = suspendidos_liga(limit=limit, season=season)
    except Exception:
        logger.debug("No se pudo leer /players/discipline", exc_info=True)
        return {}
    mapa: Dict[str, List[Dict[str, Any]]] = {}
    for p in filas:
        equipo = p.get("team")
        nombre = p.get("player")
        if not equipo or not nombre:
            continue
        clave = display_team_name(str(equipo))
        mapa.setdefault(clave, []).append(
            {"nombre": str(nombre), "motivo": str(p.get("suspension_reason") or "sancion")}
        )
    return mapa


def deficit_por_bajas(n_bajas: int) -> float:
    """Puntos de fuerza que se le restan al XI por N sancionados (con tope)."""
    try:
        n = max(0, int(n_bajas))
    except (TypeError, ValueError):
        return 0.0
    return min(n * PESO_SUSPENDIDO_PCT, MAX_DEFICIT_PCT)


def _clave(nombre: str) -> str:
    """Clave canónica de un equipo ('Chivas' y 'CD Guadalajara' -> 'guadalajara')."""
    return canonical_team_key(str(nombre or ""))


def es_equipo_conocido(nombre: str) -> bool:
    """
    True solo si el nombre resuelve a un club de Liga MX del catálogo.

    Sirve de cortafuegos: con equipos inventados (tests, datos sucios) no vale
    la pena pegarle a la API ni arriesgar un emparejamiento equivocado.
    """
    return _clave(nombre) in DISPLAY


def _bajas_de(mapa: Optional[Dict[str, List[Dict[str, Any]]]], nombre: str) -> List[Dict[str, Any]]:
    """Bajas de un equipo por igualdad exacta de clave canónica (alias incluidos)."""
    clave = _clave(nombre)
    if not clave:
        return []
    for k, v in (mapa or {}).items():
        if _clave(str(k)) == clave:
            return list(v or [])
    return []


def impacto_por_suspensiones(
    home: str,
    away: str,
    mapa: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """
    Impacto de las sanciones de un partido, en el contrato de `lineup-impact`.

    `mapa` permite bajar la lista UNA vez y reusarla en los 9 partidos de la
    jornada en vez de pegarle a la API por partido.
    """
    vacio: Dict[str, Any] = {"disponible": False, "fuente": "suspensiones", "equipos": {}}
    conocidos = [n for n in (home, away) if es_equipo_conocido(n)]
    if not conocidos:
        return vacio
    datos = mapa if mapa is not None else suspendidos_por_equipo()
    equipos: Dict[str, Any] = {}
    for nombre in conocidos:
        bajas = _bajas_de(datos, nombre)
        if not bajas:
            continue
        deficit = deficit_por_bajas(len(bajas))
        equipos[display_team_name(str(nombre))] = {
            "fuerza_xi_pct": round(100.0 - deficit, 1),
            "ausentes_clave": [b.get("nombre", "") for b in bajas if b.get("nombre")],
            "motivo": "suspension",
        }
    return {"disponible": bool(equipos), "fuente": "suspensiones", "equipos": equipos}
