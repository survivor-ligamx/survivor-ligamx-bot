#!/usr/bin/env python3
"""
ligamx_api.py — Cliente de la API externa de Liga MX (proyecto hermano).

Conecta con la Liga MX API pública (https://ligamx-api.onrender.com), del mismo
autor, que sirve datos del torneo vigente (Apertura 2026): equipos, calendario
completo por jornada, tabla, partidos, forma, disciplina, noticias y un
predictor Poisson propio.

Para el Survivor lo más valioso HOY es el **calendario completo** (`/calendar`),
que ya viene agrupado por jornada y alimenta directamente al planificador de
temporada (`planificador_survivor`) vía `data/calendario.json`.

⚠️ NO CONFUNDIR con `src/routers/api_ligamx.py`: aquel EXPONE la API pública
`/api/v1` de ESTE bot (sirve datos propios: ESPN + modelo). Este módulo
(`ligamx_api`) es lo contrario: un CLIENTE que CONSUME la API externa hermana
`ligamx-api.onrender.com`.

Cuando la temporada avance y haya partidos jugados, esta API también puede
alimentar al modelo Poisson con `resultados_historicos()` (goles finalizados) y
servir tabla/forma. En pretemporada (0 partidos jugados) esas funciones
devuelven vacío y el pipeline sigue usando ESPN sin romperse.

Diseño:
- **Opcional y tolerante a fallos.** Si la API está caída o dormida (Render free
  tier duerme y tarda ~30-60s en despertar), las funciones lanzan un error claro
  o devuelven vacío según el caso; NUNCA rompen el pipeline principal.
- **Sin scraping, sin credenciales.** Es una API JSON pública; no requiere key.
- **No cierra ni envía picks.** Solo lee datos. INFORMATIVO / REVISIÓN HUMANA.

Config (entorno, opcional):
    LIGAMX_API_URL       base de la API (default https://ligamx-api.onrender.com)
    LIGAMX_API_TIMEOUT   timeout por request en segundos (default 30)
    LIGAMX_API_AS_SOURCE si es truthy, fuentes_datos usa esta API como fuente
                         PRIMARIA de resultados para el modelo (default off).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, cast
import logging

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:  # pragma: no cover - dependencia opcional ausente
    requests = None  # type: ignore[assignment]

try:
    from team_normalizer import display_team_name, canonical_team_key, team_aliases, clean_team_name, teams_match
except ImportError:  # pragma: no cover - ruta alterna de import
    from src.team_normalizer import (  # type: ignore
        display_team_name,
        canonical_team_key,
        team_aliases,
        clean_team_name,
        teams_match,
    )

DEFAULT_BASE_URL = "https://ligamx-api.onrender.com"
DECISION = "INFORMATIVO / REVISIÓN HUMANA"


def base_url() -> str:
    """URL base de la API (configurable con LIGAMX_API_URL). Sin '/' final."""
    return os.getenv("LIGAMX_API_URL", DEFAULT_BASE_URL).strip().rstrip("/")


def _timeout() -> float:
    try:
        return float(os.getenv("LIGAMX_API_TIMEOUT", "5"))
    except (TypeError, ValueError):
        return 5.0


def _get(path: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Any:
    """
    GET a la API y devuelve el JSON parseado. Lanza RuntimeError con un mensaje
    claro si falta `requests`, hay error de red o la API responde != 200.
    `timeout`: segundos por request (default LIGAMX_API_TIMEOUT).
    """
    if requests is None:
        raise RuntimeError("La dependencia 'requests' no está instalada.")
    url = f"{base_url()}/{path.lstrip('/')}"
    try:
        resp = requests.get(url, params=params or {}, timeout=timeout if timeout is not None else _timeout())
    except requests.RequestException as exc:  # pragma: no cover - error de red
        raise RuntimeError(f"No se pudo conectar a la Liga MX API: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"Liga MX API respondió HTTP {resp.status_code} en {path}.")
    return resp.json()


# ---------------------------------------------------------------------------
# Contrato estable de partidos (ligamx-api)
# ---------------------------------------------------------------------------
def normalizar_kickoff_utc(valor: Any) -> str:
    """Normaliza ISO-8601 con Z/offset a UTC explícito; naive legacy se asume UTC."""
    raw = str(valor or "").strip()
    if not raw:
        return ""
    iso = f"{raw[:-1]}+00:00" if raw[-1:].upper() == "Z" else raw
    try:
        kickoff = datetime.fromisoformat(iso)
    except ValueError:
        logger.warning("Kickoff ISO-8601 inválido recibido de ligamx-api: %s", raw)
        return raw
    # Compatibilidad con respuestas antiguas sin zona: históricamente eran UTC.
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    return kickoff.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalizar_partido_api(partido: Dict[str, Any]) -> Dict[str, Any]:
    """Preserva espn_event_id y publica identidad/kickoff estables sin mutar la entrada."""
    salida = dict(partido)
    espn_raw = partido.get("espn_event_id")
    espn_event_id = str(espn_raw).strip() if espn_raw not in (None, "") else None
    kickoff_utc = normalizar_kickoff_utc(partido.get("match_date") or partido.get("date"))
    home = partido.get("home_team") or {}
    away = partido.get("away_team") or {}
    home_name = home.get("name", "") if isinstance(home, dict) else str(home)
    away_name = away.get("name", "") if isinstance(away, dict) else str(away)
    if espn_event_id:
        match_key = f"espn:{espn_event_id}"
    else:
        match_key = f"legacy:{canonical_team_key(home_name)}:{canonical_team_key(away_name)}:{kickoff_utc}"
    salida.update(
        {
            "espn_event_id": espn_event_id,
            "match_key": match_key,
            "kickoff_utc": kickoff_utc,
        }
    )
    return salida


# ---------------------------------------------------------------------------
# Salud / estado
# ---------------------------------------------------------------------------
def disponible(timeout: Optional[float] = None) -> bool:
    """
    True si la API responde en /health. Nunca lanza (útil para healthchecks).
    `timeout` corto (ej. 5s) sirve para decidir rápido si vale la pena enriquecer
    o si la API está dormida (Render free) y conviene saltarse el enriquecimiento.
    """
    try:
        _get("/health", timeout=timeout)
        return True
    except Exception:
        return False


def usar_como_fuente() -> bool:
    """True si LIGAMX_API_AS_SOURCE está activo (usar como fuente de resultados)."""
    return os.getenv("LIGAMX_API_AS_SOURCE", "").strip().lower() in ("1", "true", "yes", "on")


def archivar_momios(snapshots: List[Dict[str, Any]], timeout: float = 10.0) -> int:
    """
    Archiva snapshots de momios en la Liga MX API (POST /odds) para acumular el
    histórico de mercado (y poder backtestear la mezcla con el tiempo).

    Requiere LIGAMX_API_SYNC_KEY (la SYNC_API_KEY de la API) en el entorno. Sin
    key, sin `requests` o sin snapshots => no-op (devuelve 0). NUNCA rompe el
    flujo: ante cualquier error devuelve 0.
    """
    if not snapshots or requests is None:
        return 0
    key = os.getenv("LIGAMX_API_SYNC_KEY", "").strip()
    if not key:
        return 0
    try:
        resp = requests.post(
            f"{base_url()}/odds",
            json=snapshots,
            headers={"X-API-Key": key},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return int(resp.json().get("guardados", 0))
    except Exception:  # pragma: no cover - red no disponible
        return 0
    return 0


def momios_historicos(
    home_team: Optional[str] = None,
    away_team: Optional[str] = None,
    season: Optional[str] = None,
    limit: int = 2000,
) -> List[Dict[str, Any]]:
    """Consulta snapshots archivados; son la evidencia prepartido para detectar sorpresas."""
    params: Dict[str, Any] = {"limit": max(1, min(int(limit), 2000))}
    if home_team:
        params["home_team"] = home_team
    if away_team:
        params["away_team"] = away_team
    if season:
        params["season"] = season
    data = _get("/odds", params)
    return [dict(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def estado_temporada() -> Dict[str, Any]:
    """
    /season — qué torneo sirve la API y si ya arrancó.
    Ej.: {tournament_now, year, has_started, first_match_date, total_matches,
    finished_matches, ...}. `finished_matches` alimenta la cautela de arranque.
    """
    return cast(Dict[str, Any], _get("/season"))


# ---------------------------------------------------------------------------
# Calendario (lo que alimenta al planificador)
# ---------------------------------------------------------------------------
def obtener_calendario(season: Optional[str] = None) -> Dict[str, Any]:
    """/calendar — calendario completo agrupado por jornada (respuesta cruda)."""
    return cast(Dict[str, Any], _get("/calendar", {"season": season} if season else None))


def calendario_para_planificador(season: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Mapea /calendar al esquema que consume `planificador_survivor` /
    `data/calendario.json`:

        [{"jornada": int, "partidos": [{"home_team", "away_team", "date", "venue"}]}]

    OJO: confía en el campo `jornada` del upstream, que a veces agrupa mal
    (se han visto 16 jornadas con J1=11 y J12=18 en vez de 17×9). Para el
    calendario del planificador, `scripts/import_calendario.py` RE-DERIVA las
    jornadas desde las fechas reales (`fixtures_planos` + agrupado round-robin),
    lo que corrige esas anomalías. Esta función se conserva para uso directo.
    """
    data = obtener_calendario(season)
    jornadas: List[Dict[str, Any]] = []
    for j in sorted(data.get("jornadas", []), key=lambda x: int(x.get("jornada", 0))):
        partidos: List[Dict[str, Any]] = []
        for m in j.get("matches", []):
            normalizado = normalizar_partido_api(m)
            home = (m.get("home_team") or {}).get("name", "")
            away = (m.get("away_team") or {}).get("name", "")
            if not home or not away:
                continue
            partidos.append(
                {
                    "home_team": display_team_name(home),
                    "away_team": display_team_name(away),
                    "date": normalizado["kickoff_utc"] or m.get("date"),
                    "venue": m.get("venue"),
                    "espn_event_id": normalizado["espn_event_id"],
                    "match_key": normalizado["match_key"],
                    "kickoff_utc": normalizado["kickoff_utc"],
                }
            )
        if partidos:
            jornadas.append({"jornada": int(j.get("jornada", 0)), "partidos": partidos})
    return jornadas


