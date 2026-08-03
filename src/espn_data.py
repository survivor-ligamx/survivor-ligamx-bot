#!/usr/bin/env python3
"""
espn_data.py — Ingesta de datos Liga MX desde la API pública de ESPN.

ESPN expone una API JSON **pública y gratuita** (site.api.espn.com) que NO
requiere key y NO es scraping de HTML: es el mismo feed que alimenta su sitio.
Para Liga MX (código de liga `mex.1`) entrega fixtures, marcadores finales y
tabla.

Este módulo:
- Baja resultados históricos (partidos jugados, con marcador) -> insumo para el
  modelo Poisson (fuerza de equipos).
- Baja fixtures próximos.
- Escribe `data/resultados_historicos.json` en el formato que espera
  `poisson_model.calcular_fuerzas`: home_team, away_team, home_goals, away_goals.

Sin scraping, sin bypass, sin credenciales. NO cierra ni envía picks.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

try:
    import requests
except ImportError:  # pragma: no cover - dependencia opcional ausente
    requests = None  # type: ignore[assignment]

BASE_DIR = Path(__file__).resolve().parents[1]
RESULTADOS_PATH = BASE_DIR / "data" / "resultados_historicos.json"

LIGA_CODE = "mex.1"
SCOREBOARD_URL = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{LIGA_CODE}/scoreboard"

ESTADO_FINAL = "STATUS_FULL_TIME"


def _fetch_scoreboard(rango_fechas: str = "") -> Dict[str, Any]:
    """
    Trae el scoreboard de ESPN. `rango_fechas` opcional: 'YYYYMMDD' o
    'YYYYMMDD-YYYYMMDD'. Sin rango, devuelve la jornada actual.
    """
    if requests is None:
        raise RuntimeError("La dependencia 'requests' no está instalada.")
    params = {"dates": rango_fechas} if rango_fechas else {}
    resp = requests.get(SCOREBOARD_URL, params=params, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"ESPN respondió HTTP {resp.status_code}.")
    return cast(Dict[str, Any], resp.json())


def _marcador_entero(valor: Any) -> Optional[int]:
    """Convierte scores enteros de ESPN sin truncar valores fraccionarios."""
    if isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numero) or numero < 0 or not numero.is_integer():
        return None
    return int(numero)


def parsear_eventos(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Convierte la respuesta de ESPN en partidos limpios. Función pura (sin red).

    Devuelve dicts con: fecha, home_team, away_team, estado, jugado y, si está
    jugado, home_goals / away_goals (int).
    """
    partidos: List[Dict[str, Any]] = []
    for ev in data.get("events", []):
        if not isinstance(ev, dict):
            continue
        comps = ev.get("competitions") or [{}]
        competidores = comps[0].get("competitors", []) if comps else []
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
        jugado = estado == ESTADO_FINAL
        partido: Dict[str, Any] = {
            "fecha": str(ev.get("date", ""))[:10],
            "home_team": home,
            "away_team": away,
            "estado": estado,
            "jugado": jugado,
        }
        if jugado:
            home_goals = _marcador_entero(hg)
            away_goals = _marcador_entero(ag)
            if home_goals is None or away_goals is None:
                partido["jugado"] = False
            else:
                partido["home_goals"] = home_goals
                partido["away_goals"] = away_goals
        partidos.append(partido)
    return partidos


def _rangos_meses_atras(meses: int, hoy: Optional[datetime] = None) -> List[str]:
    """Genera rangos mensuales 'YYYYMMDD-YYYYMMDD' de los últimos `meses`."""
    hoy = hoy or datetime.now(timezone.utc)
    rangos: List[str] = []
    cursor = hoy
    for _ in range(max(1, meses)):
        primero = cursor.replace(day=1)
        fin = cursor
        rangos.append(f"{primero.strftime('%Y%m%d')}-{fin.strftime('%Y%m%d')}")
        cursor = primero - timedelta(days=1)
    return rangos


def _rangos_dias_adelante(dias: int, hoy: Optional[datetime] = None) -> List[str]:
    """Genera rangos de ~30 días 'YYYYMMDD-YYYYMMDD' hacia ADELANTE (próximos `dias`)."""
    hoy = hoy or datetime.now(timezone.utc)
    rangos: List[str] = []
    inicio = hoy
    restantes = max(1, dias)
    while restantes > 0:
        bloque = min(30, restantes)
        fin = inicio + timedelta(days=bloque)
        rangos.append(f"{inicio.strftime('%Y%m%d')}-{fin.strftime('%Y%m%d')}")
        inicio = fin + timedelta(days=1)
        restantes -= bloque + 1
    return rangos


