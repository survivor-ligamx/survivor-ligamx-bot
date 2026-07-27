#!/usr/bin/env python3
"""
analista_resultados.py — Análisis POST-PARTIDO de la jornada actual.

Qué hace:
- Obtiene los partidos YA JUGADOS de la jornada actual.
- Para cada partido: goles, tarjetas, alineaciones, eventos, impacto del XI.
- Genera conclusiones factuales y deterministas, sin narración libre.
- Compara picks anteriores del bot con el resultado real.
- Devuelve un mensaje HTML listo para Telegram.

Fuentes:
- ESPN (scoreboard) para marcadores y estado.
- Liga MX API para detalles (eventos, tarjetas, alineaciones).

Activación: automática desde Telegram (/analisis) o endpoint API.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, cast
import logging
import re

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

try:
    from team_normalizer import canonical_team_key, display_team_name
except ImportError:  # pragma: no cover
    from src.team_normalizer import canonical_team_key, display_team_name  # type: ignore

from src import ligamx_api as lmx

from src import analista_ia as ia

_DECISION = "INFORMATIVO / REVISIÓN HUMANA"

# Umbral para considerar que un partido ya jugó (horas desde el inicio esperado).
_HORAS_POST_PARTIDO = 2.5


def _parse_dt(iso: Any) -> Optional[datetime]:
    if not iso:
        return None
    s = str(iso).replace("Z", "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _ya_jugado(fecha_iso: str, estado: str, horas_post: float = _HORAS_POST_PARTIDO) -> bool:
    """True si el partido ya finalizó o ya pasó su horario por `horas_post`."""
    if estado == "STATUS_FULL_TIME":
        return True
    dt = _parse_dt(fecha_iso)
    if dt is None:
        return False
    ahora = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (ahora - dt).total_seconds() / 3600.0 >= horas_post


def obtener_partidos_jornada(fecha: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Obtiene los partidos YA JUGADOS de la jornada actual.
    Primero intenta con ESPN scoreboard (rango +/- 2 días).
    Si no hay suficientes, completa con Liga MX API (partidos finalizados).
    """
    partidos_espn = _obtener_partidos_espn(fecha)
    partidos_lmx = _obtener_partidos_ligamx(fecha) if len(partidos_espn) < 3 else []
    # Combinar y deduplicar
    vistos: set = set()
    combinados: List[Dict[str, Any]] = []
    for p in partidos_espn + partidos_lmx:
        clave = (p["home_team"], p["away_team"], p["fecha"])
        if clave in vistos:
            continue
        vistos.add(clave)
        combinados.append(p)
    combinados.sort(key=lambda x: x.get("fecha", ""))
    return combinados


def _obtener_detalles_fuera(home: str, away: str, fecha: str, hg: int = 0, ag: int = 0) -> Dict[str, Any]:
    """Obtiene detalles usando el scraper fuerte."""
    try:
        from src import scraper_resultados as sr

        return cast(Dict[str, Any], sr.analizar_partido_fuerte(home, away, hg, ag, fecha))
    except Exception:
        return {}


