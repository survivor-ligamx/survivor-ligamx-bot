#!/usr/bin/env python3
"""
fuentes_datos.py — Capa multi-fuente con redundancia para datos Liga MX.

Objetivo: que el proyecto NO dependa de una sola fuente. Cadena de respaldo:
    1) ESPN (site.api.espn.com)        — primaria, rica (resultados completos).
    2) TheSportsDB (free key)          — respaldo si ESPN falla.
    3) Caché local (resultados_historicos.json) — último recurso si todo falla.

Todas son APIs públicas/gratuitas. Sin scraping, sin bypass, sin credenciales
privadas. Devuelve resultados en el formato del modelo Poisson
(home_team, away_team, home_goals, away_goals, fecha).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, cast, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from src import espn_data

from src import ligamx_api
from src.team_normalizer import canonical_team_key

BASE_DIR = Path(__file__).resolve().parents[1]
CACHE_PATH = BASE_DIR / "data" / "resultados_historicos.json"

# TheSportsDB: key pública gratuita "3", Liga MX league id 4350.
TSDB_KEY = "3"
TSDB_LIGAMX_ID = "4350"
TSDB_URL = f"https://www.thesportsdb.com/api/v1/json/{TSDB_KEY}/eventspastleague.php"

# Mínimo de partidos para considerar una fuente "suficiente".
MIN_ACEPTABLE = 10


def parsear_thesportsdb(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parser puro de la respuesta de TheSportsDB (eventos pasados con marcador)."""
    salida: List[Dict[str, Any]] = []
    for e in data.get("events") or []:
        if not isinstance(e, dict):
            continue
        home = e.get("strHomeTeam")
        away = e.get("strAwayTeam")
        hs = e.get("intHomeScore")
        as_ = e.get("intAwayScore")
        if not home or not away or hs is None or as_ is None:
            continue
        try:
            hg, ag = int(hs), int(as_)
        except (TypeError, ValueError):
            continue
        salida.append(
            {
                "home_team": home,
                "away_team": away,
                "home_goals": hg,
                "away_goals": ag,
                "fecha": e.get("dateEvent", ""),
            }
        )
    return salida


def _fetch_thesportsdb() -> Dict[str, Any]:
    if requests is None:
        raise RuntimeError("La dependencia 'requests' no está instalada.")
    resp = requests.get(TSDB_URL, params={"id": TSDB_LIGAMX_ID}, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"TheSportsDB respondió HTTP {resp.status_code}.")
    return cast(Dict[str, Any], resp.json())


def obtener_resultados_thesportsdb() -> List[Dict[str, Any]]:
    return parsear_thesportsdb(_fetch_thesportsdb())


