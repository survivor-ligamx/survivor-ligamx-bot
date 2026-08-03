#!/usr/bin/env python3
"""
comparador_mercado.py — Capa OPCIONAL de comparación modelo vs mercado.

El modelo (ESPN + Poisson) es la FUENTE DE VERDAD. Esta capa es un extra
informativo: si hay una API de momios configurada, baja las cuotas reales del
mercado y las compara con las probabilidades del modelo para señalar:

- 1X2: favorito y dónde el modelo ve "valor" (diferencia a su favor).
- Over/Under: si el mercado ve el partido EXPLOSIVO (Over barato / favorito el
  Over) o CAUTELOSO (Under), y valor en Over/Under.
- Hándicap (asiático): qué tan FAVORITO es un equipo según la línea.
- Empate: se muestra como referencia, marcado NO accionable para Survivor.

Gating: sin key (`ODDS_API_IO_KEY`) TODO queda en no-op y las predicciones
pasan sin cambios. NUNCA dice "apuesta"; solo marca diferencias para revisión
humana. Fuente: odds-api.io (tier gratis, cubre Liga MX). Sin scraping.
"""

from __future__ import annotations

import json
import os
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, cast
import logging

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

# Persistencia de momios en disco (caché reutilizable por el pick y el plan).
BASE_DIR = Path(__file__).resolve().parents[1]
MOMIOS_PATH = BASE_DIR / "data" / "momios.json"
# Antigüedad máxima (horas) de los momios guardados para seguir usándolos.
MOMIOS_MAX_EDAD_HORAS = float(os.getenv("MOMIOS_MAX_EDAD_HORAS", "72"))

# Pinnacle (API "guest", GRATIS y sin key de paga): la casa más afilada, cubre
# Liga MX y da 1X2 + totales. La key se inyecta SOLO por env (PINNACLE_API_KEY);
# sin ella no hay fallback y la fuente falla seguro (no-op).
PINNACLE_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
PINNACLE_KEY = os.getenv("PINNACLE_API_KEY", "").strip()
PINNACLE_SOCCER_ID = int(os.getenv("PINNACLE_SOCCER_ID", "29"))
# Nombre de la liga en Pinnacle (aparece cuando ya hay partidos con líneas).
PINNACLE_LIGA = os.getenv("PINNACLE_LIGA", "Mexico - Liga MX")

# ESPN como fuente de momios GRATIS y sin key (misma familia que ya usamos).
# El scoreboard da la jornada próxima; el core-API da el 1X2 completo (home/draw/
# away moneyline en americano) + total O/U. Proveedor tipo DraftKings.
ESPN_LIGA = os.getenv("ESPN_ODDS_LIGA", "mex.1")
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{liga}/scoreboard"
ESPN_ODDS_URL = "https://sports.core.api.espn.com/v2/sports/soccer/leagues/{liga}/events/{eid}/competitions/{cid}/odds"

# --- Configuración (todo por env, apagado por defecto) ---------------------
ENV_KEY = "ODDS_API_IO_KEY"
BASE_URL = os.getenv("ODDS_API_IO_URL", "https://api.odds-api.io/v3")
# Slug de Liga MX en odds-api.io (confirmado: "mexico-liga-mx-apertura").
LIGA_SLUG = os.getenv("ODDS_API_IO_LIGA", "mexico-liga-mx-apertura")
SPORT_SLUG = os.getenv("ODDS_API_IO_SPORT", "football")
# Tope de partidos a consultar por ciclo (cuida el límite del tier gratis).
MAX_EVENTOS = int(os.getenv("ODDS_API_IO_MAX_EVENTOS", "20"))
# Ventana de fechas hacia adelante (días). Liga MX puede arrancar en >14 días
# (el default de la API es 14), así que la ampliamos para captar la próxima
# jornada en pretemporada.
VENTANA_DIAS = int(os.getenv("ODDS_API_IO_VENTANA_DIAS", "120"))
# Casas a consultar (la API exige el parámetro `bookmakers` en /odds, máx 30).
# Lista de casas globales que suelen cubrir Liga MX; se intersecta con las
# casas ACTIVAS reales de la API. Override con ODDS_API_IO_BOOKMAKERS.
BOOKMAKERS_PRIORIDAD = [
    "Bet365",
    "Pinnacle",
    "1xBet",
    "Unibet",
    "William Hill",
    "Betfair",
    "Bwin",
    "888sport",
    "Betsson",
    "Marathonbet",
    "Betway",
    "Dafabet",
    "Betano",
    "Codere",
    "Caliente",
    "Betcris",
    "Betsafe",
    "Sportingbet",
    "10Bet",
    "22Bet",
    "Megapari",
    "1win",
    "Betobet",
    "Parimatch",
    "Pinnacle Sports",
    "Stake",
    "BetWinner",
    "Melbet",
    "888",
    "Vbet",
]
_BOOKMAKERS_OVERRIDE = os.getenv("ODDS_API_IO_BOOKMAKERS", "").strip()
# Máximo de casas por request. El tier gratis de odds-api.io permite 2.
MAX_CASAS = int(os.getenv("ODDS_API_IO_MAX_CASAS", "2"))
# Candidatas a sondear para AUTO-seleccionar las casas con odds de Liga MX
# (LATAM/México primero, luego globales). Se intersecta con las activas.
CANDIDATOS_AUTO = [
    "Caliente",
    "Betcris",
    "Codere",
    "Betano",
    "Bet365",
    "1xBet",
    "Betsson",
    "Bwin",
    "Pinnacle",
    "Stake",
    "Betway",
    "William Hill",
    "Unibet",
    "bet-at-home",
]
# Cada cuánto se re-sondea qué casas tienen odds de Liga MX (horas).
CASAS_AUTO_TTL_HORAS = float(os.getenv("ODDS_API_IO_CASAS_TTL_HORAS", "6"))

# Diferencia mínima (proporción) para marcar "valor" del modelo vs mercado.
UMBRAL_VALOR = 0.05
# Línea de goles de referencia del modelo.
LINEA_GOLES = 2.5

DISCLAIMER = "ℹ️ Informativo / revisión humana. No es consejo de apuesta."