def _extraer_eventos_espn(ev: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extrae eventos detallados (goles, tarjetas, cambios, penales) del response de ESPN."""
    eventos: List[Dict[str, Any]] = []
    comps = ev.get("competitions") or [{}]
    comp = comps[0] if comps else {}
    for e in comp.get("events") or []:
        if not isinstance(e, dict):
            continue
        tipo_raw = (e.get("type") or {}).get("text") or (e.get("type") or {}).get("name") or ""
        tipo = str(tipo_raw).lower()
        minuto = (e.get("clock") or {}).get("displayValue", "") or ""
        equipo = ""
        team_data = e.get("team")
        if isinstance(team_data, dict):
            equipo = team_data.get("displayName", "") or team_data.get("name", "")
        jugador = ""
        athletes = e.get("athletesInvolved") or []
        if athletes and isinstance(athletes[0], dict):
            jugador = athletes[0].get("displayName", "") or athletes[0].get("name", "")
        detalle = e.get("text", "") or ""

        if "goal" in tipo:
            eventos.append({"type": "goal", "minute": minuto, "team": equipo, "player": jugador, "detail": detalle})
        elif "yellow" in tipo:
            eventos.append(
                {"type": "yellow_card", "minute": minuto, "team": equipo, "player": jugador, "detail": detalle}
            )
        elif "red" in tipo:
            eventos.append({"type": "red_card", "minute": minuto, "team": equipo, "player": jugador, "detail": detalle})
        elif "substitution" in tipo or "sub" in tipo:
            sale = ""
            entra = ""
            if len(athletes) >= 1 and isinstance(athletes[0], dict):
                sale = athletes[0].get("displayName", "") or athletes[0].get("name", "")
            if len(athletes) >= 2 and isinstance(athletes[1], dict):
                entra = athletes[1].get("displayName", "") or athletes[1].get("name", "")
            eventos.append(
                {
                    "type": "substitution",
                    "minute": minuto,
                    "team": equipo,
                    "player": sale,
                    "playerIn": entra,
                    "playerOut": sale,
                    "detail": detalle,
                }
            )
        elif "penalty" in tipo:
            eventos.append({"type": "penalty", "minute": minuto, "team": equipo, "player": jugador, "detail": detalle})
    return eventos


def _obtener_partidos_espn(fecha: Optional[str] = None) -> List[Dict[str, Any]]:
    """Obtiene partidos jugados desde ESPN scoreboard."""
    if requests is None:
        return []
    hoy = datetime.now(timezone.utc)
    fecha_base = fecha or hoy.strftime("%Y%m%d")
    try:
        dt_base = datetime.strptime(fecha_base, "%Y%m%d")
    except ValueError:
        dt_base = hoy

    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/mex.1/scoreboard"
    partidos_vistos: set = set()
    partidos: List[Dict[str, Any]] = []

    for delta in range(-2, 3):
        rango_fecha = (dt_base + timedelta(days=delta)).strftime("%Y%m%d")
        try:
            resp = requests.get(url, params={"dates": rango_fecha}, timeout=20)
            if resp.status_code != 200:
                continue
            data = resp.json()
        except Exception:
            continue

        for ev in data.get("events", []):
            if not isinstance(ev, dict):
                continue
            comps = ev.get("competitions") or [{}]
            comp = comps[0] if comps else {}
            competidores = comp.get("competitors", [])
            home = away = None
            hg = ag = None
            for c in competidores:
                if not isinstance(c, dict):
                    continue
                nombre = (c.get("team") or {}).get("displayName", "")
                score = c.get("score")
                if c.get("homeAway") == "home":
                    home, hg = nombre, score
                elif c.get("homeAway") == "away":
                    away, ag = nombre, score
            if not home or not away:
                continue
            estado = ((ev.get("status") or {}).get("type") or {}).get("name", "")
            fecha_iso = str(ev.get("date", ""))
            if not _ya_jugado(fecha_iso, estado):
                continue
            try:
                home_goals = int(hg) if hg is not None else None
                away_goals = int(ag) if ag is not None else None
            except (TypeError, ValueError):
                continue
            clave = (display_team_name(home), display_team_name(away), fecha_iso[:10])
            if clave in partidos_vistos:
                continue
            partidos_vistos.add(clave)
            eventos_espn = _extraer_eventos_espn(ev)
            partidos.append(
                {
                    "fecha": fecha_iso[:10],
                    "home_team": display_team_name(home),
                    "away_team": display_team_name(away),
                    "home_goals": home_goals,
                    "away_goals": away_goals,
                    "estado": estado,
                    "event_id": ev.get("id"),
                    "eventos_espn": eventos_espn,
                }
            )
    return partidos


def _obtener_partidos_ligamx(fecha: Optional[str] = None) -> List[Dict[str, Any]]:
    """Obtiene partidos finalizados desde Liga MX API."""
    try:
        partidos_crudos = lmx.obtener_partidos(status="finished", limit=50)
    except Exception:
        return []
    partidos: List[Dict[str, Any]] = []
    vistos: set = set()
    for m in partidos_crudos:
        if not isinstance(m, dict):
            continue
        home = (m.get("home_team") or {}).get("name", "")
        away = (m.get("away_team") or {}).get("name", "")
        hg, ag = m.get("home_score"), m.get("away_score")
        fecha_m = str(m.get("match_date") or "")[:10]
        if not home or not away or hg is None or ag is None:
            continue
        try:
            hg, ag = int(hg), int(ag)
        except (TypeError, ValueError):
            continue
        clave = (display_team_name(home), display_team_name(away), fecha_m)
        if clave in vistos:
            continue
        vistos.add(clave)
        partidos.append(
            {
                "fecha": fecha_m,
                "home_team": display_team_name(home),
                "away_team": display_team_name(away),
                "home_goals": hg,
                "away_goals": ag,
                "estado": "STATUS_FULL_TIME",
                "event_id": m.get("id"),
            }
        )
    return partidos


def _buscar_eventos_partido(home: str, away: str, fecha: str) -> List[Dict[str, Any]]:
    """Busca eventos detallados del partido en múltiples fuentes web."""
    eventos: List[Dict[str, Any]] = []

    # Fuente 1: ESPN resumen
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/soccer/mex.1/scoreboard",
            params={"dates": fecha.replace("-", "")},
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code == 200:
            data = resp.json()
            for ev in data.get("events", []):
                if not isinstance(ev, dict):
                    continue
                comps = ev.get("competitions") or [{}]
                comp = comps[0] if comps else {}
                competitors = comp.get("competitors", [])
                h_name = a_name = ""
                for c in competitors:
                    if not isinstance(c, dict):
                        continue
                    team_name = (c.get("team") or {}).get("displayName", "")
                    if c.get("homeAway") == "home":
                        h_name = team_name
                    elif c.get("homeAway") == "away":
                        a_name = team_name
                if home.lower() in h_name.lower() and away.lower() in a_name.lower():
                    for e in comp.get("events") or []:
                        if not isinstance(e, dict):
                            continue
                        tipo = ((e.get("type") or {}).get("text") or "").lower()
                        minuto = (e.get("clock") or {}).get("displayValue", "") or ""
                        equipo = ""
                        team_data = e.get("team")
                        if isinstance(team_data, dict):
                            equipo = team_data.get("displayName", "")
                        athletes = e.get("athletesInvolved") or []
                        jugador = ""
                        if athletes and isinstance(athletes[0], dict):
                            jugador = athletes[0].get("displayName", "")
                        detalle = e.get("text", "") or ""
                        if "goal" in tipo or "gol" in tipo:
                            eventos.append(
                                {"type": "goal", "minute": minuto, "team": equipo, "player": jugador, "detail": detalle}
                            )
                        elif "yellow" in tipo or "tarjeta amarilla" in tipo:
                            eventos.append(
                                {
                                    "type": "yellow_card",
                                    "minute": minuto,
                                    "team": equipo,
                                    "player": jugador,
                                    "detail": detalle,
                                }
                            )
                        elif "red" in tipo or "tarjeta roja" in tipo:
                            eventos.append(
                                {
                                    "type": "red_card",
                                    "minute": minuto,
                                    "team": equipo,
                                    "player": jugador,
                                    "detail": detalle,
                                }
                            )
                        elif "substitution" in tipo or "cambio" in tipo:
                            eventos.append(
                                {
                                    "type": "substitution",
                                    "minute": minuto,
                                    "team": equipo,
                                    "player": jugador,
                                    "detail": detalle,
                                }
                            )
    except Exception:
        logger.debug("Exception silenciada en _buscar_eventos_partido", exc_info=True)

    # Fuente 2: buscar en web
    if len(eventos) < 2:
        consultas = [
            f"{home} vs {away} {fecha} goles tarjetas resumen",
            f"{home} {away} Liga MX {fecha} resultado completo",
        ]
        for q in consultas[:2]:
            resultados = ia._buscar_web(q, max_results=4)
            for r in resultados:
                titulo = r.get("title", "")
                snippet = r.get("snippet", "")
                texto = f"{titulo} {snippet}".lower()
                import re

                goles = re.findall(r"(\d+)\s*[-:]\s*(\d+)", texto)
                if goles:
                    eventos.append(
                        {
                            "type": "goal_search",
                            "team": home if home.lower() in texto else away if away.lower() in texto else "",
                            "player": "",
                            "minute": "",
                            "detail": f"Resultado según búsqueda: {goles[0][0]}-{goles[0][1]}",
                            "source": r.get("url", ""),
                        }
                    )
                if "expuls" in texto or "roja" in texto or "red card" in texto:
                    eventos.append(
                        {
                            "type": "card_search",
                            "team": home if home.lower() in texto else away if away.lower() in texto else "",
                            "player": "",
                            "minute": "",
                            "detail": "Expulsión reportada",
                            "source": r.get("url", ""),
                        }
                    )

    return eventos[:15]


_CACHE_EVENTOS_365: Dict[str, int] = {}
_CACHE_DETALLES: Dict[str, Any] = {}


def _cache_key_365(home: str, away: str) -> str:
    return f"{canonical_team_key(home)}:{canonical_team_key(away)}"


def obtener_detalle_partido(home: str, away: str, event_id: Optional[str] = None, fecha: str = "") -> Dict[str, Any]:
    """
    Obtiene detalle completo de un partido ya jugado.
    Usa cache para no repetir consultas.
    """
    key = _cache_key_365(home, away)
    if key in _CACHE_DETALLES:
        return cast(Dict[str, Any], _CACHE_DETALLES[key])

    out: Dict[str, Any] = {
        "home": home,
        "away": away,
        "eventos": [],
        "alineacion": None,
        "impacto_xi": None,
        "noticias": [],
    }
    # Intentar obtener eventos desde 365scores primero (con cache)
    eid = _CACHE_EVENTOS_365.get(key)
    if eid is None:
        try:
            eid = lmx.evento_365_id(home, away)
            if eid:
                _CACHE_EVENTOS_365[key] = eid
        except Exception:
            eid = None
    if eid:
        try:
            eventos_365 = lmx.eventos_365_partido(eid)
            if eventos_365:
                out["eventos"] = eventos_365
        except Exception:
            logger.debug("Exception silenciada en obtener_detalle_partido", exc_info=True)
    # Si 365scores no tiene eventos, buscar en ESPN/liga MX (solo si hay pocos partidos)
    if not out["eventos"]:
        try:
            mid = lmx.match_id_de_partido(home, away)
            if mid:
                try:
                    eventos_lmx = lmx.eventos_partido(mid)
                    if eventos_lmx:
                        out["eventos"] = eventos_lmx
                except Exception:
                    logger.debug("Exception silenciada en obtener_detalle_partido", exc_info=True)
                try:
                    out["alineacion"] = lmx.alineacion_de_partido(home, away)
                except Exception:
                    logger.debug("Exception silenciada en obtener_detalle_partido", exc_info=True)
                try:
                    out["impacto_xi"] = lmx.lineup_impact_partido(home, away)
                except Exception:
                    logger.debug("Exception silenciada en obtener_detalle_partido", exc_info=True)
        except Exception:
            logger.debug("Exception silenciada en obtener_detalle_partido", exc_info=True)
    # Si no hay eventos en absoluto, buscar en web (solo para partidos recientes)
    if not out["eventos"] and fecha:
        try:
            from datetime import datetime as _dt

            fecha_dt = _dt.strptime(fecha, "%Y-%m-%d")
            ahora = _dt.now()
            if (ahora - fecha_dt).days <= 7:
                out["eventos"] = _buscar_eventos_partido(home, away, fecha)
        except Exception:
            logger.debug("Exception silenciada en obtener_detalle_partido", exc_info=True)
    # Noticias
    try:
        out["noticias"] = lmx.noticias_de_equipos([home, away], limit=3, dias=7)
    except Exception:
        logger.debug("Exception silenciada en obtener_detalle_partido", exc_info=True)
    _CACHE_DETALLES[key] = out
    return out


def _formatear_eventos(eventos: List[Dict[str, Any]]) -> List[str]:
    """Convierte eventos a líneas legibles, ordenados por minuto."""
    # Primero filtrar y formatear
    items: List[tuple[int, str]] = []  # (minuto_sort, linea)
    for e in (eventos or [])[:30]:
        if not isinstance(e, dict):
            continue
        tipo_raw = str(e.get("type", "") or e.get("category", "") or "").lower()
        minuto = str(e.get("minute", "") or e.get("time", "") or e.get("clock", "") or "")
        equipo = str(e.get("team", "") or e.get("team_name", "") or e.get("home_team", "") or "")
        jugador = str(e.get("player", "") or e.get("playerName", "") or e.get("athlete", "") or e.get("name", "") or "")
        detalle = str(e.get("detail", "") or e.get("description", "") or e.get("text", "") or "")
        # Ignorar eventos basura de búsqueda web
        if any(
            k in tipo_raw
            for k in ["search", "goal_search", "card_search", "injury_search", "substitution_search", "penalty_search"]
        ):
            continue
        if not tipo_raw and not jugador:
            continue
        # Goal variants
        if any(k in tipo_raw for k in ["goal", "gol", "score", "point", "cancha"]):
            linea = f"⚽ {minuto}' {equipo} — {jugador} {detalle}".strip()
        # Card variants
        elif any(k in tipo_raw for k in ["card", "yellow", "red", "tarjeta", "amonest", "foul"]):
            color = "🟨" if any(k in tipo_raw for k in ["yellow", "amarilla", "yellow_card"]) else "🟥"
            linea = f"{color} {minuto}' {equipo} — {jugador}".strip()
        # Substitution variants
        elif any(k in tipo_raw for k in ["substitution", "sub", "cambio", "change"]):
            entra = str(e.get("playerIn", "") or e.get("substitute", "") or e.get("player_in", "") or "")
            sale = str(e.get("playerOut", "") or e.get("player_out", "") or jugador)
            if entra:
                linea = f"🔄 {minuto}' {equipo} — entra {entra}, sale {sale}".strip()
            else:
                linea = f"🔄 {minuto}' {equipo} — {sale}".strip()
        # Penalty variants
        elif any(k in tipo_raw for k in ["penalty", "penal"]):
            linea = f"🎯 {minuto}' {equipo} — {jugador} {detalle}".strip()
        # Woodwork / other notable
        elif any(k in tipo_raw for k in ["woodwork", "poste", "palo", "save", "salvada"]):
            linea = f"🥅 {minuto}' {equipo} — {jugador} {detalle}".strip()
        else:
            continue
        # Extraer minuto numérico del campo minute para ordenar
        import re

        m = re.search(r"(\d+)", minuto)
        minuto_sort = int(m.group(1)) if m else 9999
        items.append((minuto_sort, linea))
    # Ordenar por minuto
    items.sort(key=lambda x: x[0])
    return [linea for _, linea in items]


def _formatear_tarjetas(eventos: List[Dict[str, Any]]) -> List[str]:
    """Solo tarjetas amarillas y rojas."""
    out: List[str] = []
    for e in eventos or []:
        if not isinstance(e, dict):
            continue
        tipo = str(e.get("type", "") or e.get("category", "") or "").lower()
        if not any(k in tipo for k in ["card", "yellow", "red", "tarjeta", "amonest"]):
            continue
        minuto = str(e.get("minute", "") or e.get("time", "") or e.get("clock", "") or "")
        minuto = minuto.replace("''", "'").strip()
        if minuto and not minuto.endswith("'"):
            minuto = minuto + "'"
        equipo = str(e.get("team", "") or e.get("team_name", "") or e.get("home_team", "") or "")
        jugador = str(e.get("player", "") or e.get("playerName", "") or e.get("athlete", "") or e.get("name", "") or "")
        color = "🟨" if any(k in tipo for k in ["yellow", "amarilla", "yellow_card"]) else "🟥"
        out.append(f"{color} {minuto}' {equipo} — {jugador}".strip())
    return out[:10]


def _goles_desde_marcador(home: str, away: str, hg: Optional[int], ag: Optional[int]) -> List[str]:
    """Genera líneas de goles a partir del marcador si no hay eventos detallados."""
    if hg is None or ag is None:
        return []
    lineas: List[str] = []
    for i in range(hg):
        lineas.append(f"⚽ {home} — Gol {i + 1}")
    for i in range(ag):
        lineas.append(f"⚽ {away} — Gol {i + 1}")
    return lineas


def _normalizar_eventos(eventos: Any) -> List[Dict[str, Any]]:
    """Unifica eventos estructurados y líneas del scraper fuerte."""
    salida: List[Dict[str, Any]] = []
    for evento in eventos or []:
        if isinstance(evento, dict):
            salida.append(evento)
            continue
        linea = str(evento or "").strip()
        if not linea:
            continue
        gol = re.match(r"^⚽\s*(\d+(?:\+\d+)?)'?\s+(.+?)\s+[—-]\s*(.*)$", linea)
        if gol:
            salida.append(
                {
                    "type": "goal",
                    "minute": gol.group(1),
                    "team": gol.group(2).strip(),
                    "detail": gol.group(3).strip(),
                }
            )
            continue
        roja = re.match(r"^🟥\s*(\d+(?:\+\d+)?)'?\s+(.+?)\s+[—-]\s*(.*)$", linea)
        if roja:
            salida.append(
                {
                    "type": "red_card",
                    "minute": roja.group(1),
                    "team": roja.group(2).strip(),
                    "detail": roja.group(3).strip(),
                }
            )
    return salida


def _minuto_numerico(valor: Any) -> Optional[int]:
    """Convierte 90+6, 90'+6' o 96 a minuto absoluto para comparar."""
    texto = str(valor or "").replace("’", "'").strip()
    if not texto:
        return None
    partes = re.findall(r"\d+", texto)
    if not partes:
        return None
    numeros = [int(parte) for parte in partes[:2]]
    return sum(numeros) if "+" in texto and len(numeros) > 1 else numeros[0]


def _minuto_visible(valor: Any) -> str:
    texto = str(valor or "").replace("’", "'").replace("'", "").strip()
    return texto or "minuto no disponible"


def _gol_tardio_del_ganador(eventos: List[Dict[str, Any]], ganador: str) -> Optional[str]:
    """Devuelve el minuto visible de un gol del ganador desde el 90'."""
    clave_ganador = canonical_team_key(ganador)
    candidatos: List[tuple[int, str]] = []
    for evento in eventos or []:
        if not isinstance(evento, dict):
            continue
        tipo = str(evento.get("type", "") or evento.get("category", "") or "").lower()
        if not any(palabra in tipo for palabra in ("goal", "gol", "score")):
            continue
        equipo = str(evento.get("team", "") or evento.get("team_name", "") or "")
        if canonical_team_key(equipo) != clave_ganador:
            continue
        valor_minuto = evento.get("minute", "") or evento.get("time", "") or evento.get("clock", "")
        minuto = _minuto_numerico(valor_minuto)
        if minuto is not None and minuto >= 90:
            candidatos.append((minuto, _minuto_visible(valor_minuto)))
    return max(candidatos, default=(0, ""))[1] or None


def _conclusion_factual(
    home: str,
    away: str,
    hg: Optional[int],
    ag: Optional[int],
    eventos: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Describe solo hechos observables; no deduce dominio desde el marcador."""
    if hg is None or ag is None:
        return {"disponible": False, "motivo": "Marcador final no disponible.", "conclusion": ""}
    if hg == ag:
        texto = (
            f"{home} y {away} empataron {hg}-{ag}. "
            "Sin posesión, tiros y xG no se puede afirmar qué equipo dominó."
        )
        return {"disponible": True, "conclusion": texto, "fuente": "marcador_eventos"}

    ganador, perdedor = (home, away) if hg > ag else (away, home)
    goles_ganador, goles_perdedor = (hg, ag) if hg > ag else (ag, hg)
    margen = goles_ganador - goles_perdedor
    minuto_tardio = _gol_tardio_del_ganador(eventos, ganador) if margen == 1 else None
    if minuto_tardio and goles_perdedor == 0:
        descripcion = f"Fue una victoria sufrida por margen mínimo, resuelta con un gol al {minuto_tardio}."
    elif minuto_tardio:
        descripcion = f"Fue una victoria por margen mínimo que incluyó un gol tardío al {minuto_tardio}."
    elif margen == 1:
        descripcion = "Fue una victoria por margen mínimo."
    else:
        descripcion = f"El marcador fue amplio, con {margen} goles de diferencia."
    texto = (
        f"{ganador} ganó {goles_ganador}-{goles_perdedor} a {perdedor}. {descripcion} "
        "Sin posesión, tiros y xG no se puede calificar la actuación como dominante ni sólida."
    )
    return {"disponible": True, "conclusion": texto, "fuente": "marcador_eventos"}


def _senales_partido(
    home: str, away: str, hg: Optional[int], ag: Optional[int], eventos: List[Dict[str, Any]]
) -> Tuple[List[str], Set[str], Set[str]]:
    """
    Detecta señales factuales del partido a partir de marcador + eventos:
    - Victoria por margen mínimo o marcador amplio
    - Gol ganador tardío en un 1-0/0-1
    - Equipo que jugó con un hombre menos
    No infiere dominio ni etiqueta a un visitante como underdog sin datos previos.
    Devuelve tupla (lineas, bien_set, mal_set) donde bien/mal son sets de equipos
    clasificados de forma determinista (el sujeto es siempre home/away explícito).
    """
    senales: List[str] = []
    bien: set = set()
    mal: set = set()
    if hg is None or ag is None:
        return senales, bien, mal

    # Contar rojas por equipo
    rojas: Dict[str, int] = {}
    for e in eventos or []:
        if not isinstance(e, dict):
            continue
        tipo = str(e.get("type", "") or e.get("category", "") or "").lower()
        if "red" in tipo or "tarjeta roja" in tipo:
            eq = str(e.get("team", "") or e.get("team_name", "") or "")
            if eq:
                rojas[eq] = rojas.get(eq, 0) + 1

    # Resultado base: describir el marcador sin convertirlo en evaluación de juego.
    if hg == ag:
        senales.append(f"Empate {home} {hg}-{ag} {away}")
    else:
        ganador, perdedor = (home, away) if hg > ag else (away, home)
        goles_ganador, goles_perdedor = (hg, ag) if hg > ag else (ag, hg)
        margen = goles_ganador - goles_perdedor
        minuto_tardio = _gol_tardio_del_ganador(eventos, ganador) if margen == 1 else None
        if minuto_tardio and goles_perdedor == 0:
            senales.append(
                f"Victoria sufrida de {ganador}: {goles_ganador}-{goles_perdedor} "
                f"con gol al {minuto_tardio}; el resultado no demuestra dominio"
            )
        elif minuto_tardio:
            senales.append(
                f"Victoria de {ganador} por margen mínimo ({goles_ganador}-{goles_perdedor}) "
                f"con gol tardío al {minuto_tardio}; el marcador no demuestra dominio"
            )
        elif margen == 1:
            senales.append(
                f"Victoria de {ganador} por margen mínimo ({goles_ganador}-{goles_perdedor}); "
                "el marcador no demuestra dominio"
            )
        else:
            senales.append(
                f"Victoria amplia en el marcador de {ganador} ({goles_ganador}-{goles_perdedor}); "
                "faltan estadísticas para evaluar dominio"
            )
        bien.add(ganador)
        mal.add(perdedor)

    # Equipo con roja
    for eq, n in rojas.items():
        gf = hg if eq == home else (ag if eq == away else None)
        gc = ag if eq == home else (hg if eq == away else None)
        if gf is None or gc is None:
            continue
        if n >= 1:
            if gf > gc:
                senales.append(f"{eq} ganó CON {n} roja(s)")
                bien.add(eq)
            elif gf == gc:
                senales.append(f"{eq} empató CON {n} roja(s)")
            else:
                senales.append(f"{eq} perdió CON {n} roja(s) (arranque malo)")
                mal.add(eq)
    return senales, bien, mal


def _conclusion_ia(
    home: str, away: str, detalle: Dict[str, Any], hg: Optional[int] = None, ag: Optional[int] = None
) -> Dict[str, Any]:
    """Genera una conclusión factual y determinista; no usa narración libre."""
    return _conclusion_factual(home, away, hg, ag, detalle.get("eventos", []))


def _comparar_picks_anteriores(home: str, away: str, picks_anteriores: List[Dict[str, Any]]) -> List[str]:
    """
    Compara este partido con picks anteriores del bot.
    Devuelve líneas como: "El bot había recomendado América (local) — acertó."
    """
    lineas: List[str] = []
    if not picks_anteriores:
        return lineas
    key_home = canonical_team_key(home)
    key_away = canonical_team_key(away)
    for pk in picks_anteriores:
        pk_eq = pk.get("equipo", "")
        pk_rival = pk.get("rival", "")
        pk_cond = pk.get("condicion", "")
        if canonical_team_key(pk_eq) == key_home and canonical_team_key(pk_rival) == key_away:
            lineas.append(f"🤖 El bot había recomendado {pk_eq} ({pk_cond}) en este partido.")
        elif canonical_team_key(pk_eq) == key_away and canonical_team_key(pk_rival) == key_home:
            lineas.append(f"🤖 El bot había recomendado {pk_eq} ({pk_cond}) en este partido.")
    return lineas


def _procesar_partido(p: Dict[str, Any], picks_anteriores: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Procesa UN partido de forma aislada: obtiene detalle (con cache), conclusion IA,
    y devuelve el dict de análisis. Pensado para correr en paralelo por partido.
    """
    home = p.get("home_team", "")
    away = p.get("away_team", "")
    hg = p.get("home_goals")
    ag = p.get("away_goals")

    detalle = obtener_detalle_partido(home, away, event_id=p.get("event_id"), fecha=p.get("fecha", ""))

    # Prioridad: eventos de ESPN si existen
    if p.get("eventos_espn"):
        detalle["eventos"] = p["eventos_espn"]

    # Si no hay eventos, fallback rápido SIN web search
    if not detalle.get("eventos"):
        detalle_fuera = _obtener_detalles_fuera(home, away, p.get("fecha", ""), hg=hg or 0, ag=ag or 0)
        if detalle_fuera:
            detalle["eventos"] = detalle_fuera.get("eventos", [])

    detalle["eventos"] = _normalizar_eventos(detalle.get("eventos", []))

    # La narración siempre pasa por el generador factual; no se aceptan
    # conclusiones web preescritas que puedan atribuir dominio sin estadísticas.
    detalle.pop("conclusion_ia", None)
    conclusion = _conclusion_ia(home, away, detalle, hg=hg, ag=ag)

    eventos_lineas = _formatear_eventos(detalle.get("eventos", []))
    tarjetas_lineas = _formatear_tarjetas(detalle.get("eventos", []))
    picks_lineas = _comparar_picks_anteriores(home, away, picks_anteriores)

    if hg is not None and ag is not None:
        if hg > ag:
            resultado = f"🏆 {home} {hg}-{ag} {away}"
        elif hg < ag:
            resultado = f"🏆 {away} {ag}-{hg} {home}"
        else:
            resultado = f"🤝 {home} {hg}-{ag} {away}"
    else:
        resultado = f"⏳ {home} vs {away}"

    # Calcular señales una sola vez (lista + sets bien/mal)
    _sen_list, _sen_bien, _sen_mal = _senales_partido(home, away, hg, ag, detalle.get("eventos", []))
    ret = {
        "home": home,
        "away": away,
        "home_goals": hg,
        "away_goals": ag,
        "resultado": resultado,
        "eventos": detalle.get("eventos", []),
        "eventos_lineas": eventos_lineas,
        "tarjetas": tarjetas_lineas,
        "alineacion": detalle.get("alineacion"),
        "impacto_xi": detalle.get("impacto_xi"),
        "picks_lineas": picks_lineas,
        "senales": _sen_list,
        "bien": _sen_bien,
        "mal": _sen_mal,
        "conclusion_ia": conclusion,
    }
    return ret


def analizar_jornada(
    fecha: Optional[str] = None, picks_anteriores: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Analiza TODOS los partidos YA JUGADOS de la jornada actual.
    Devuelve un dict con:
      - partidos: lista de análisis por partido
      - resumen: texto HTML para Telegram (mensaje 1)
      - resumen_2: texto HTML para Telegram (mensaje 2, si hay más de 5 partidos)
      - tabla_posiciones: resumen de cómo va cada equipo
    """
    partidos = obtener_partidos_jornada(fecha)
    if not partidos:
        return {
            "partidos": [],
            "resumen": "No hay partidos jugados aún en la jornada actual.",
            "resumen_2": "",
            "tabla_posiciones": "",
        }

    picks_anteriores = picks_anteriores or []

    # Procesar TODOS los partidos en paralelo (cada uno hace sus HTTP + IA en su hilo)
    analisis: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(len(partidos), 8)) as ex:
        futuros = {ex.submit(_procesar_partido, p, picks_anteriores): p for p in partidos}
        for fut in as_completed(futuros):
            try:
                analisis.append(fut.result())
            except Exception:
                p = futuros[fut]
                analisis.append(
                    {
                        "home": p.get("home_team", ""),
                        "away": p.get("away_team", ""),
                        "home_goals": p.get("home_goals"),
                        "away_goals": p.get("away_goals"),
                        "eventos": [],
                        "eventos_lineas": [],
                        "tarjetas": [],
                        "alineacion": None,
                        "impacto_xi": None,
                        "picks_lineas": [],
                        "conclusion_ia": {"disponible": False, "motivo": "Error al procesar", "conclusion": ""},
                    }
                )
    # Ordenar por fecha/orden original de la jornada
    mapa_orden = {(p.get("home_team"), p.get("away_team")): i for i, p in enumerate(partidos)}
    analisis.sort(key=lambda a: mapa_orden.get((a["home"], a["away"]), 0))

    # Estadísticas por equipo (solo esta jornada, para contexto si hiciera falta)
    stats_equipos: Dict[str, Dict[str, Any]] = {}
    for a in analisis:
        home = a["home"]
        away = a["away"]
        hg = a.get("home_goals")
        ag = a.get("away_goals")
        for equipo, gf, gc in [(home, hg or 0, ag or 0), (away, ag or 0, hg or 0)]:
            if equipo not in stats_equipos:
                stats_equipos[equipo] = {"gf": 0, "gc": 0, "pj": 0, "g": 0, "e": 0, "p": 0, "puntos": 0}
            stats_equipos[equipo]["gf"] += gf
            stats_equipos[equipo]["gc"] += gc
            stats_equipos[equipo]["pj"] += 1
            if gf > gc:
                stats_equipos[equipo]["g"] += 1
                stats_equipos[equipo]["puntos"] += 3
            elif gf == gc:
                stats_equipos[equipo]["e"] += 1
                stats_equipos[equipo]["puntos"] += 1
            else:
                stats_equipos[equipo]["p"] += 1

    # Tabla general del TORNEO (acumula todas las jornadas guardadas)
    _fecha_guardado = fecha or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _guardar_resultados_jornada(stats_equipos, _fecha_guardado)
    hist = cargar_historial_resultados()
    tabla_torneo = _tabla_acumulada()
    total_jornadas = len((hist.get("por_fecha") or {})) if isinstance(hist, dict) else 0

    # Tabla de posiciones resumida (formato móvil)
    tabla_lineas = [
        "",
        f"📈 <b>TABLA GENERAL ({total_jornadas} j.)</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if not tabla_torneo:
        tabla_lineas.append("(sin datos aún)")
    for pos, (eq, st) in enumerate(
        sorted(tabla_torneo.items(), key=lambda x: (x[1]["puntos"], x[1]["gf"] - x[1]["gc"]), reverse=True), 1
    ):
        dg = st["gf"] - st["gc"]
        dg_str = f"+{dg}" if dg > 0 else str(dg)
        pts = st["puntos"]
        # Formato compacto: posición | equipo | pts | pj | dg
        tabla_lineas.append(f"{pos}º {eq}")
        tabla_lineas.append(f"   PTS:{pts}  PJ:{st['pj']}  DG:{dg_str}")
    tabla_lineas.append("━━━━━━━━━━━━━━━━━━")

    # Señales de la jornada: qué equipos empezaron bien / mal
    bien_set: set = set()
    mal_set: set = set()
    for a in analisis:
        bien_set |= a.get("bien") or set()
        mal_set |= a.get("mal") or set()
    # Un equipo no puede estar en ambos: si aparece en los dos, lo quitamos de MAL
    mal_set -= bien_set
    if bien_set or mal_set:
        tabla_lineas.append("")
        tabla_lineas.append("🚦 <b>SEÑALES DE LA JORNADA</b>")
        tabla_lineas.append("━━━━━━━━━━━━━━━━━━")
        if bien_set:
            tabla_lineas.append("✅ <b>Empezaron BIEN:</b>")
            for eq in sorted(bien_set):
                tabla_lineas.append(f"   {eq}")
        if mal_set:
            tabla_lineas.append("❌ <b>Empezaron MAL:</b>")
            for eq in sorted(mal_set):
                tabla_lineas.append(f"   {eq}")
        tabla_lineas.append("━━━━━━━━━━━━━━━━━━")
    tabla_lineas.append(f"<i>{_DECISION}</i>")

    # Mensaje 1: primeros 5 partidos
    ahora_str = datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC")
    mensaje1_partes = [
        "",
        "📊 <b>ANÁLISIS DE LA JORNADA</b>",
        f"🕒 {ahora_str}",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for a in analisis[:5]:
        mensaje1_partes.extend(_bloque_partido(a))
    mensaje1_partes.append(f"<i>{_DECISION}</i>")

    # Mensaje 2: resto + tabla
    mensaje2_partes = [
        "",
        "📊 <b>ANÁLISIS (continuación)</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for a in analisis[5:]:
        mensaje2_partes.extend(_bloque_partido(a))
    mensaje2_partes.extend(tabla_lineas)

    # Mensajes individuales (uno por partido)
    mensajes_individuales = ["\n".join(_bloque_partido(a)) for a in analisis]

    return {
        "partidos": analisis,
        "resumen": "\n".join(mensaje1_partes),
        "resumen_2": "\n".join(mensaje2_partes) if mensaje2_partes else "",
        "tabla_posiciones": "\n".join(tabla_lineas),
        "mensajes_individuales": mensajes_individuales,
        "mensaje_tabla": "\n".join(tabla_lineas),
    }


def _bloque_partido(a: Dict[str, Any]) -> List[str]:
    """Arma el bloque de un partido para Telegram (con contexto)."""
    home = a["home"]
    away = a["away"]
    hg = a.get("home_goals")
    ag = a.get("away_goals")

    # Encabezado del partido
    if hg is not None and ag is not None:
        if hg > ag:
            marcador = f"🏆 <b>{home}</b> {hg}-{ag} {away}"
        elif hg < ag:
            marcador = f"🏆 <b>{away}</b> {ag}-{hg} {home}"
        else:
            marcador = f"🤝 <b>{home}</b> {hg}-{ag} {away}"
    else:
        marcador = f"⏳ {home} vs {away}"

    bloque = [
        "",
        f"⚽  <b>{home}</b>  vs  <b>{away}</b>",
        marcador,
    ]

    eventos_lineas = a.get("eventos_lineas") or []

    # Goles - mostrar hasta 4
    if eventos_lineas:
        goles = [e for e in eventos_lineas if any(emoji in e for emoji in ["⚽", "🥅"])]
        if goles:
            bloque.append("")
            bloque.append("⚽ <b>Goles:</b>")
            for g in goles[:4]:
                bloque.append(g)

    # Tarjetas rojas (máx 2)
    if eventos_lineas:
        tarjetas_rojas = [e for e in eventos_lineas if "🟥" in e]
        if tarjetas_rojas:
            bloque.append("")
            bloque.append("🟥 <b>Rojas:</b>")
            for t in tarjetas_rojas[:2]:
                bloque.append(t)

    # Señales detectadas - hasta 3 (explicadas brevemente)
    senales = a.get("senales") or []
    if senales:
        bloque.append("")
        bloque.append("🚦 <b>Señales clave:</b>")
        for s in senales[:3]:
            # Añadir explicación breve
            senal_explicada = f"  • {s}"  # Ej: "  • Local vencido por bajo marcador"
            bloque.append(senal_explicada)

    # Conclusión factual - 800 caracteres máximo.
    conclusion = a.get("conclusion_ia", {})
    if conclusion.get("disponible") and conclusion.get("conclusion"):
        texto = conclusion["conclusion"]
        if len(texto) > 800:
            texto = texto[:800] + "..."
        bloque.append("")
        bloque.append("💡 <b>Análisis:</b>")
        bloque.append(texto)
    elif conclusion.get("motivo"):
        bloque.append("")
        bloque.append(f"<i>IA: {conclusion['motivo']}</i>")

    bloque.append("━━━━━━━━━━")
    return bloque


def _guardar_resultados_jornada(stats_equipos: Dict[str, Dict[str, Any]], fecha: str) -> None:
    """
    Guarda los resultados de la jornada en data/historial_resultados.json.
    Usa un dict por fecha (merge) para NO duplicar si se corre /analisis varias
    veces en la misma jornada.
    """
    import json
    from pathlib import Path

    BASE_DIR = Path(__file__).resolve().parents[1]
    historial_path = BASE_DIR / "data" / "historial_resultados.json"
    try:
        if historial_path.exists():
            with open(historial_path, "r", encoding="utf-8") as f:
                historial = json.load(f)
        else:
            historial = {}
    except Exception:
        historial = {}

    if not isinstance(historial, dict):
        historial = {}

    por_fecha = historial.get("por_fecha", {})
    por_fecha[fecha] = {
        "fecha": fecha,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "equipos": stats_equipos,
    }
    historial["por_fecha"] = por_fecha
    # Mantener también lista legible de las últimas 10 fechas
    historial["jornadas"] = [por_fecha[f] for f in sorted(por_fecha.keys())[-10:]]

    try:
        historial_path.parent.mkdir(parents=True, exist_ok=True)
        with open(historial_path, "w", encoding="utf-8") as f:
            json.dump(historial, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.debug("Exception silenciada en _guardar_resultados_jornada", exc_info=True)


def _tabla_acumulada() -> Dict[str, Dict[str, Any]]:
    """
    Tabla general del torneo: suma los equipos de TODAS las jornadas guardadas
    en data/historial_resultados.json. Devuelve {equipo: {pj,g,e,p,gf,gc,puntos}}.
    Si no hay historial, devuelve {}.
    """
    historial = cargar_historial_resultados()
    acum: Dict[str, Dict[str, Any]] = {}
    por_fecha = (historial.get("por_fecha") or {}) if isinstance(historial, dict) else {}
    # fallback a formato viejo (lista de jornadas)
    if not por_fecha and isinstance(historial, dict):
        for j in historial.get("jornadas", []) or []:
            if isinstance(j, dict) and isinstance(j.get("equipos"), dict):
                por_fecha[j.get("fecha", "x")] = j
    for datos in por_fecha.values():
        equipos = (datos or {}).get("equipos", {}) if isinstance(datos, dict) else {}
        for eq, st in equipos.items():
            if not isinstance(st, dict):
                continue
            a = acum.setdefault(eq, {"pj": 0, "g": 0, "e": 0, "p": 0, "gf": 0, "gc": 0, "puntos": 0})
            a["pj"] += int(st.get("pj", 0) or 0)
            a["g"] += int(st.get("g", 0) or 0)
            a["e"] += int(st.get("e", 0) or 0)
            a["p"] += int(st.get("p", 0) or 0)
            a["gf"] += int(st.get("gf", 0) or 0)
            a["gc"] += int(st.get("gc", 0) or 0)
            a["puntos"] += int(st.get("puntos", 0) or 0)
    return acum


def cargar_historial_resultados() -> Dict[str, Any]:
    """Carga el historial de resultados de las jornadas guardadas."""
    import json
    from pathlib import Path

    BASE_DIR = Path(__file__).resolve().parents[1]
    historial_path = BASE_DIR / "data" / "historial_resultados.json"
    try:
        if historial_path.exists():
            with open(historial_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {"jornadas": []}
    except Exception:
        logger.debug("Exception silenciada en cargar_historial_resultados", exc_info=True)
    return {"jornadas": []}