def leer_cache(path: Path = CACHE_PATH) -> List[Dict[str, Any]]:
    if not Path(path).exists():
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def guardar_cache(resultados: List[Dict[str, Any]], path: Path = CACHE_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(resultados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def obtener_resultados(meses: int = 6, minimo: int = MIN_ACEPTABLE) -> Dict[str, Any]:
    """
    Devuelve resultados con redundancia: {fuente, resultados, total}.

    Cadena: (Liga MX API, si LIGAMX_API_AS_SOURCE está activo) -> ESPN ->
    TheSportsDB -> caché local. La fuente que tenga suficientes datos gana; el
    resultado elegido se cachea para futuros respaldos.
    """
    # 0) Liga MX API (opt-in): fuente curada del proyecto hermano. Solo si está
    #    habilitada por entorno Y ya tiene partidos jugados (en pretemporada da []).
    if ligamx_api.usar_como_fuente():
        try:
            lmx = ligamx_api.resultados_historicos()
        except Exception:
            lmx = []
        if len(lmx) >= minimo:
            guardar_cache(lmx)
            return {"fuente": "LigaMX-API", "resultados": lmx, "total": len(lmx)}

    # 1) ESPN (primaria)
    try:
        espn = espn_data.obtener_resultados(meses)
    except Exception:
        espn = []
    if len(espn) >= minimo:
        guardar_cache(espn)
        return {"fuente": "ESPN", "resultados": espn, "total": len(espn)}

    # 2) TheSportsDB (respaldo)
    try:
        tsdb = obtener_resultados_thesportsdb()
    except Exception:
        tsdb = []
    # Combinar ESPN parcial + TheSportsDB (dedup por equipos+fecha).
    combinado = _combinar(espn, tsdb)
    if combinado:
        guardar_cache(combinado)
        fuente = "ESPN+TheSportsDB" if espn else "TheSportsDB"
        return {"fuente": fuente, "resultados": combinado, "total": len(combinado)}

    # 3) Caché local (último recurso)
    cache = leer_cache()
    return {"fuente": "cache", "resultados": cache, "total": len(cache)}


def obtener_historico_largo(min_espn_meses: int = 18, minimo: int = MIN_ACEPTABLE) -> Dict[str, Any]:
    """
    Historial LARGO (varias temporadas) para backtest y calibración, donde tener
    MUCHOS torneos hace el veredicto confiable.

    Prefiere la **Liga MX API** (proyecto hermano), que trae temporadas
    backfilleadas desde ~2022 (~1200+ partidos). Si falla o trae poco, cae a
    ESPN (~18 meses) y luego a la caché local. NO mezcla fuentes: usa la que
    tenga MÁS datos, para no duplicar partidos por diferencias de nombres entre
    fuentes (lo que corrompería la estimación de fuerzas del modelo).
    """
    try:
        lmx = ligamx_api.resultados_historicos(max_partidos=5000)
    except Exception:
        lmx = []
    try:
        espn = espn_data.obtener_resultados(min_espn_meses)
    except Exception:
        espn = []
    if len(lmx) >= minimo and len(lmx) >= len(espn):
        guardar_cache(lmx)
        return {"fuente": "LigaMX-API", "resultados": lmx, "total": len(lmx)}
    if len(espn) >= minimo:
        guardar_cache(espn)
        return {"fuente": "ESPN", "resultados": espn, "total": len(espn)}
    cache = leer_cache()
    return {"fuente": "cache", "resultados": cache, "total": len(cache)}


def _fecha_clave(valor: Any) -> str:
    """Normaliza la parte de fecha compartida por las fuentes."""
    return str(valor or "").strip()[:10]


def _combinar(a: List[Dict[str, Any]], b: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Combina preservando prioridad de ``a`` y deduplicando aliases."""
    vistos = set()
    out: List[Dict[str, Any]] = []
    for fuente in (a, b):
        for r in fuente:
            clave = (
                canonical_team_key(str(r.get("home_team") or "")),
                canonical_team_key(str(r.get("away_team") or "")),
                _fecha_clave(r.get("fecha")),
            )
            if clave in vistos:
                continue
            vistos.add(clave)
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Healthcheck de fuentes (para detectar caídas antes de la jornada).
# ---------------------------------------------------------------------------
def _ping(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 8) -> Dict[str, Any]:
    """Hace un GET ligero y reporta {ok, http, ms} o {ok:False, error}."""
    if requests is None:
        return {"ok": False, "error": "requests no instalado"}
    try:
        t0 = time.monotonic()
        resp = requests.get(url, params=params or {}, timeout=timeout)
        ms = int((time.monotonic() - t0) * 1000)
        return {"ok": resp.status_code == 200, "http": resp.status_code, "ms": ms}
    except Exception as exc:  # pragma: no cover - depende de red
        return {"ok": False, "error": str(exc)[:100]}


def estado_fuentes() -> Dict[str, Any]:
    """
    Estado de las fuentes de datos (ESPN, TheSportsDB, odds-api.io).
    odds-api.io solo se prueba si hay ODDS_API_IO_KEY. Devuelve un dict con el
    estado por fuente y `ok_global` (True si al menos una de datos responde).
    """
    espn = _ping(espn_data.SCOREBOARD_URL)
    tsdb = _ping(TSDB_URL, {"id": TSDB_LIGAMX_ID})

    odds_key = os.getenv("ODDS_API_IO_KEY", "").strip()
    if odds_key:
        base = os.getenv("ODDS_API_IO_URL", "https://api.odds-api.io/v3")
        odds = _ping(f"{base}/sports", {"apiKey": odds_key})
    else:
        odds = {"ok": None, "estado": "deshabilitado (sin ODDS_API_IO_KEY)"}

    # Liga MX API (proyecto hermano): fuente del calendario para el planificador.
    ligamx_base = os.getenv("LIGAMX_API_URL", "https://ligamx-api.onrender.com").strip().rstrip("/")
    ligamx = _ping(f"{ligamx_base}/health")

    # IA opcional (Groq): estado por presencia de key (sin llamada de red).
    try:
        from src import analista_ia

        groq = (
            {"ok": True, "estado": "habilitado (IA opcional)"}
            if analista_ia.habilitado()
            else {"ok": None, "estado": "deshabilitado (sin GROQ_API_KEY)"}
        )
    except Exception:  # pragma: no cover
        groq = {"ok": None, "estado": "no disponible"}

    return {
        "fuentes": {"espn": espn, "thesportsdb": tsdb, "odds_api_io": odds, "ligamx_api": ligamx, "groq_ia": groq},
        "ok_global": bool(espn.get("ok") or tsdb.get("ok")),
        "decision": "INFORMATIVO / REVISIÓN HUMANA",
    }


if __name__ == "__main__":
    res = obtener_resultados()
    print(f"Fuente usada: {res['fuente']} | resultados: {res['total']}")