def obtener_resultados(meses: int = 6) -> List[Dict[str, Any]]:
    """
    Baja partidos JUGADOS (con marcador) de los últimos `meses`, deduplicados.
    Formato listo para poisson_model.calcular_fuerzas.
    """
    vistos = set()
    resultados: List[Dict[str, Any]] = []
    for rango in _rangos_meses_atras(meses):
        try:
            data = _fetch_scoreboard(rango)
        except RuntimeError:
            continue
        for p in parsear_eventos(data):
            if not p.get("jugado"):
                continue
            clave = (p["home_team"], p["away_team"], p["fecha"])
            if clave in vistos:
                continue
            vistos.add(clave)
            resultados.append(
                {
                    "home_team": p["home_team"],
                    "away_team": p["away_team"],
                    "home_goals": p["home_goals"],
                    "away_goals": p["away_goals"],
                    "fecha": p["fecha"],
                }
            )
    return resultados


def obtener_fixtures() -> List[Dict[str, Any]]:
    """Devuelve los partidos próximos (no jugados) del scoreboard actual."""
    data = _fetch_scoreboard()
    return [p for p in parsear_eventos(data) if not p.get("jugado")]


def obtener_fixtures_proxima_jornada(dias: int = 25) -> List[Dict[str, Any]]:
    """
    Partidos de la PRÓXIMA jornada COMPLETA (no solo el scoreboard recortado de
    ESPN, que a veces trae 1-2 juegos). Baja fixtures futuros y toma el primer
    grupo cronológico, cerrando la jornada cuando un equipo se REPITE, hay un
    HUECO de fechas > 3 días, o ya se juntaron 9 (Liga MX: 18 equipos).
    """
    fixtures = obtener_fixtures_futuros(dias)
    if not fixtures:
        return []

    def _a_fecha(s: Any):
        try:
            y, m, d = str(s or "")[:10].split("-")
            return date(int(y), int(m), int(d))
        except (ValueError, TypeError):
            return None

    validos = []
    for fx in fixtures:
        f = _a_fecha(fx.get("fecha"))
        if f and fx.get("home_team") and fx.get("away_team"):
            validos.append((f, fx))
    validos.sort(key=lambda t: t[0])

    jornada: List[Dict[str, Any]] = []
    equipos: set = set()
    fecha_prev: Optional[date] = None
    for f, fx in validos:
        h, a = fx["home_team"], fx["away_team"]
        repite = h.lower() in equipos or a.lower() in equipos
        hueco = fecha_prev is not None and (f - fecha_prev).days > 3
        lleno = len(jornada) >= 9
        if jornada and (repite or hueco or lleno):
            break
        jornada.append({"home_team": h, "away_team": a, "fecha": fx.get("fecha", "")})
        equipos |= {h.lower(), a.lower()}
        fecha_prev = f
    return jornada


def obtener_fixtures_futuros(dias: int = 160) -> List[Dict[str, Any]]:
    """
    Baja partidos PROGRAMADOS (no jugados) de los próximos `dias`, deduplicados.
    Útil para construir el calendario completo de la temporada cuando ESPN ya lo
    publicó. Devuelve dicts con fecha/home_team/away_team. Sin red => [].
    """
    vistos = set()
    fixtures: List[Dict[str, Any]] = []
    for rango in _rangos_dias_adelante(dias):
        try:
            data = _fetch_scoreboard(rango)
        except RuntimeError:
            continue
        for p in parsear_eventos(data):
            if p.get("jugado"):
                continue
            clave = (p["home_team"], p["away_team"], p["fecha"])
            if clave in vistos:
                continue
            vistos.add(clave)
            fixtures.append(
                {
                    "home_team": p["home_team"],
                    "away_team": p["away_team"],
                    "fecha": p["fecha"],
                }
            )
    return fixtures


def guardar_resultados(resultados: List[Dict[str, Any]], path: Path = RESULTADOS_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(resultados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingesta de resultados Liga MX (ESPN).")
    parser.add_argument("--meses", type=int, default=6, help="Meses de histórico a bajar.")
    parser.add_argument("--output", default=str(RESULTADOS_PATH))
    args = parser.parse_args()

    print(f"📥 Bajando resultados Liga MX de ESPN (últimos {args.meses} meses)...")
    try:
        resultados = obtener_resultados(args.meses)
    except RuntimeError as exc:
        print(f"⚠️ No se pudo consultar ESPN: {exc}")
        return 1

    guardar_resultados(resultados, Path(args.output))
    print(f"✅ {len(resultados)} partidos jugados guardados → {args.output}")
    if resultados:
        ej = resultados[0]
        print(f"   Ej.: {ej['home_team']} {ej['home_goals']}-{ej['away_goals']} {ej['away_team']} ({ej['fecha']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