def _norm(texto: str) -> str:
    base = unicodedata.normalize("NFKD", str(texto or "")).lower()
    base = "".join(c for c in base if not unicodedata.combining(c))
    return " ".join(base.split())


def mercado_habilitado() -> bool:
    """True solo si hay key configurada para la API de momios."""
    return bool(os.getenv(ENV_KEY, "").strip())


# ---------------------------------------------------------------------------
# Matemática pura: quitar vig y comparar. Esto es lo testeable y útil.
# ---------------------------------------------------------------------------
def quitar_vig(momio_local: float, momio_empate: float, momio_visita: float) -> Dict[str, float]:
    """Momios decimales 1X2 -> probabilidades implícitas SIN vig (+ vig)."""
    momios = [float(momio_local), float(momio_empate), float(momio_visita)]
    if any(m <= 1.0 for m in momios):
        raise ValueError("Los momios decimales deben ser > 1.0.")
    implicitas = [1.0 / m for m in momios]
    suma = sum(implicitas)
    return {
        "prob_local": implicitas[0] / suma,
        "prob_empate": implicitas[1] / suma,
        "prob_visita": implicitas[2] / suma,
        "vig": suma - 1.0,
    }


def quitar_vig_2(momio_a: float, momio_b: float) -> Dict[str, float]:
    """Mercado de 2 vías (ej. Over/Under) -> probabilidades sin vig (+ vig)."""
    a, b = float(momio_a), float(momio_b)
    if a <= 1.0 or b <= 1.0:
        raise ValueError("Los momios decimales deben ser > 1.0.")
    ia, ib = 1.0 / a, 1.0 / b
    suma = ia + ib
    return {"prob_a": ia / suma, "prob_b": ib / suma, "vig": suma - 1.0}


def comparar_1x2(
    prob_modelo: Sequence[float],
    momio_local: float,
    momio_empate: float,
    momio_visita: float,
    *,
    umbral: float = UMBRAL_VALOR,
) -> Dict[str, Any]:
    """
    Compara el 1X2 del modelo vs mercado (sin vig). Marca el favorito del
    mercado y dónde el modelo ve valor. El empate se marca NO accionable.
    `prob_modelo` = [local, empate, visita] (acepta % o proporción).
    """
    pmod = [float(x) for x in prob_modelo]
    if len(pmod) != 3:
        raise ValueError("prob_modelo debe tener 3 valores (1X2).")
    if sum(pmod) > 1.5:
        pmod = [x / 100.0 for x in pmod]

    mercado = quitar_vig(momio_local, momio_empate, momio_visita)
    pmkt = [mercado["prob_local"], mercado["prob_empate"], mercado["prob_visita"]]
    etiquetas = ["local", "empate", "visita"]

    favorito = etiquetas[max(range(3), key=lambda i: pmkt[i])]
    diffs = [round(pmod[i] - pmkt[i], 4) for i in range(3)]
    mejor_i = max(range(3), key=lambda i: diffs[i])
    hay_valor = diffs[mejor_i] >= umbral
    valor_en = etiquetas[mejor_i] if hay_valor else None

    return {
        "favorito_mercado": favorito,
        "momios": {
            "local": round(float(momio_local), 2),
            "empate": round(float(momio_empate), 2),
            "visita": round(float(momio_visita), 2),
        },
        "prob_modelo_pct": [round(p * 100, 2) for p in pmod],
        "prob_mercado_pct": [round(p * 100, 2) for p in pmkt],
        "vig_pct": round(mercado["vig"] * 100, 2),
        "diff_pct": [round(d * 100, 2) for d in diffs],
        "valor_en": valor_en,
        "hay_valor": hay_valor,
        "empate_accionable": False,  # en Survivor el empate no se elige
    }


def comparar_totales(
    prob_over_modelo: float,
    momio_over: float,
    momio_under: float,
    linea: float = LINEA_GOLES,
    *,
    umbral: float = UMBRAL_VALOR,
) -> Dict[str, Any]:
    """
    Compara Over/Under del modelo vs mercado. Indica si el mercado ve el partido
    EXPLOSIVO (favorece Over) o CAUTELOSO (favorece Under), y dónde hay valor.
    `prob_over_modelo` en % o proporción.
    """
    p_over = float(prob_over_modelo)
    if p_over > 1.5:
        p_over /= 100.0
    mkt = quitar_vig_2(momio_over, momio_under)
    p_over_mkt = mkt["prob_a"]

    diff = round(p_over - p_over_mkt, 4)
    if abs(diff) >= umbral:
        valor_en = "Over" if diff > 0 else "Under"
    else:
        valor_en = None

    return {
        "linea": linea,
        "mercado_ve": "explosivo" if p_over_mkt >= 0.5 else "cauteloso",
        "momios": {"over": round(float(momio_over), 2), "under": round(float(momio_under), 2)},
        "prob_over_modelo_pct": round(p_over * 100, 2),
        "prob_over_mercado_pct": round(p_over_mkt * 100, 2),
        "diff_over_pct": round(diff * 100, 2),
        "vig_pct": round(mkt["vig"] * 100, 2),
        "valor_en": valor_en,
        "hay_valor": valor_en is not None,
    }


def resumen_handicap(linea: float, momio_local: float, momio_visita: float) -> Dict[str, Any]:
    """
    Interpreta el hándicap asiático (línea desde la perspectiva del local).
    Línea negativa => el local es favorito (da goles). Magnitud alta => muy
    favorito. Informativo.
    """
    ln = float(linea)
    favorito = "local" if ln < 0 else ("visitante" if ln > 0 else "parejo")
    magnitud = abs(ln)
    if magnitud >= 1.5:
        fuerza = "muy favorito"
    elif magnitud >= 0.75:
        fuerza = "favorito claro"
    elif magnitud > 0:
        fuerza = "ligero favorito"
    else:
        fuerza = "parejo"
    return {
        "linea": ln,
        "favorito": favorito,
        "fuerza": fuerza,
        "momio_local": round(float(momio_local), 2),
        "momio_visita": round(float(momio_visita), 2),
    }