def fixtures_planos(season: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Lista PLANA de partidos (sin agrupar por jornada), con la fecha real de cada
    uno, en el formato que consume `import_calendario.construir_calendario`:

        [{"fecha", "home_team", "away_team", "venue"}]

    Ignora el campo `jornada` del upstream a propósito: las jornadas se
    re-derivan de las fechas + la regla round-robin (cada equipo juega una vez
    por jornada), que reconstruye las 17 jornadas limpias de Liga MX. Nombres
    normalizados con `display_team_name`.
    """
    data = obtener_calendario(season)
    fixtures: List[Dict[str, Any]] = []
    for j in data.get("jornadas", []):
        for m in j.get("matches", []):
            home = (m.get("home_team") or {}).get("name", "")
            away = (m.get("away_team") or {}).get("name", "")
            fecha = m.get("date")
            if not home or not away or not fecha:
                continue
            normalizado = normalizar_partido_api(m)
            fixtures.append(
                {
                    "fecha": normalizado["kickoff_utc"] or fecha,
                    "home_team": display_team_name(home),
                    "away_team": display_team_name(away),
                    "venue": m.get("venue"),
                    "espn_event_id": normalizado["espn_event_id"],
                    "match_key": normalizado["match_key"],
                    "kickoff_utc": normalizado["kickoff_utc"],
                }
            )
    return fixtures


# ---------------------------------------------------------------------------
# Partidos / resultados (para el modelo cuando haya datos jugados)
# ---------------------------------------------------------------------------
def obtener_partidos(
    status: Optional[str] = None, season: Optional[str] = None, limit: int = 100, offset: int = 0
) -> List[Dict[str, Any]]:
    """/matches — partidos crudos (MatchResponse), con paginación y filtros."""
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    if season:
        params["season"] = season
    return cast(List[Dict[str, Any]], _get("/matches", params))


def partidos_proximos(limit: int = 10) -> List[Dict[str, Any]]:
    """/matches/upcoming — próximos partidos programados."""
    return cast(List[Dict[str, Any]], _get("/matches/upcoming", {"limit": limit}))


def resultados_historicos(season: Optional[str] = None, max_partidos: int = 1000) -> List[Dict[str, Any]]:
    """
    Resultados FINALIZADOS en el formato que espera el modelo Poisson
    (`home_team, away_team, home_goals, away_goals, fecha`).

    Pagina sobre /matches?status=finished y normaliza nombres. Si la temporada
    aún no tiene partidos jugados (pretemporada), devuelve [] (el pipeline cae a
    ESPN sin romperse).
    """
    salida: List[Dict[str, Any]] = []
    offset = 0
    page = 100
    while len(salida) < max_partidos:
        lote = obtener_partidos(status="finished", season=season, limit=page, offset=offset)
        if not lote:
            break
        for m in lote:
            home = (m.get("home_team") or {}).get("name", "")
            away = (m.get("away_team") or {}).get("name", "")
            hg, ag = m.get("home_score"), m.get("away_score")
            if not home or not away or hg is None or ag is None:
                continue
            try:
                hg, ag = int(hg), int(ag)
            except (TypeError, ValueError):
                continue
            normalizado = normalizar_partido_api(m)
            salida.append(
                {
                    "home_team": display_team_name(home),
                    "away_team": display_team_name(away),
                    "home_goals": hg,
                    "away_goals": ag,
                    "fecha": str(normalizado["kickoff_utc"] or m.get("match_date") or "")[:10],
                    "espn_event_id": normalizado["espn_event_id"],
                    "match_key": normalizado["match_key"],
                    "kickoff_utc": normalizado["kickoff_utc"],
                }
            )
        if len(lote) < page:
            break
        offset += page
    return salida


# ---------------------------------------------------------------------------
# Señales de contexto (opcionales)
# ---------------------------------------------------------------------------
def obtener_equipos(limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    """/teams — lista de equipos con id, nombre y estadio."""
    return cast(List[Dict[str, Any]], _get("/teams", {"limit": limit, "offset": offset}))


def mapa_equipos() -> Dict[str, int]:
    """
    Mapa {clave_canónica_del_nombre: team_id} de los 18 equipos. El `team_id` es
    la llave que habilita /predict, /h2h y proyecciones por partido. Usa
    `team_normalizer.canonical_team_key` para que casen alias/acentos.
    """
    mapa: Dict[str, int] = {}
    for e in obtener_equipos(limit=100):
        nombre, tid = e.get("name"), e.get("id")
        if nombre and tid is not None:
            mapa[canonical_team_key(nombre)] = int(tid)
    return mapa


def id_de_equipo(nombre: str, mapa: Optional[Dict[str, int]] = None) -> Optional[int]:
    """team_id a partir de un nombre (tolerante a alias/acentos). None si no está."""
    m = mapa if mapa is not None else mapa_equipos()
    return m.get(canonical_team_key(nombre))


def obtener_tabla(season: Optional[str] = None) -> List[Dict[str, Any]]:
    """/standings — tabla general (posición, PJ, PG, PE, PP, GF, GC, DG, Pts)."""
    return cast(List[Dict[str, Any]], _get("/standings", {"season": season} if season else None))


def tabla_normalizada(season: Optional[str] = None) -> Dict[str, Any]:
    """
    Mapea /standings al esquema intermedio que consume
    `tabla_posiciones.tabla_con_motivacion` (posicion, equipo, puntos, jugados,
    ganados, empatados, perdidos, goles_favor, goles_contra, diferencia).

    Devuelve {"torneo", "tabla": [...]}. Nombres vía display_team_name.
    """
    filas: List[Dict[str, Any]] = []
    for r in obtener_tabla(season):
        equipo = (r.get("team") or {}).get("name", "")
        if not equipo:
            continue
        filas.append(
            {
                "posicion": int(r.get("position", 0)),
                "equipo": display_team_name(equipo),
                "puntos": int(r.get("points", 0)),
                "jugados": int(r.get("played", 0)),
                "ganados": int(r.get("won", 0)),
                "empatados": int(r.get("drawn", 0)),
                "perdidos": int(r.get("lost", 0)),
                "goles_favor": int(r.get("goals_for", 0)),
                "goles_contra": int(r.get("goals_against", 0)),
                "diferencia": int(r.get("goal_difference", 0)),
            }
        )
    filas.sort(key=lambda x: x["posicion"] if x["posicion"] > 0 else 999)
    torneo = ""
    try:
        torneo = estado_temporada().get("tournament_now", "")
    except Exception:
        torneo = ""
    return {"torneo": torneo, "tabla": filas}


def predecir(home_id: int, away_id: int, season: Optional[str] = None) -> Dict[str, Any]:
    """
    /predict — predictor Poisson de la API (goles esperados, 1/X/2, marcador).
    OJO: requiere partidos jugados en la temporada; en pretemporada responde
    error (se propaga como RuntimeError).
    """
    params: Dict[str, Any] = {"home": home_id, "away": away_id}
    if season:
        params["season"] = season
    return cast(Dict[str, Any], _get("/predict", params))


# ---------------------------------------------------------------------------
# Señales por EQUIPO (forma, disciplina/tarjetas, racha, perfil, xG)
# ---------------------------------------------------------------------------
def perfil_equipo(team_id: int, season: Optional[str] = None) -> Dict[str, Any]:
    """/teams/{id}/profile — ficha + posición, forma, xG, próximo partido."""
    return cast(Dict[str, Any], _get(f"/teams/{team_id}/profile", {"season": season} if season else None))


def forma_equipo(team_id: int, limit: int = 5) -> Dict[str, Any]:
    """/teams/{id}/form — forma reciente (W/D/L de los últimos N) + racha texto."""
    return cast(Dict[str, Any], _get(f"/teams/{team_id}/form", {"limit": limit}))


def disciplina_equipo(team_id: int, season: Optional[str] = None) -> Dict[str, Any]:
    """
    /teams/{id}/discipline — tarjetas del equipo + jugadores EN RIESGO de
    suspensión por acumulación (`at_risk`). Señal directa de riesgo Survivor.
    """
    return cast(Dict[str, Any], _get(f"/teams/{team_id}/discipline", {"season": season} if season else None))


def racha_equipo(team_id: int) -> Dict[str, Any]:
    """/teams/{id}/streak — rachas actuales (invicto, victorias, sin ganar, ...)."""
    return cast(Dict[str, Any], _get(f"/teams/{team_id}/streak"))


def stats_temporada_equipo(team_id: int, season: Optional[str] = None) -> Dict[str, Any]:
    """/teams/{id}/season-stats — ~100 métricas de temporada (ESPN)."""
    return cast(Dict[str, Any], _get(f"/teams/{team_id}/season-stats", {"season": season} if season else None))


def xg_equipos(order: str = "over", season: Optional[str] = None) -> Any:
    """/teams/xg-performance — goles vs xG por equipo (efectivos vs que desperdician)."""
    params: Dict[str, Any] = {"order": order}
    if season:
        params["season"] = season
    return _get("/teams/xg-performance", params)


# ---------------------------------------------------------------------------
# Señales de LIGA (proyección, goleadores, power-ranking, noticias/lesiones)
# ---------------------------------------------------------------------------
def proyeccion_tabla(season: Optional[str] = None) -> Dict[str, Any]:
    """
    /standings/projection — proyección de la tabla FINAL (puntos esperados de los
    partidos restantes, Poisson). Requiere partidos jugados; en pretemporada
    puede responder error (se propaga).
    """
    return cast(Dict[str, Any], _get("/standings/projection", {"season": season} if season else None))


def power_ranking() -> Dict[str, Any]:
    """/power-ranking — ranking de fuerza (ppg + dif. goles; xG informativo)."""
    return cast(Dict[str, Any], _get("/power-ranking"))


def goleadores(limit: int = 20, season: Optional[str] = None) -> List[Dict[str, Any]]:
    """/top-scorers — tabla de goleo."""
    params: Dict[str, Any] = {"limit": limit}
    if season:
        params["season"] = season
    return cast(List[Dict[str, Any]], _get("/top-scorers", params))


def _campo(d: Dict[str, Any], *claves: str) -> Any:
    """Primer valor no vacío entre varias posibles claves (API tolerante)."""
    for k in claves:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def goleadores_por_equipo(
    limit: int = 50, por_equipo: int = 2, season: Optional[str] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Mapa {equipo_display: [ {nombre, goles} ]} con los máximos goleadores de cada
    equipo (para 'jugadores a seguir' por partido, sin llamadas por partido).

    Usa SOLO el goleo del torneo ACTUAL. En pretemporada (sin goles aún) devuelve
    {} a propósito: NO se cae al torneo anterior porque muchos jugadores cambian
    de equipo/se venden y sería un dato viejo y engañoso. Se llena solo cuando el
    torneo arranca (jornada 1-2).
    """
    try:
        data = goleadores(limit=limit, season=season)
    except Exception:  # pragma: no cover - red no disponible
        return {}
    if not isinstance(data, list):
        return {}
    mapa: Dict[str, List[Dict[str, Any]]] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        nombre = _campo(row, "player", "name", "player_name", "full_name")
        equipo = _campo(row, "team", "team_name", "club")
        if isinstance(equipo, dict):
            equipo = equipo.get("name") or equipo.get("team_name")
        goles = _campo(row, "goals", "goals_count", "total_goals", "g")
        if not nombre or not equipo:
            continue
        clave = display_team_name(str(equipo))
        entrada = {"nombre": str(nombre), "goles": goles}
        mapa.setdefault(clave, [])
        if len(mapa[clave]) < max(1, por_equipo):
            mapa[clave].append(entrada)
    return mapa


def porteros() -> List[Dict[str, Any]]:
    """/365scores/goalkeepers — porteros: vallas invictas, goles recibidos, salvadas."""
    d = _get("/365scores/goalkeepers")
    if isinstance(d, dict):
        d = d.get("goalkeepers") or d.get("rows") or d.get("data") or []
    return d if isinstance(d, list) else []


def transfers_365(status: str = "confirmado") -> Dict[str, Any]:
    """
    /365scores/transfers — altas/bajas por equipo (agrupado). Por defecto solo
    movimientos CONFIRMADOS (status='confirmado') para evitar rumores; usa
    status='' para traer todo. {} si no hay/falla.
    """
    params = {"status": status} if status else None
    d = _get("/365scores/transfers", params)
    return d if isinstance(d, dict) else {}


def _fmt_movimientos(lst: Any, campo_club: str) -> List[str]:
    """Formatea movimientos a 'Jugador (club)', deduplicando por jugador."""
    out: List[str] = []
    vistos = set()
    for m in lst or []:
        if isinstance(m, dict):
            nom = m.get("jugador") or m.get("nombre") or m.get("player")
            club = m.get(campo_club)
            if nom and nom not in vistos:
                vistos.add(nom)
                out.append(f"{nom} ({club})" if club else str(nom))
        elif isinstance(m, str) and m not in vistos:
            vistos.add(m)
            out.append(m)
    return out


def transfers_equipo(nombre: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
    """
    Altas/bajas de un equipo desde /365scores/transfers (match tolerante por
    nombre). `data` = respuesta ya bajada (para no llamar por cada equipo).
    Devuelve {'altas': [...], 'bajas': [...]} (listas de strings). Vacío si no hay.
    """
    data = data if data is not None else transfers_365()
    equipos = (data or {}).get("equipos") or {}
    eq: Optional[Dict[str, Any]] = None
    for k, v in equipos.items():
        if teams_match(str(k), nombre):
            eq = v or {}
            break
    if not eq:
        return {"altas": [], "bajas": []}
    return {
        "altas": _fmt_movimientos(eq.get("altas"), "desde"),
        "bajas": _fmt_movimientos(eq.get("bajas"), "hacia"),
    }


def porteros_por_equipo() -> Dict[str, Dict[str, Any]]:
    """
    Mapa {equipo_display: {nombre, vallas_invictas, goles_recibidos}} con el
    mejor portero (más vallas invictas) de cada equipo. Tolerante: {} si falla o
    en pretemporada (sin datos aún).
    """
    try:
        data = porteros()
    except Exception:  # pragma: no cover - red no disponible
        return {}
    mapa: Dict[str, Dict[str, Any]] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        nombre = _campo(row, "player", "name", "player_name", "goalkeeper", "nombre")
        equipo = _campo(row, "team", "team_name", "club")
        if isinstance(equipo, dict):
            equipo = equipo.get("name") or equipo.get("team_name")
        vallas = _campo(row, "clean_sheets", "cleanSheets", "vallas_invictas", "clean_sheet", "shutouts")
        recibidos = _campo(row, "goals_conceded", "goalsConceded", "goals_against", "goles_recibidos", "conceded")
        if not nombre or not equipo:
            continue
        clave = display_team_name(str(equipo))
        entrada = {"nombre": str(nombre), "vallas_invictas": vallas, "goles_recibidos": recibidos}
        # nos quedamos con el de más vallas invictas por equipo
        prev = mapa.get(clave)
        if prev is None:
            mapa[clave] = entrada
        else:
            try:
                if (int(vallas or 0)) > int(prev.get("vallas_invictas") or 0):
                    mapa[clave] = entrada
            except (TypeError, ValueError):
                logger.debug("Exception silenciada en porteros_por_equipo", exc_info=True)
    return mapa


def match_id_de_partido(home: str, away: str, espn_event_id: Optional[str] = None) -> Optional[int]:
    """Resuelve id interno; con ESPN ID exige coincidencia exacta, sin fallback ambiguo."""
    esperado = str(espn_event_id).strip() if espn_event_id not in (None, "") else None

    def _buscar(lista: Any) -> Optional[int]:
        if not isinstance(lista, list):
            return None
        for m in lista:
            if not isinstance(m, dict):
                continue
            if esperado is not None:
                actual = m.get("espn_event_id")
                if actual is None or str(actual).strip() != esperado:
                    continue
            else:
                h = m.get("home_team") or {}
                a = m.get("away_team") or {}
                hn = h.get("name") if isinstance(h, dict) else h
                an = a.get("name") if isinstance(a, dict) else a
                if not hn or not an or not (teams_match(str(hn), home) and teams_match(str(an), away)):
                    continue
            mid = _campo(m, "id", "match_id", "matchId")
            try:
                return int(mid) if mid is not None else None
            except (TypeError, ValueError):
                return None
        return None

    fuentes = (
        lambda: partidos_proximos(limit=50),
        lambda: obtener_partidos(status="finished", limit=100),
        lambda: obtener_partidos(limit=100),
    )
    for cargar in fuentes:
        mid = _buscar(_safe(cargar, []))
        if mid is not None:
            return mid
    return None


def jugadores_a_seguir_partido(home: str, away: str) -> Dict[str, List[str]]:
    """
    'Jugadores a seguir' de un partido (por nombres), vía
    /matches/{id}/players-to-watch. Devuelve {'local': [...], 'visita': [...]}.
    Tolerante: si no hay match_id o datos, listas vacías. Parseo defensivo de
    varias formas posibles de respuesta.
    """
    vacio: Dict[str, List[Any]] = {"local": [], "visita": []}
    mid = match_id_de_partido(home, away)
    if mid is None:
        return vacio
    data = _safe(lambda: jugadores_a_seguir(mid), None)
    if not isinstance(data, dict):
        return vacio

    def _nombres(bloque: Any) -> List[str]:
        out: List[str] = []
        if isinstance(bloque, dict):
            bloque = bloque.get("players") or bloque.get("jugadores") or list(bloque.values())
        if isinstance(bloque, list):
            for pl in bloque:
                if isinstance(pl, dict):
                    nom = _campo(pl, "player", "name", "player_name", "nombre", "full_name")
                    if nom:
                        out.append(str(nom))
                elif isinstance(pl, str):
                    out.append(pl)
        return out

    local = _nombres(_campo(data, "home", "local", "home_team"))
    visita = _nombres(_campo(data, "away", "visita", "away_team"))
    if not local and not visita:
        # forma plana: {players_to_watch: [{player, team}]}
        planos = data.get("players_to_watch") or data.get("players") or []
        if isinstance(planos, list):
            for pl in planos:
                if not isinstance(pl, dict):
                    continue
                nom = _campo(pl, "player", "name", "player_name", "nombre")
                eq = _campo(pl, "team", "team_name", "club")
                if isinstance(eq, dict):
                    eq = eq.get("name")
                if not nom:
                    continue
                if eq and teams_match(str(eq), away):
                    visita.append(str(nom))
                else:
                    local.append(str(nom))
    return {"local": local, "visita": visita}


def noticias_365() -> List[Dict[str, Any]]:
    """
    Noticias Liga MX desde **365Scores** (/365scores/news, plataforma real).
    Normaliza a {title, link, description, source, image_url, published_at}.
    """
    out: List[Dict[str, Any]] = []
    for n in _get("/365scores/news"):
        if not isinstance(n, dict):
            continue
        out.append(
            {
                "title": n.get("title", ""),
                "link": n.get("url", ""),
                "description": n.get("description", ""),
                "source": "365Scores",
                "image_url": n.get("image", ""),
                "published_at": n.get("published_at", ""),
            }
        )
    return out


def noticias_google() -> List[Dict[str, Any]]:
    """/news — noticias vía Google News RSS (ya viene en el esquema estándar)."""
    return cast(List[Dict[str, Any]], _get("/news"))


def _clave_titulo(item: Dict[str, Any]) -> str:
    """Clave de dedup por título (sin acentos, minúsculas, espacios colapsados)."""
    return cast(str, clean_team_name(str(item.get("title", ""))))


def noticias() -> List[Dict[str, Any]]:
    """
    Noticias Liga MX combinando **365Scores (primario)** + **Google News (relleno)**,
    deduplicadas por título. Tolerante: si una fuente falla, usa la otra. Esquema
    estable: {title, link, description, source, image_url, published_at}.
    """
    items: List[Dict[str, Any]] = list(_safe(noticias_365, []) or [])
    vistos = {_clave_titulo(i) for i in items if i.get("title")}
    for g in _safe(noticias_google, []) or []:
        if not isinstance(g, dict):
            continue
        clave = _clave_titulo(g)
        if clave and clave not in vistos:
            items.append(g)
            vistos.add(clave)
    return items


def noticias_recientes(limit: int = 10) -> List[Dict[str, Any]]:
    """
    Noticias recientes en forma COMPACTA (title, fuente, publicado, link),
    listas para mostrar/mandar. Ordenadas por fecha de publicación (recientes
    primero). Tolerante: si la API falla, propaga el error al caller.
    """
    crudas = noticias()
    items: List[Dict[str, Any]] = []
    for n in crudas:
        if not isinstance(n, dict):
            continue
        items.append(
            {
                "titulo": n.get("title", ""),
                "fuente": n.get("source", ""),
                "publicado": n.get("published_at", ""),
                "link": n.get("link", ""),
            }
        )
    items.sort(key=lambda x: str(x.get("publicado") or ""), reverse=True)
    return items[: max(0, limit)]


def noticias_de_equipos(nombres: List[str], limit: int = 5, dias: int = 30) -> List[Dict[str, Any]]:
    """
    Noticias que MENCIONAN a alguno de los equipos dados (por título/descripción),
    en forma compacta. Útil para el dossier del pick: ahí aparecen lesiones,
    bajas y fichajes del equipo. Match tolerante por alias (team_normalizer),
    con guarda de longitud para evitar falsos positivos por alias muy cortos.

    Filtros aplicados:
    - Solo noticias de los últimos `dias` días (default 30).
    - Ordenadas por fecha de publicación (recientes primero).
    - Dedup por título normalizado.
    """
    aliases: set = set()
    for nombre in nombres:
        for a in team_aliases(nombre):
            if len(a) >= 4:
                aliases.add(a)
    if not aliases:
        return []

    from datetime import datetime, timezone

    def _es_reciente(publicado: str, max_dias: int) -> bool:
        if not publicado:
            return True
        try:
            pub = datetime.fromisoformat(str(publicado).replace("Z", "+00:00"))
            ahora = datetime.now(timezone.utc)
            return (ahora - pub).days <= max_dias
        except Exception:
            return True

    out: List[Dict[str, Any]] = []
    vistos: set = set()
    for n in noticias():
        if not isinstance(n, dict):
            continue
        publicado = n.get("published_at", "")
        if not _es_reciente(publicado, dias):
            continue
        texto = clean_team_name(f"{n.get('title', '')} {n.get('description', '')}")
        if any(a in texto for a in aliases):
            clave = _clave_titulo(n)
            if clave and clave in vistos:
                continue
            if clave:
                vistos.add(clave)
            out.append(
                {
                    "titulo": n.get("title", ""),
                    "fuente": n.get("source", ""),
                    "publicado": publicado,
                    "link": n.get("link", ""),
                }
            )
    out.sort(key=lambda x: str(x.get("publicado") or ""), reverse=True)
    return out[: max(0, limit)]


def jugadores_en_riesgo() -> Any:
    """/players/discipline — jugadores con tarjetas / riesgo de suspensión."""
    return _get("/players/discipline")


def jugadores_en_riesgo_liga(limit: int = 20) -> Dict[str, Any]:
    """
    Versión compacta de /players/discipline: jugadores de TODA la liga en riesgo
    de suspensión por acumulación de tarjetas. Devuelve {season, count, jugadores}.
    En pretemporada (sin tarjetas aún) `jugadores` viene vacío. Tolerante.
    """
    d = jugadores_en_riesgo()
    if not isinstance(d, dict):
        return {"season": "", "count": 0, "jugadores": []}
    players = d.get("players") or []
    return {
        "season": d.get("season", ""),
        "count": int(d.get("count", len(players)) or 0),
        "jugadores": players[: max(0, limit)],
    }


# ---------------------------------------------------------------------------
# Enfrentamiento directo (H2H)
# ---------------------------------------------------------------------------
def h2h(team1_id: int, team2_id: int) -> List[Dict[str, Any]]:
    """/h2h/{t1}/{t2} — historial de partidos entre dos equipos."""
    return cast(List[Dict[str, Any]], _get(f"/h2h/{team1_id}/{team2_id}"))


def h2h_resumen(team1_id: int, team2_id: int) -> Dict[str, Any]:
    """/h2h/{t1}/{t2}/summary — resumen: jugados, victorias c/u, empates, goles."""
    return cast(Dict[str, Any], _get(f"/h2h/{team1_id}/{team2_id}/summary"))


# ---------------------------------------------------------------------------
# Señales por PARTIDO (alineaciones, eventos, tarjetas, jugadores a seguir)
# ---------------------------------------------------------------------------
def alineaciones(match_id: int) -> Dict[str, Any]:
    """/matches/{id}/lineups — titulares, suplentes, formación (cuando existan)."""
    return cast(Dict[str, Any], _get(f"/matches/{match_id}/lineups"))


def eventos_partido(match_id: int) -> Dict[str, Any]:
    """/matches/{id}/events — goles, tarjetas y cambios."""
    return cast(Dict[str, Any], _get(f"/matches/{match_id}/events"))


def tarjetas_partido(match_id: int) -> Dict[str, Any]:
    """/matches/{id}/cards — solo tarjetas (amarillas y rojas)."""
    return cast(Dict[str, Any], _get(f"/matches/{match_id}/cards"))


def jugadores_a_seguir(match_id: int) -> Dict[str, Any]:
    """/matches/{id}/players-to-watch — jugadores a seguir del partido."""
    return cast(Dict[str, Any], _get(f"/matches/{match_id}/players-to-watch"))


def partido_full(match_id: int) -> Dict[str, Any]:
    """/matches/{id}/full — todo el detalle del partido en una respuesta."""
    return cast(Dict[str, Any], _get(f"/matches/{match_id}/full"))


# ---------------------------------------------------------------------------
# Alineaciones confirmadas (vía 365Scores) — señal "¿salió con suplentes?".
# Se publican ~1h antes del inicio; antes de eso vienen vacías.
# ---------------------------------------------------------------------------
def eventos_365() -> List[Dict[str, Any]]:
    """/365scores/matches — fixtures de la temporada actual con event_id de 365Scores."""
    d = _get("/365scores/matches")
    return d if isinstance(d, list) else []


def evento_365_id(home: str, away: str) -> Optional[int]:
    """Busca el event_id de 365Scores del partido home vs away (match flexible por nombre)."""
    for m in eventos_365():
        if teams_match(m.get("home_team", ""), home) and teams_match(m.get("away_team", ""), away):
            eid = m.get("event_id")
            return int(eid) if eid is not None else None
    return None


def eventos_365_partido(event_id: int) -> List[Dict[str, Any]]:
    """/365scores/matches/{id}/events — goles, tarjetas y cambios de un partido."""
    d = _get(f"/365scores/matches/{event_id}/events")
    return d if isinstance(d, list) else []


def _nombre_jugador(p: Dict[str, Any]) -> str:
    for k in ("name", "player", "player_name", "full_name", "short_name"):
        v = p.get(k) if isinstance(p, dict) else None
        if v:
            return str(v)
    return ""


def alineacion_365(event_id: int) -> Dict[str, Any]:
    """
    Alineación de un partido vía /365scores/matches/{id}/lineups, normalizada:
    {disponible, equipos:[{equipo, condicion, formacion, titulares:[nombres]}]}.
    `disponible=False` si aún no publican XI (pretemporada o >1h antes).
    """
    d = _get(f"/365scores/matches/{event_id}/lineups")
    equipos: List[Dict[str, Any]] = []
    disponible = False
    for t in d.get("teams", []) if isinstance(d, dict) else []:
        players = t.get("players") or []
        if players:
            disponible = True
        equipos.append(
            {
                "equipo": t.get("team_name", ""),
                "condicion": t.get("home_away", ""),
                "formacion": t.get("formation"),
                "titulares": [n for n in (_nombre_jugador(p) for p in players) if n][:11],
            }
        )
    return {"disponible": disponible, "equipos": equipos}


def lineup_impact(game_id: int) -> Dict[str, Any]:
    """/365scores/matches/{id}/lineup-impact — fuerza del XI y ausentes clave. {} si falla."""
    d = _get(f"/365scores/matches/{game_id}/lineup-impact")
    return d if isinstance(d, dict) else {}


def lineup_impact_partido(home: str, away: str) -> Dict[str, Any]:
    """
    Impacto del XI de un partido (por NOMBRE): resuelve el event_id de 365Scores
    y devuelve {disponible, equipos:{equipo:{fuerza_xi_pct, ausentes_clave,...}}}.
    {} tolerante si no hay evento/datos.
    """
    eid = _safe(lambda: evento_365_id(home, away))
    if not eid:
        return {}
    return _safe(lambda: lineup_impact(eid), {}) or {}


def probable_lineup(game_id: int) -> Dict[str, Any]:
    """/365scores/matches/{id}/probable-lineup — XI probable (no confirmado). {} si falla."""
    d = _get(f"/365scores/matches/{game_id}/probable-lineup")
    return d if isinstance(d, dict) else {}


def probable_lineup_partido(home: str, away: str) -> Dict[str, Any]:
    """XI probable de un partido (por NOMBRE). {} tolerante si no hay evento/datos."""
    eid = _safe(lambda: evento_365_id(home, away))
    if not eid:
        return {}
    return _safe(lambda: probable_lineup(eid), {}) or {}


def alineacion_de_partido(home: str, away: str) -> Dict[str, Any]:
    """
    Alineación confirmada de un partido por NOMBRE de equipo. Tolerante: si no se
    encuentra el evento o aún no hay XI, devuelve disponible=False con nota.
    """
    eid = _safe(lambda: evento_365_id(home, away))
    if not eid:
        return {
            "disponible": False,
            "equipos": [],
            "nota": "No se encontró el partido en 365Scores (¿nombres o temporada?).",
        }
    r = _safe(lambda: alineacion_365(eid), None)
    if r is None:
        return {
            "disponible": False,
            "equipos": [],
            "event_id": eid,
            "nota": "No se pudo leer la alineación (aún no publicada).",
        }
    r["event_id"] = eid
    return cast(Dict[str, Any], r)


# ---------------------------------------------------------------------------
# Dossier agregado por partido (para enriquecer el pronóstico / decisión)
# ---------------------------------------------------------------------------
def _safe(fn, default=None):
    """Ejecuta fn() y devuelve su resultado; ante CUALQUIER error, `default`."""
    try:
        return fn()
    except Exception:
        return default


def analisis_partido(
    home: str,
    away: str,
    mapa: Optional[Dict[str, int]] = None,
    incluir_prediccion: bool = True,
) -> Dict[str, Any]:
    """
    Dossier agregado de un partido (por NOMBRE de equipo), juntando las señales
    de la Liga MX API que importan para el Survivor. Tolerante: cada pieza que
    falle o no tenga datos aún (pretemporada) queda en None sin romper el resto.

    Devuelve:
      {home, away, home_id, away_id, prediccion_api, forma_local, forma_visita,
       disciplina_local, disciplina_visita, racha_local, racha_visita,
       h2h_resumen, decision}
    """
    m = mapa if mapa is not None else _safe(mapa_equipos, {}) or {}
    hid = id_de_equipo(home, m)
    aid = id_de_equipo(away, m)

    dossier: Dict[str, Any] = {
        "home": display_team_name(home),
        "away": display_team_name(away),
        "home_id": hid,
        "away_id": aid,
        "prediccion_api": None,
        "forma_local": None,
        "forma_visita": None,
        "disciplina_local": None,
        "disciplina_visita": None,
        "racha_local": None,
        "racha_visita": None,
        "h2h_resumen": None,
        "decision": DECISION,
    }
    if hid is None or aid is None:
        dossier["nota"] = "No se pudo resolver el team_id de uno o ambos equipos."
        return dossier

    if incluir_prediccion:
        dossier["prediccion_api"] = _safe(lambda: predecir(hid, aid))
    dossier["forma_local"] = _safe(lambda: forma_equipo(hid))
    dossier["forma_visita"] = _safe(lambda: forma_equipo(aid))
    dossier["disciplina_local"] = _safe(lambda: disciplina_equipo(hid))
    dossier["disciplina_visita"] = _safe(lambda: disciplina_equipo(aid))
    dossier["racha_local"] = _safe(lambda: racha_equipo(hid))
    dossier["racha_visita"] = _safe(lambda: racha_equipo(aid))
    dossier["h2h_resumen"] = _safe(lambda: h2h_resumen(hid, aid))
    return dossier


def resumen_partido(
    home: str,
    away: str,
    mapa: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """
    Versión COMPACTA del dossier, pensada para adjuntar a un pick o mandar por
    Telegram (no vuelca los payloads crudos). Tolerante: lo que la API no tenga
    aún (pretemporada) queda en None / lista vacía.

    Devuelve:
      {home, away, home_id, away_id,
       prediccion_api: {prob_local_pct, prob_empate_pct, prob_visita_pct, goles_esp} | None,
       forma_local, forma_visita,           # cadenas tipo "WWDLW" o None
       en_riesgo_local, en_riesgo_visita,   # nombres de jugadores en riesgo de suspensión
       h2h}                                  # resumen head-to-head o None
    """
    m = mapa if mapa is not None else (_safe(mapa_equipos, {}) or {})
    hid = id_de_equipo(home, m)
    aid = id_de_equipo(away, m)
    out: Dict[str, Any] = {
        "home": display_team_name(home),
        "away": display_team_name(away),
        "home_id": hid,
        "away_id": aid,
        "prediccion_api": None,
        "forma_local": None,
        "forma_visita": None,
        "en_riesgo_local": [],
        "en_riesgo_visita": [],
        "h2h": None,
        "noticias": [],
        "decision": DECISION,
    }
    if hid is None or aid is None:
        out["nota"] = "No se pudo resolver el team_id de uno o ambos equipos."
        return out

    pred = _safe(lambda: predecir(hid, aid))
    if isinstance(pred, dict) and pred.get("probabilities"):
        p = pred["probabilities"]
        eg = pred.get("expected_goals") or {}
        out["prediccion_api"] = {
            "prob_local_pct": round(100.0 * float(p.get("home_win", 0) or 0), 1),
            "prob_empate_pct": round(100.0 * float(p.get("draw", 0) or 0), 1),
            "prob_visita_pct": round(100.0 * float(p.get("away_win", 0) or 0), 1),
            "goles_esp": f"{eg.get('home', '?')}-{eg.get('away', '?')}",
        }

    fl = _safe(lambda: forma_equipo(hid))
    fv = _safe(lambda: forma_equipo(aid))
    out["forma_local"] = (fl or {}).get("form") if isinstance(fl, dict) else None
    out["forma_visita"] = (fv or {}).get("form") if isinstance(fv, dict) else None

    def _riesgo(d: Any) -> List[str]:
        if not isinstance(d, dict):
            return []
        nombres = []
        for pl in d.get("at_risk", []) or []:
            nombre = pl.get("player") if isinstance(pl, dict) else None
            if nombre:
                nombres.append(nombre)
        return nombres

    out["en_riesgo_local"] = _riesgo(_safe(lambda: disciplina_equipo(hid)))
    out["en_riesgo_visita"] = _riesgo(_safe(lambda: disciplina_equipo(aid)))

    h = _safe(lambda: h2h_resumen(hid, aid))
    if isinstance(h, dict) and h:
        out["h2h"] = h
    out["noticias"] = _safe(lambda: noticias_de_equipos([out["home"], out["away"]], limit=4), []) or []
    # Alineación confirmada (365Scores, ~1h antes): señal de "¿salió con suplentes?".
    out["alineacion"] = _safe(lambda: alineacion_de_partido(out["home"], out["away"]), None)
    # Jugadores a seguir del partido (/matches/{id}/players-to-watch).
    out["jugadores_seguir"] = _safe(
        lambda: jugadores_a_seguir_partido(out["home"], out["away"]),
        {"local": [], "visita": []},
    ) or {"local": [], "visita": []}
    return out