def anotar_pronostico(pron: Dict[str, Any], mercado: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Devuelve una COPIA del pronóstico con un bloque 'mercado' (1x2 + totales +
    handicap) si hay momios; si no, mercado=None.
    `mercado` = salida de parsear_mercado: {ml, totals, handicap}.
    """
    salida = dict(pron)
    if not mercado:
        salida["mercado"] = None
        return salida

    bloque: Dict[str, Any] = {"decision": DISCLAIMER}
    try:
        ml = mercado.get("ml")
        if ml:
            bloque["1x2"] = comparar_1x2(
                [pron["prob_local_pct"], pron["prob_empate_pct"], pron["prob_visitante_pct"]],
                ml["local"],
                ml["empate"],
                ml["visita"],
            )
    except (KeyError, ValueError, TypeError):
        logger.debug("Exception silenciada en anotar_pronostico", exc_info=True)
    try:
        tot = mercado.get("totals")
        if tot and "prob_over_pct" in pron:
            bloque["over_under"] = comparar_totales(
                pron["prob_over_pct"],
                tot["over"],
                tot["under"],
                tot.get("linea", LINEA_GOLES),
            )
    except (KeyError, ValueError, TypeError):
        logger.debug("Exception silenciada en anotar_pronostico", exc_info=True)
    try:
        hcp = mercado.get("handicap")
        if hcp:
            bloque["handicap"] = resumen_handicap(hcp["linea"], hcp["local"], hcp["visita"])
    except (KeyError, ValueError, TypeError):
        logger.debug("Exception silenciada en anotar_pronostico", exc_info=True)

    salida["mercado"] = bloque if len(bloque) > 1 else None
    return salida


def pronostico_desde_momios(
    home: str, away: str, mercado: Optional[Dict[str, Any]], fecha: str = ""
) -> Optional[Dict[str, Any]]:
    """
    Construye un pronóstico SOLO con los momios del mercado, para partidos que el
    modelo no puede pronosticar (equipo sin histórico, p.ej. un recién ascendido
    como Atlante). Devuelve None si no hay momios 1X2 (`ml`) utilizables.

    Las probabilidades salen de quitar el vig al 1X2. Se marca claramente como
    'solo mercado' para no confundirlo con el pronóstico del modelo. Over/Under se
    incluye si el mercado trae 'totals'. No hay marcador exacto (los momios no lo dan).
    """
    ml = (mercado or {}).get("ml")
    if not ml:
        return None
    try:
        v = quitar_vig(ml["local"], ml["empate"], ml["visita"])
    except (KeyError, ValueError, TypeError):
        return None
    pl = round(v["prob_local"] * 100, 2)
    pe = round(v["prob_empate"] * 100, 2)
    pv = round(v["prob_visita"] * 100, 2)
    pick = max((("Gana Local", pl), ("Empate", pe), ("Gana Visitante", pv)), key=lambda x: x[1])[0]
    pron: Dict[str, Any] = {
        "local": home,
        "visitante": away,
        "fecha": fecha,
        "pick_1x2": pick,
        "prob_local_pct": pl,
        "prob_empate_pct": pe,
        "prob_visitante_pct": pv,
        "prob_pick_pct": max(pl, pe, pv),
        "nivel_confianza": "SOLO MERCADO",
        "no_perder_local_pct": round(pl + pe, 2),
        "no_perder_visitante_pct": round(pv + pe, 2),
        "precaucion": True,
        "nivel_alerta": "ℹ️ Solo mercado",
        "motivos_alerta": [
            "Sin histórico del equipo en el modelo (equipo nuevo); probabilidades tomadas de los momios."
        ],
        "explicacion_1x2": "El modelo no tiene datos de este equipo todavía; se usan los momios del mercado.",
        "fuente_pick": "mercado",
    }
    # Over/Under desde el mercado, si viene.
    tot = (mercado or {}).get("totals")
    try:
        if tot and tot.get("over") and tot.get("under"):
            ou = quitar_vig_2(tot["over"], tot["under"])
            pron["prob_over_pct"] = round(ou["prob_a"] * 100, 2)
            pron["prob_under_pct"] = round(ou["prob_b"] * 100, 2)
            pron["pick_ou"] = "Over" if ou["prob_a"] >= ou["prob_b"] else "Under"
    except (KeyError, ValueError, TypeError):
        logger.debug("Exception silenciada en pronostico_desde_momios", exc_info=True)
    return pron


def _clave_partido(local: str, visitante: str) -> str:
    return f"{_norm(local)}|{_norm(visitante)}"


_PALABRAS_COMUNES = {"club", "cf", "fc", "deportivo", "real", "atletico", "atletico", "de", "the"}


def _token_significativo(nombre: str) -> set:
    return {t for t in _norm(nombre).split() if len(t) >= 4 and t not in _PALABRAS_COMUNES}


def _equipos_coinciden(a: str, b: str) -> bool:
    """Empareja nombres de equipo de forma flexible (ESPN vs odds-api.io)."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return bool(_token_significativo(a) & _token_significativo(b))


def _buscar_mercado(home: str, away: str, momios: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Encuentra el mercado de un partido por clave exacta o por nombres flexibles."""
    clave = _clave_partido(home, away)
    if clave in momios:
        return momios[clave]
    for k, mercado in momios.items():
        h_key, _, a_key = k.partition("|")
        if _equipos_coinciden(home, h_key) and _equipos_coinciden(away, a_key):
            return mercado
    return None


def buscar_mercado_partido(home: str, away: str, momios: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """API pública: mercado de un partido (match flexible de nombres). None si no hay."""
    return _buscar_mercado(home, away, momios)


def anotar_pronosticos(
    pronosticos: Sequence[Dict[str, Any]],
    momios_por_partido: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Anota una lista de pronósticos con la comparación de mercado disponible."""
    momios_por_partido = momios_por_partido or {}
    salida = []
    for p in pronosticos:
        mercado = _buscar_mercado(p.get("local", ""), p.get("visitante", ""), momios_por_partido)
        salida.append(anotar_pronostico(p, mercado))
    return salida


def mezclar_pronosticos_con_mercado(
    pronosticos: Sequence[Dict[str, Any]],
    momios: Optional[Dict[str, Dict[str, Any]]] = None,
    peso_modelo: float = 0.5,
) -> List[Dict[str, Any]]:
    """
    Mezcla (ensemble) el 1X2 del MODELO con el del MERCADO (momios sin vig) en las
    probabilidades que usa el pick de Survivor. Devuelve COPIAS con prob 1X2,
    pick_1x2, no-perder y confianza recalculados. Over/Under y BTTS quedan del
    modelo (el Survivor se decide con el 1X2).

    Las casas suelen ser más afinadas que el modelo solo, así que subir un poco
    el acierto por jornada ayuda (y se compone semana a semana).

    `peso_modelo` en [0,1]: 0 = solo mercado, 1 = solo modelo, 0.5 = mitad/mitad.
    Sin `momios` (o sin ODDS_API_IO_KEY) => devuelve los pronósticos SIN cambios
    (no-op seguro: el pick sigue siendo el del modelo).
    """
    from src import poisson_model as pm

    if momios is None:
        if not mercado_habilitado():
            return [dict(p) for p in pronosticos]
        momios, _fuente = momios_para_uso()
    if not momios:
        return [dict(p) for p in pronosticos]

    salida: List[Dict[str, Any]] = []
    for p in pronosticos:
        q = dict(p)
        if "prob_local_pct" not in p:
            salida.append(q)
            continue
        mercado = buscar_mercado_partido(p.get("local", ""), p.get("visitante", ""), momios)
        ml = (mercado or {}).get("ml")
        try:
            dv = quitar_vig(ml["local"], ml["empate"], ml["visita"]) if ml else None
        except (KeyError, ValueError, TypeError):
            dv = None
        if not dv:
            salida.append(q)
            continue
        modelo = (p["prob_local_pct"] / 100.0, p["prob_empate_pct"] / 100.0, p["prob_visitante_pct"] / 100.0)
        mkt = (dv["prob_local"], dv["prob_empate"], dv["prob_visita"])
        try:
            mez = pm.combinar_con_mercado(modelo, mkt, peso_modelo=peso_modelo)
        except (ValueError, TypeError):
            salida.append(q)
            continue
        pl, pe, pv = round(mez[0] * 100, 2), round(mez[1] * 100, 2), round(mez[2] * 100, 2)
        prob_pick = max(pl, pe, pv)
        if pl >= pe and pl >= pv:
            pick = "Gana Local"
        elif pv >= pl and pv >= pe:
            pick = "Gana Visitante"
        else:
            pick = "Empate"
        nivel = "ALTA" if prob_pick >= 55.0 else ("MEDIA" if prob_pick >= 42.0 else "BAJA")
        q.update(
            {
                "prob_local_pct": pl,
                "prob_empate_pct": pe,
                "prob_visitante_pct": pv,
                "prob_pick_pct": round(prob_pick, 2),
                "pick_1x2": pick,
                "nivel_confianza": nivel,
                "no_perder_local_pct": round(pl + pe, 2),
                "no_perder_visitante_pct": round(pv + pe, 2),
                "mezcla_mercado": {"peso_modelo": peso_modelo, "vig": round(dv.get("vig", 0.0), 4)},
            }
        )
        salida.append(q)
    return salida


def construir_snapshots_momios(
    pronosticos: Sequence[Dict[str, Any]],
    momios: Optional[Dict[str, Dict[str, Any]]] = None,
    source: Optional[str] = None,
    season: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Arma snapshots de momios (1 por partido) listos para archivar en la Liga MX
    API (POST /odds). Cruza los pronósticos (que traen los NOMBRES) con el dict
    de momios (indexado por clave). Devuelve [] si no hay momios.
    """
    if not momios:
        return []
    out: List[Dict[str, Any]] = []
    for p in pronosticos:
        home, away = p.get("local"), p.get("visitante")
        if not home or not away:
            continue
        mercado = buscar_mercado_partido(home, away, momios)
        if not mercado:
            continue
        ml = mercado.get("ml") or {}
        tot = mercado.get("totals") or {}
        if not ml and not tot:
            continue
        snap: Dict[str, Any] = {"home_team": home, "away_team": away, "source": source, "season": season}
        if p.get("fecha"):
            snap["match_date"] = p.get("fecha")
        if ml:
            snap["odds_local"] = ml.get("local")
            snap["odds_empate"] = ml.get("empate")
            snap["odds_visita"] = ml.get("visita")
        if tot:
            snap["ou_linea"] = tot.get("linea")
            snap["odds_over"] = tot.get("over")
            snap["odds_under"] = tot.get("under")
        out.append(snap)
    return out


# ---------------------------------------------------------------------------
# Parseo del formato real de odds-api.io (función pura, defensiva).
# bookmakers = {casa: [ {name, odds:[{...}]}, ... ]}. Markets: ML, Over/Under,
# Asian Handicap, etc. Promediamos entre casas.
# ---------------------------------------------------------------------------
def _f(valor: Any) -> Optional[float]:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def parsear_mercado(odds_response: Any) -> Dict[str, Any]:
    """
    De la respuesta /odds extrae promedios de mercado:
        {ml: {local, empate, visita},
         totals: {linea, over, under},
         handicap: {linea, local, visita}}
    Promedia entre casas. Para totales prefiere la línea 2.5. Devuelve {} si no
    hay nada utilizable.
    """
    if not isinstance(odds_response, dict):
        return {}
    bookmakers = odds_response.get("bookmakers")
    if not isinstance(bookmakers, dict) or not bookmakers:
        return {}

    ml_h: List[float] = []
    ml_d: List[float] = []
    ml_a: List[float] = []
    # totales agrupados por línea: {linea: {"over": [...], "under": [...]}}
    tot: Dict[Any, Any] = defaultdict(lambda: {"over": [], "under": []})
    hdp_line: List[float] = []
    hdp_h: List[float] = []
    hdp_a: List[float] = []

    for markets in bookmakers.values():
        if not isinstance(markets, list):
            continue
        for m in markets:
            if not isinstance(m, dict):
                continue
            nombre = _norm(cast(Any, m.get("name")))
            odds_list = m.get("odds") or []
            if not isinstance(odds_list, list) or not odds_list:
                continue
            o = odds_list[0]
            if not isinstance(o, dict):
                continue
            if nombre == "ml":
                h, d, a = _f(o.get("home")), _f(o.get("draw")), _f(o.get("away"))
                if h and d and a and h > 1 and d > 1 and a > 1:
                    ml_h.append(h)
                    ml_d.append(d)
                    ml_a.append(a)
            elif nombre in ("over/under", "totals", "total goals"):
                linea = _f(o.get("max")) or _f(o.get("hdp")) or _f(o.get("line"))
                ov, un = _f(o.get("over")), _f(o.get("under"))
                if linea is not None and ov and un and ov > 1 and un > 1:
                    tot[round(linea, 2)]["over"].append(ov)
                    tot[round(linea, 2)]["under"].append(un)
            elif nombre in ("asian handicap", "handicap", "spread"):
                linea = _f(o.get("hdp"))
                h, a = _f(o.get("home")), _f(o.get("away"))
                if linea is not None and h and a and h > 1 and a > 1:
                    hdp_line.append(linea)
                    hdp_h.append(h)
                    hdp_a.append(a)

    prom = lambda xs: sum(xs) / len(xs)
    out: Dict[str, Any] = {}
    if ml_h:
        out["ml"] = {"local": prom(ml_h), "empate": prom(ml_d), "visita": prom(ml_a)}
    if tot:
        # Preferir la línea 2.5; si no, la línea con más casas.
        if 2.5 in tot and tot[2.5]["over"]:
            linea = 2.5
        else:
            linea = max(tot, key=lambda k: len(tot[k]["over"]))
        if tot[linea]["over"]:
            out["totals"] = {
                "linea": linea,
                "over": prom(tot[linea]["over"]),
                "under": prom(tot[linea]["under"]),
            }
    if hdp_line:
        out["handicap"] = {
            "linea": prom(hdp_line),
            "local": prom(hdp_h),
            "visita": prom(hdp_a),
        }
    return out


# ---------------------------------------------------------------------------
# Red (gated, defensiva): odds-api.io. Sin key => {} y todo queda en no-op.
# ---------------------------------------------------------------------------
def _get(url: str, params: Dict[str, Any]) -> Any:
    if requests is None:
        raise RuntimeError("La dependencia 'requests' no está instalada.")
    resp = requests.get(url, params=params, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"odds-api.io respondió HTTP {resp.status_code} en {url}.")
    return resp.json()


_BOOKMAKERS_CACHE: Optional[str] = None
_ACTIVAS_CACHE: Optional[List[str]] = None


def _casas_activas() -> List[str]:
    """Lista de nombres de casas ACTIVAS (endpoint /bookmakers, sin auth). Cacheada."""
    global _ACTIVAS_CACHE
    if _ACTIVAS_CACHE is not None:
        return _ACTIVAS_CACHE
    try:
        data = _get(f"{BASE_URL}/bookmakers", {})
        _ACTIVAS_CACHE = [
            cast(str, b.get("name")) for b in data if isinstance(b, dict) and b.get("active") and b.get("name")
        ]
    except RuntimeError:
        _ACTIVAS_CACHE = []
    return _ACTIVAS_CACHE


def _bookmakers_consulta() -> str:
    """
    Cadena de casas (máx MAX_CASAS) para el parámetro `bookmakers` de /odds.
    Override por env > intersección de la lista de prioridad con casas activas >
    relleno con otras activas. Cacheado.
    """
    global _BOOKMAKERS_CACHE
    if _BOOKMAKERS_OVERRIDE:
        return ",".join([b.strip() for b in _BOOKMAKERS_OVERRIDE.split(",") if b.strip()][:MAX_CASAS])
    if _BOOKMAKERS_CACHE is not None:
        return _BOOKMAKERS_CACHE
    activas = _casas_activas()
    if not activas:
        _BOOKMAKERS_CACHE = ",".join(BOOKMAKERS_PRIORIDAD[:MAX_CASAS])
        return _BOOKMAKERS_CACHE
    activas_norm = {_norm(n): n for n in activas}
    elegidas: List[str] = []
    for pref in BOOKMAKERS_PRIORIDAD:
        real = activas_norm.get(_norm(pref))
        if real and real not in elegidas:
            elegidas.append(real)
    for n in activas:  # rellenar con otras activas
        if len(elegidas) >= MAX_CASAS:
            break
        if n not in elegidas:
            elegidas.append(n)
    _BOOKMAKERS_CACHE = ",".join(elegidas[:MAX_CASAS])
    return _BOOKMAKERS_CACHE


def _listar_eventos(key: str) -> List[Dict[str, Any]]:
    """Eventos próximos de Liga MX (ventana ampliada, solo no jugados)."""
    hasta = (datetime.now(timezone.utc) + timedelta(days=VENTANA_DIAS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    eventos = _get(
        f"{BASE_URL}/events",
        {
            "apiKey": key,
            "sport": SPORT_SLUG,
            "league": LIGA_SLUG,
            "status": "pending",
            "to": hasta,
        },
    )
    if not isinstance(eventos, list):
        return []
    out = []
    for ev in eventos:
        if not isinstance(ev, dict):
            continue
        slug = _norm((ev.get("league") or {}).get("slug", ""))
        if LIGA_SLUG and slug and _norm(LIGA_SLUG) not in slug:
            continue
        if ev.get("id") is not None and ev.get("home") and ev.get("away"):
            out.append(ev)
    return out


def _odds_evento(key: str, ev_id: Any, bookmakers: str) -> Optional[Dict[str, Any]]:
    """Odds de UN evento (/odds individual; disponible en el plan gratis)."""
    data = _get(
        f"{BASE_URL}/odds",
        {
            "apiKey": key,
            "eventId": ev_id,
            "bookmakers": bookmakers,
        },
    )
    return data if isinstance(data, dict) else None


def _odds_multi(key: str, ids: List[Any], bookmakers: str) -> List[Dict[str, Any]]:
    """Odds para hasta 10 eventos en una llamada (/odds/multi; suele ser premium)."""
    if not ids:
        return []
    data = _get(
        f"{BASE_URL}/odds/multi",
        {
            "apiKey": key,
            "eventIds": ",".join(str(i) for i in ids[:10]),
            "bookmakers": bookmakers,
        },
    )
    return data if isinstance(data, list) else []


_CASAS_AUTO_CACHE: Dict[str, Any] = {"casas": None, "ts": None}


def casas_con_odds_liga(key: str) -> List[str]:
    """
    Auto-selecciona hasta MAX_CASAS casas que SÍ tienen odds de Liga MX ahora,
    sondeando /events?bookmaker= por cada candidata (intersectada con activas).
    Cacheado CASAS_AUTO_TTL_HORAS. Devuelve [] si ninguna tiene odds (p. ej.
    pretemporada).
    """
    ahora = datetime.now(timezone.utc)
    ts = _CASAS_AUTO_CACHE["ts"]
    if (
        _CASAS_AUTO_CACHE["casas"] is not None
        and ts is not None
        and ((ahora - ts).total_seconds() < CASAS_AUTO_TTL_HORAS * 3600)
    ):
        return cast(List[str], _CASAS_AUTO_CACHE["casas"])

    activas_norm = {_norm(n): n for n in _casas_activas()}
    candidatas = []
    for c in CANDIDATOS_AUTO:
        real = activas_norm.get(_norm(c))
        if real and real not in candidatas:
            candidatas.append(real)

    hasta = (ahora + timedelta(days=VENTANA_DIAS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conteo: Dict[str, int] = {}
    for casa in candidatas:
        try:
            evs = _get(
                f"{BASE_URL}/events",
                {
                    "apiKey": key,
                    "sport": SPORT_SLUG,
                    "league": LIGA_SLUG,
                    "bookmaker": casa,
                    "to": hasta,
                },
            )
            n = len(evs) if isinstance(evs, list) else 0
        except RuntimeError:
            n = 0
        if n > 0:
            conteo[casa] = n

    casas = sorted(conteo, key=lambda c: conteo[c], reverse=True)[:MAX_CASAS]
    _CASAS_AUTO_CACHE["casas"] = casas
    _CASAS_AUTO_CACHE["ts"] = ahora
    return casas


def _casas_objetivo(key: str) -> str:
    """Cadena de casas a consultar: override > auto-selección > fallback prioridad."""
    if _BOOKMAKERS_OVERRIDE:
        return _bookmakers_consulta()
    auto = casas_con_odds_liga(key)
    if auto:
        return ",".join(auto)
    return _bookmakers_consulta()  # fallback (probablemente sin datos aún)


def obtener_momios_liga_mx() -> Dict[str, Dict[str, Any]]:
    """
    Baja momios de Liga MX desde odds-api.io. Devuelve
    {clave_partido: {ml, totals, handicap}}. Sin key o ante cualquier fallo
    devuelve {} (no-op). Limita a MAX_EVENTOS por ciclo. Usa /odds individual
    (plan gratis); /odds/multi es premium y da 403 en el free tier.
    """
    if not mercado_habilitado():
        return {}
    key = os.getenv(ENV_KEY, "").strip()
    out: Dict[str, Dict[str, Any]] = {}
    try:
        eventos = _listar_eventos(key)[:MAX_EVENTOS]
        if not eventos:
            return {}
        bookmakers = _casas_objetivo(key)
        for ev in eventos:
            try:
                odds = _odds_evento(key, ev["id"], bookmakers)
            except RuntimeError:
                continue
            if not odds:
                continue
            home = odds.get("home") or ev.get("home", "")
            away = odds.get("away") or ev.get("away", "")
            mercado = parsear_mercado(odds)
            if mercado and home and away:
                out[_clave_partido(home, away)] = mercado
    except RuntimeError:
        return {}
    return out


# ---------------------------------------------------------------------------
# Persistencia: guardar/cargar momios en data/momios.json (caché reutilizable).
# ---------------------------------------------------------------------------
def guardar_momios(momios: Dict[str, Dict[str, Any]], path: Path = MOMIOS_PATH) -> str:
    """Guarda los momios (ml/totals/handicap por partido) con timestamp UTC."""
    payload = {
        "generado_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fuente": "odds-api.io",
        "partidos": len(momios),
        "momios": momios,
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def cargar_momios(max_edad_horas: float = MOMIOS_MAX_EDAD_HORAS, path: Path = MOMIOS_PATH) -> Dict[str, Dict[str, Any]]:
    """
    Carga los momios guardados. Devuelve {} si no existe, está corrupto, vacío o
    más viejo que `max_edad_horas` (0/None = sin límite de antigüedad).
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    momios = data.get("momios")
    if not isinstance(momios, dict) or not momios:
        return {}
    if max_edad_horas and max_edad_horas > 0:
        try:
            dt = datetime.strptime(str(data.get("generado_utc")), "%Y-%m-%dT%H:%M:%SZ")
            dt = dt.replace(tzinfo=timezone.utc)
            edad_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
            if edad_h > max_edad_horas:
                return {}
        except (ValueError, TypeError):
            logger.debug("Exception silenciada en cargar_momios", exc_info=True)
    return momios


def _americano_a_decimal(momio: Any) -> Optional[float]:
    """Momio americano (-110, +275) -> decimal (1.91, 3.75). None si inválido."""
    m = _f(momio)
    if m is None or m == 0:
        return None
    return (m / 100.0 + 1.0) if m > 0 else (100.0 / (-m) + 1.0)


def _parsear_odds_espn(items: Any) -> Dict[str, Any]:
    """De los items del core-API de ESPN saca {ml, totals} (decimales)."""
    if not isinstance(items, list):
        return {}
    for it in items:
        if not isinstance(it, dict):
            continue
        h = _americano_a_decimal((it.get("homeTeamOdds") or {}).get("moneyLine"))
        a = _americano_a_decimal((it.get("awayTeamOdds") or {}).get("moneyLine"))
        d = _americano_a_decimal((it.get("drawOdds") or {}).get("moneyLine"))
        if not (h and d and a):
            continue  # necesitamos el 1X2 completo
        out: Dict[str, Any] = {"ml": {"local": h, "empate": d, "visita": a}}
        total = it.get("total") or {}
        linea = _f(it.get("overUnder"))
        ov = total.get("over") or {}
        un = total.get("under") or {}
        ov_odds = _americano_a_decimal((ov.get("close") or ov.get("open") or {}).get("odds"))
        un_odds = _americano_a_decimal((un.get("close") or un.get("open") or {}).get("odds"))
        if linea is not None and ov_odds and un_odds:
            out["totals"] = {"linea": round(linea, 2), "over": ov_odds, "under": un_odds}
        return out
    return {}


def _get_pinnacle(path: str) -> Any:
    """GET a la API guest de Pinnacle (con su header de key). Lanza RuntimeError."""
    if not PINNACLE_KEY:
        raise RuntimeError("Pinnacle sin PINNACLE_API_KEY: fuente apagada.")
    if requests is None:
        raise RuntimeError("La dependencia 'requests' no está instalada.")
    resp = requests.get(
        f"{PINNACLE_BASE}{path}",
        headers={"User-Agent": "Mozilla/5.0", "X-API-Key": PINNACLE_KEY},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Pinnacle respondió HTTP {resp.status_code} en {path}.")
    return resp.json()


def _pinnacle_liga_id(nombre: str = PINNACLE_LIGA) -> Optional[int]:
    """Id de la liga en Pinnacle por nombre (match flexible). None si no está."""
    try:
        ligas = _get_pinnacle(f"/sports/{PINNACLE_SOCCER_ID}/leagues?brandId=0")
    except RuntimeError:
        return None
    if not isinstance(ligas, list):
        return None
    objetivo = _norm(nombre)
    for lg in ligas:
        if _norm(lg.get("name", "")) == objetivo:
            return cast(Optional[int], lg.get("id"))
    # Respaldo: cualquier liga de México que no sea femenil/reservas/expansión.
    excluir = ("women", "femenil", "reserv", "u20", "u19", "u23", "expansion")
    for lg in ligas:
        n = _norm(lg.get("name", ""))
        if "mexico" in n and "liga mx" in n and not any(x in n for x in excluir):
            return cast(Optional[int], lg.get("id"))
    return None


def _pinnacle_odds_matchup(mid: Any) -> Dict[str, Any]:
    """1X2 + total 2.5 (decimales) de un matchup de Pinnacle. {} si no hay."""
    try:
        mkts = _get_pinnacle(f"/matchups/{mid}/markets/related/straight")
    except RuntimeError:
        return {}
    if not isinstance(mkts, list):
        return {}
    out: Dict[str, Any] = {}
    for mk in mkts:
        if not isinstance(mk, dict) or mk.get("period") != 0:
            continue
        precios = {p.get("designation"): p.get("price") for p in (mk.get("prices") or [])}
        if mk.get("type") == "moneyline":
            h = _americano_a_decimal(precios.get("home"))
            d = _americano_a_decimal(precios.get("draw"))
            a = _americano_a_decimal(precios.get("away"))
            if h and d and a:
                out["ml"] = {"local": h, "empate": d, "visita": a}
        elif mk.get("type") == "total" and str(mk.get("key", "")).endswith(";2.5"):
            ov = _americano_a_decimal(precios.get("over"))
            un = _americano_a_decimal(precios.get("under"))
            if ov and un:
                out["totals"] = {"linea": 2.5, "over": ov, "under": un}
    return out


def obtener_momios_pinnacle(
    liga_nombre: str = PINNACLE_LIGA, max_eventos: int = MAX_EVENTOS
) -> Dict[str, Dict[str, Any]]:
    """
    Momios de Liga MX desde Pinnacle (gratis, sin key de paga). Devuelve
    {clave_partido: {ml, totals}}. {} si la liga aún no tiene líneas o ante fallo.
    """
    lid = _pinnacle_liga_id(liga_nombre)
    if lid is None:
        return {}
    try:
        matchups = _get_pinnacle(f"/leagues/{lid}/matchups?brandId=0")
    except RuntimeError:
        return {}
    if not isinstance(matchups, list):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for m in matchups:
        if not isinstance(m, dict) or m.get("type") != "matchup" or m.get("parent"):
            continue
        parts = m.get("participants") or []
        home = next((p.get("name", "") for p in parts if p.get("alignment") == "home"), "")
        away = next((p.get("name", "") for p in parts if p.get("alignment") == "away"), "")
        if not home or not away:
            continue
        mercado = _pinnacle_odds_matchup(m.get("id"))
        if mercado:
            out[_clave_partido(home, away)] = mercado
        if len(out) >= max_eventos:
            break
    return out


def obtener_momios_espn(liga: str = ESPN_LIGA, max_eventos: int = MAX_EVENTOS) -> Dict[str, Dict[str, Any]]:
    """
    Momios de la jornada próxima desde ESPN (gratis, sin key). Devuelve
    {clave_partido: {ml, totals}}. Ante cualquier fallo devuelve {} (no-op).
    """
    if requests is None:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    try:
        sb = _get(ESPN_SCOREBOARD_URL.format(liga=liga), {})
    except RuntimeError:
        return {}
    eventos = sb.get("events", []) if isinstance(sb, dict) else []
    for ev in eventos[:max_eventos]:
        if not isinstance(ev, dict):
            continue
        comps = ev.get("competitions") or [{}]
        comp = comps[0] if comps else {}
        cid = comp.get("id") or ev.get("id")
        eid = ev.get("id")
        if not eid or not cid:
            continue
        home = away = ""
        for c in comp.get("competitors", []):
            nm = (c.get("team") or {}).get("displayName", "")
            if c.get("homeAway") == "home":
                home = nm
            elif c.get("homeAway") == "away":
                away = nm
        if not home or not away:
            continue
        try:
            odds_resp = _get(ESPN_ODDS_URL.format(liga=liga, eid=eid, cid=cid), {})
        except RuntimeError:
            continue
        mercado = _parsear_odds_espn(odds_resp.get("items") if isinstance(odds_resp, dict) else None)
        if mercado:
            out[_clave_partido(home, away)] = mercado
    return out


def momios_para_uso(guardar_si_hay: bool = False, incluir_gratis: bool = False) -> tuple:
    """
    Momios listos para el pick/plan. Orden de preferencia:
      odds-api.io (si hay key) -> Pinnacle (gratis) -> ESPN (gratis) -> caché.
    Devuelve (momios, fuente). El pick NO activa las fuentes gratis (evita red en
    cada pick); se bajan bajo demanda con /momios o scripts/fetch_momios.py y se
    cachean en data/momios.json, que el pick sí lee.
    """

    def _quizas_guardar(m: Dict[str, Any]) -> None:
        if guardar_si_hay:
            try:
                guardar_momios(m)
            except OSError:  # pragma: no cover - disco no escribible
                logger.debug("Exception silenciada en _quizas_guardar", exc_info=True)

    if mercado_habilitado():
        live = obtener_momios_liga_mx()
        if live:
            _quizas_guardar(live)
            return live, "odds-api.io"
    if incluir_gratis:
        for fetch, nombre in ((obtener_momios_pinnacle, "Pinnacle"), (obtener_momios_espn, "ESPN (DraftKings)")):
            try:
                data = fetch()
            except Exception:  # pragma: no cover - fuente caída: probar la siguiente
                data = {}
            if data:
                _quizas_guardar(data)
                return data, nombre
    guardados = cargar_momios()
    if guardados:
        return guardados, "cache (data/momios.json)"
    return {}, None


def diagnostico_mercado() -> Dict[str, Any]:
    """
    Diagnóstico en vivo de la conexión a odds-api.io (para depurar). Devuelve
    conteos y muestras SIN exponer la key. No lanza: captura errores.
    """
    info: Dict[str, Any] = {
        "habilitado": mercado_habilitado(),
        "liga_slug": LIGA_SLUG,
        "sport": SPORT_SLUG,
        "ventana_dias": VENTANA_DIAS,
    }
    if not info["habilitado"]:
        info["nota"] = "Sin ODDS_API_IO_KEY: capa apagada (no-op)."
        return info
    key = os.getenv(ENV_KEY, "").strip()
    try:
        eventos = _listar_eventos(key)
        info["n_eventos"] = len(eventos)
        info["eventos_muestra"] = [
            {
                "id": e.get("id"),
                "home": e.get("home"),
                "away": e.get("away"),
                "date": e.get("date"),
                "liga": (e.get("league") or {}).get("slug"),
            }
            for e in eventos[:5]
        ]
        info["bookmakers_usadas"] = _bookmakers_consulta()[:300]
        # ¿Qué casas tiene seleccionadas la cuenta? (plan gratis suele limitar)
        try:
            sel = _get(f"{BASE_URL}/bookmakers/selected", {"apiKey": key})
            info["bookmakers_seleccionadas"] = sel
        except RuntimeError as exc:
            info["bookmakers_seleccionadas_error"] = str(exc)
        if eventos:
            ev_id = eventos[0]["id"]
            todas = _bookmakers_consulta().split(",")
            # (a) Hallar el límite de casas: probar 1, 2, 3, 5, 10.
            pruebas: List[Dict[str, Any]] = []
            for n in (1, 2, 3, 5, 10):
                libro = ",".join(todas[:n])
                try:
                    r = _odds_evento(key, ev_id, libro)
                    casas = list((r.get("bookmakers") or {}).keys()) if r else []
                    pruebas.append({"casas_pedidas": n, "ok": True, "casas_con_datos": len(casas)})
                except RuntimeError as exc:
                    pruebas.append({"casas_pedidas": n, "ok": False, "error": str(exc)[-60:]})
            info["pruebas_por_n_casas"] = pruebas
            # (b) Intentar SELECCIONAR 2 casas y volver a pedir odds.
            if requests is not None:
                try:
                    put = requests.put(
                        f"{BASE_URL}/bookmakers/selected/select",
                        params={"apiKey": key, "bookmakers": "Bet365,1xbet"},
                        timeout=20,
                    )
                    info["seleccion_intento"] = {"status": put.status_code, "resp": str(put.text)[:160]}
                except Exception as exc:  # pragma: no cover
                    info["seleccion_intento"] = {"error": str(exc)[:120]}
                try:
                    r2 = _odds_evento(key, ev_id, "Bet365,1xbet")
                    info["odds_tras_seleccion"] = {
                        "casas": list((r2.get("bookmakers") or {}).keys()) if r2 else [],
                        "parseado": parsear_mercado(r2) if r2 else {},
                    }
                except RuntimeError as exc:
                    info["odds_tras_seleccion"] = {"error": str(exc)[-80:]}
            # (c) ¿Qué casas tienen odds de Liga MX AHORA? (/events?bookmaker=)
            hasta = (datetime.now(timezone.utc) + timedelta(days=VENTANA_DIAS)).strftime("%Y-%m-%dT%H:%M:%SZ")
            candidatos = [
                "Bet365",
                "1xbet",
                "Betano",
                "Betcris",
                "Caliente",
                "Codere",
                "Betsson",
                "Pinnacle",
                "Stake",
                "bwin",
            ]
            con_odds: Dict[str, Any] = {}
            for b in candidatos:
                try:
                    evs = _get(
                        f"{BASE_URL}/events",
                        {
                            "apiKey": key,
                            "sport": SPORT_SLUG,
                            "league": LIGA_SLUG,
                            "bookmaker": b,
                            "to": hasta,
                        },
                    )
                    con_odds[b] = len(evs) if isinstance(evs, list) else 0
                except RuntimeError as exc:
                    con_odds[b] = f"err {str(exc)[-24:]}"
            info["eventos_con_odds_por_casa"] = con_odds
            # Casas que el bot AUTO-seleccionara para consultar momios.
            info["casas_auto_seleccionadas"] = casas_con_odds_liga(key)
    except RuntimeError as exc:
        info["error"] = str(exc)
    return info


def comparar_pronosticos(pronosticos: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Punto de entrada de alto nivel: anota los pronósticos con la comparación de
    mercado si está habilitada. Si no, devuelve los pronósticos sin cambios.
    """
    habilitado = mercado_habilitado()
    momios, fuente = momios_para_uso()
    return {
        "mercado_habilitado": habilitado,
        "fuente_mercado": fuente,
        "partidos_con_momios": len(momios),
        "pronosticos": anotar_pronosticos(pronosticos, momios),
        "decision": DISCLAIMER,
    }
