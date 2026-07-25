#!/usr/bin/env python3
"""
motor_pronosticos.py — Cerebro de pronósticos Liga MX (datos reales, gratis).

Ata todas las piezas legítimas:
    fuentes_datos (ESPN/TheSportsDB/caché)  ->  fuerza de equipos
    poisson_model (Dixon-Coles)             ->  probabilidades por partido
    espn_data (fixtures próximos)           ->  qué partidos predecir

Produce, por partido próximo: 1X2, Over/Under, BTTS, marcador probable y el
"no perder" para Survivor. Además calcula el mejor pick de Survivor de la
jornada (equipo con mayor probabilidad de no perder, excluyendo los ya usados).

Sin momios, sin scraping, sin APIs de pago. Solo resultados reales de ESPN.
Decisión operativa informativa: este motor NO cierra ni envía picks por sí solo.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
import logging

logger = logging.getLogger(__name__)

from src import fuentes_datos, espn_data
from src import poisson_model as pm
from src.cache_ttl import ttl_cache
from src.team_normalizer import canonical_team_key

BASE_DIR = Path(__file__).resolve().parents[1]
PRONOSTICOS_PATH = BASE_DIR / "data" / "pronosticos.json"

DEC_INFORMATIVA = "INFORMATIVO / REVISIÓN HUMANA"


def _norm(t: str) -> str:
    base = unicodedata.normalize("NFKD", str(t or "")).lower()
    base = "".join(c for c in base if not unicodedata.combining(c))
    return " ".join(base.split())


def _equipo_conocido(nombre: str, fuerzas: Dict[str, Any]) -> bool:
    return pm._norm(nombre) in fuerzas.get("equipos", {})


def _explicar_partido(p: Dict[str, Any]) -> Dict[str, str]:
    """Explica, con los NÚMEROS del modelo (no opinión), el 1X2 y el Over/Under."""
    local, visitante = p["local"], p["visitante"]
    pl, pe, pv = p["prob_local_pct"], p["prob_empate_pct"], p["prob_visitante_pct"]
    gl, gv = p["lambda_local"], p["lambda_visitante"]
    total = gl + gv
    linea = p.get("linea_goles", 2.5)

    if p["pick_1x2"] == "Gana Local":
        e1 = (
            f"{local} es favorito: el modelo le da {pl}% de ganar (vs {pv}% de {visitante}); "
            f"pesan su mayor fuerza y la ventaja de local. Marcador esperado ~{gl:.1f}-{gv:.1f}."
        )
    elif p["pick_1x2"] == "Gana Visitante":
        e1 = (
            f"{visitante} es favorito aun de visita: {pv}% de ganar (vs {pl}% de {local}); "
            f"su fuerza supera la ventaja local del rival. Marcador esperado ~{gl:.1f}-{gv:.1f}."
        )
    else:
        e1 = (
            f"Partido parejo (L {pl}% / E {pe}% / V {pv}%): sin favorito claro y con ~{total:.1f} "
            f"goles esperados, el EMPATE es el escenario más probable del modelo."
        )

    if p["pick_ou"] == "Over":
        e2 = (
            f"Over {linea}: se esperan ~{total:.1f} goles ({p['prob_over_pct']}%); los ataques "
            f"pesan más que las defensas."
        )
    else:
        e2 = (
            f"Under {linea}: solo ~{total:.1f} goles esperados ({p['prob_under_pct']}%); "
            f"defensas sólidas o ataques flojos, por eso el modelo ve pocos goles."
        )
    return {"explicacion_1x2": e1, "explicacion_ou": e2}


def _nivel_confianza_1x2(prob_pick_pct: float) -> str:
    if prob_pick_pct >= 55.0:
        return "ALTA"
    if prob_pick_pct >= 42.0:
        return "MEDIA"
    return "BAJA"


_EMPATE_ALTO_PCT = 30.0
_GOLES_CERRADO = 2.3
_PICK_ABIERTO_PCT = 45.0
_UNDER_VALOR_PCT = 60.0
_GOLEADA_RIESGO_PCT = 25.0


def _nota_under_handicap(
    pick_ou: str, prob_under_pct: float, total: float, prob_margen2_pct: float, prob_margen3_pct: float
) -> Dict[str, Any]:
    under_valor = pick_ou == "Under" and prob_under_pct >= _UNDER_VALOR_PCT
    if not under_valor:
        return {"under_valor": False, "nota_handicap": None}
    riesgo_alto = prob_margen3_pct >= _GOLEADA_RIESGO_PCT
    base = (
        f"UNDER de valor: {prob_under_pct:.0f}% bajo 2.5 (~{total:.1f} goles). "
        f"Margen 2+: {prob_margen2_pct:.0f}% · goleada 3+: {prob_margen3_pct:.0f}%."
    )
    if riesgo_alto:
        base += " ⚠️ OJO: riesgo de goleada ALTO — el +1.5/+2 podría NO cubrir; el under es de lectura arriesgada."
    else:
        base += " ✅ Riesgo de goleada bajo — el +1.5/+2 luce cubierto."
    return {"under_valor": True, "nota_handicap": base}


def _alertas_partido(pick_1x2: str, prob_empate: float, prob_pick: float, goles_totales: float) -> Dict[str, Any]:
    motivos: List[str] = []
    if pick_1x2 == "Gana Visitante":
        motivos.append("El favorito es VISITANTE (de visita hay más sorpresas).")
    if prob_pick < _PICK_ABIERTO_PCT:
        motivos.append(f"Sin favorito claro (pick {prob_pick:.0f}%): resultado muy abierto.")
    if prob_empate >= _EMPATE_ALTO_PCT:
        motivos.append(f"Empate probable ({prob_empate:.0f}%): riesgo de 'push' (empate = sobrevives sin punto).")
    if goles_totales < _GOLES_CERRADO:
        motivos.append(f"Partido cerrado (~{goles_totales:.1f} goles): pocos goles, propenso a empate/sorpresa.")
    if len(motivos) >= 2:
        nivel = "🚨 ALERTA ROJA"
    elif len(motivos) == 1:
        nivel = "⚠️ PRECAUCIÓN"
    else:
        nivel = "OK"
    return {"precaucion": bool(motivos), "nivel_alerta": nivel, "motivos": motivos}


def pronosticar_partido(home: str, away: str, fuerzas: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not _equipo_conocido(home, fuerzas) or not _equipo_conocido(away, fuerzas):
        return None
    p = pm.pronostico(home, away, fuerzas)
    exp = _explicar_partido(p)
    prob_pick = max(p["prob_local_pct"], p["prob_empate_pct"], p["prob_visitante_pct"])
    goles_totales = p["lambda_local"] + p["lambda_visitante"]
    alerta = _alertas_partido(p["pick_1x2"], p["prob_empate_pct"], prob_pick, goles_totales)
    hand = _nota_under_handicap(p["pick_ou"], p["prob_under_pct"], goles_totales, p["prob_margen2_pct"], p["prob_margen3_pct"])
    return {
        "local": home, "visitante": away,
        "pick_1x2": p["pick_1x2"],
        "prob_local_pct": p["prob_local_pct"],
        "prob_empate_pct": p["prob_empate_pct"],
        "prob_visitante_pct": p["prob_visitante_pct"],
        "prob_pick_pct": round(prob_pick, 2),
        "nivel_confianza": _nivel_confianza_1x2(prob_pick),
        "precaucion": alerta["precaucion"],
        "nivel_alerta": alerta["nivel_alerta"],
        "motivos_alerta": alerta["motivos"],
        "goles_esperados_local": p["lambda_local"],
        "goles_esperados_visitante": p["lambda_visitante"],
        "pick_ou": p["pick_ou"],
        "prob_over_pct": p["prob_over_pct"],
        "prob_under_pct": p["prob_under_pct"],
        "prob_margen2_pct": p["prob_margen2_pct"],
        "prob_margen3_pct": p["prob_margen3_pct"],
        "under_valor": hand["under_valor"],
        "nota_handicap": hand["nota_handicap"],
        "pick_btts": p["pick_btts"],
        "prob_btts_si_pct": p["prob_btts_si_pct"],
        "marcador_mas_probable": p["marcador_mas_probable"],
        "marcador_pick": p.get("marcador_pick", p["marcador_mas_probable"]),
        "no_perder_local_pct": round(p["prob_local_pct"] + p["prob_empate_pct"], 2),
        "no_perder_visitante_pct": round(p["prob_visitante_pct"] + p["prob_empate_pct"], 2),
        "explicacion_1x2": exp["explicacion_1x2"],
        "explicacion_ou": exp["explicacion_ou"],
    }


@ttl_cache(600)
def generar_pronosticos(
    meses: int = 18,
    fixtures: Optional[Sequence[Dict[str, Any]]] = None,
    resultados: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if resultados is None:
        datos = fuentes_datos.obtener_resultados(meses)
        resultados = datos["resultados"]
        fuente = datos["fuente"]
    else:
        fuente = "inyectada"
    if fixtures is None:
        try:
            fixtures = espn_data.obtener_fixtures_proxima_jornada()
            if not fixtures:
                fixtures = espn_data.obtener_fixtures()
        except Exception:
            fixtures = []
    pronosticos: List[Dict[str, Any]] = []
    fuerzas: Optional[Dict[str, Any]] = None
    if resultados:
        try:
            fuerzas = pm.calcular_fuerzas(resultados)
        except ValueError:
            fuerzas = None
    fixtures_sin_modelo: List[Dict[str, Any]] = []
    if fuerzas:
        for fx in fixtures:
            home = fx.get("home_team", "")
            away = fx.get("away_team", "")
            pron = pronosticar_partido(home, away, fuerzas)
            if pron:
                pron["fecha"] = fx.get("fecha", "")
                pronosticos.append(pron)
            elif home and away:
                fixtures_sin_modelo.append({"home_team": home, "away_team": away, "fecha": fx.get("fecha", "")})
    try:
        from src import matchup_h2h as mh2h
        h2h_hist = resultados
        try:
            from src import ligamx_api as _lmx
            largo = _lmx.resultados_historicos()
            if isinstance(largo, list) and len(largo) > len(resultados):
                h2h_hist = largo
        except Exception:
            logger.debug("Exception silenciada en generar_pronosticos", exc_info=True)
        pronosticos = mh2h.anotar_h2h(pronosticos, h2h_hist)
    except Exception:
        logger.debug("Exception silenciada en generar_pronosticos", exc_info=True)
    return {
        "generado_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fuente_datos": fuente,
        "total_resultados_historicos": len(resultados),
        "total_pronosticos": len(pronosticos),
        "pronosticos": pronosticos,
        "fixtures_sin_modelo": fixtures_sin_modelo,
        "decision": DEC_INFORMATIVA,
    }


def mejores_picks_survivor(
    pronosticos: Sequence[Dict[str, Any]],
    equipos_usados: Optional[Sequence[str]] = None,
    motivacion: Optional[Dict[str, Dict[str, Any]]] = None,
    n: int = 3,
    uno_por_partido: bool = True,
) -> List[Dict[str, Any]]:
    usados = {canonical_team_key(e) for e in (equipos_usados or [])}
    mot = motivacion or {}
    candidatos: List[Dict[str, Any]] = []
    for p in pronosticos:
        empate = p.get("prob_empate_pct")
        for equipo, rival, cond, prob, win in (
            (p["local"], p["visitante"], "Local", p["no_perder_local_pct"], p.get("prob_local_pct")),
            (p["visitante"], p["local"], "Visitante", p["no_perder_visitante_pct"], p.get("prob_visitante_pct")),
        ):
            if canonical_team_key(equipo) in usados:
                continue
            candidatos.append({
                "equipo": equipo, "rival": rival, "condicion": cond,
                "no_perder_pct": prob, "prob_victoria_pct": win, "prob_empate_pct": empate,
                "nivel": _nivel_pick(prob, win),
                "motivacion_propia": (mot.get(_norm(equipo)) or {}).get("motivacion_nivel"),
                "rival_motivacion": (mot.get(_norm(rival)) or {}).get("motivacion_nivel"),
            })
    candidatos.sort(key=lambda c: (c["no_perder_pct"], c.get("prob_victoria_pct") or 0.0, _rank_motivacion(c["rival_motivacion"])), reverse=True)
    if uno_por_partido:
        candidatos = _uno_por_partido(candidatos)
    return candidatos[: max(0, n)]


def _clave_partido(c: Dict[str, Any]) -> frozenset:
    return frozenset({canonical_team_key(c.get("equipo", "")), canonical_team_key(c.get("rival", ""))})


def _uno_por_partido(candidatos: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    vistos: set = set()
    salida: List[Dict[str, Any]] = []
    for c in candidatos:
        k = _clave_partido(c)
        if k in vistos:
            continue
        vistos.add(k)
        salida.append(c)
    return salida


_NIVEL_NO_PERDER_ALTA = 75.0
_NIVEL_GANAR_ALTA = 55.0
_NIVEL_NO_PERDER_MEDIA = 65.0


def _nivel_pick(no_perder_pct: float, win_pct: Optional[float]) -> str:
    if win_pct is None:
        if no_perder_pct >= _NIVEL_NO_PERDER_ALTA:
            return "ALTA"
        if no_perder_pct >= _NIVEL_NO_PERDER_MEDIA:
            return "MEDIA"
        return "RIESGOSA"
    if no_perder_pct >= _NIVEL_NO_PERDER_ALTA and win_pct >= _NIVEL_GANAR_ALTA:
        return "ALTA"
    if no_perder_pct >= _NIVEL_NO_PERDER_MEDIA:
        return "MEDIA"
    return "RIESGOSA"


def mejor_pick_survivor(
    pronosticos: Sequence[Dict[str, Any]],
    equipos_usados: Optional[Sequence[str]] = None,
    motivacion: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    tops = mejores_picks_survivor(pronosticos, equipos_usados, motivacion, n=1)
    return tops[0] if tops else None


UMBRAL_CAUTELA_PARTIDOS = 27
PEN_VISITANTE = 4.0
PEN_VISITANTE_CAUTELA = 8.0
PESO_VICTORIA_PICK = 0.5
PESO_VICTORIA_PICK_CAUTELA = 0.25

CROWD_PEN_ALTO_PCT = 15.0
CROWD_PEN_MED_PCT = 5.0
PEN_CROWD_ALTO = 12.0
PEN_CROWD_MEDIO = 2.0

try:
    try:
        from routers.predicciones import CROWD_DISTRIBUTION as _CROWD_DIST
    except ImportError:
        from src.routers.predicciones import CROWD_DISTRIBUTION as _CROWD_DIST
except Exception:
    _CROWD_DIST = {}


def _penalizacion_crowd(equipo):
    pct = _CROWD_DIST.get(equipo, 0.0)
    if pct >= CROWD_PEN_ALTO_PCT:
        return PEN_CROWD_ALTO
    if pct >= CROWD_PEN_MED_PCT:
        return PEN_CROWD_MEDIO
    return 0.0


def _razon_pick(c: Dict[str, Any], es_local: bool, cautela: bool, vida_consumida: bool = False) -> str:
    rival_mot = (c.get("rival_motivacion") or "").lower()
    np_pct = c.get("no_perder_pct")
    win = c.get("prob_victoria_pct")
    emp = c.get("prob_empate_pct")
    nums = f"{np_pct}% de no perder"
    if win is not None:
        nums += f" ({win}% ganar"
        if emp is not None:
            nums += f" + {emp}% empatar"
        nums += ")"
    cond = "de LOCAL" if es_local else "de VISITA"
    base = f"{c.get('equipo')} {cond}: {nums}."
    if vida_consumida:
        base += " ⚠️ Vida de empate YA CONSUMIDA: empatar no sirve, solo ganar sobrevive."
    if es_local:
        base += " Los locales fallan menos que los visitantes."
        if rival_mot == "baja":
            base += f" Además {c.get('rival')} llega sin presión (relajado/eliminado): escenario más seguro."
    else:
        base += " ⚠️ Ojo: es favorito visitante y de visita hay más sorpresas."
    if cautela:
        base += " Arranque de torneo: voy conservador."
    return base


def _nivel_estrategico(no_perder: float, win: Optional[float], es_local: bool, cautela: bool) -> str:
    nivel = _nivel_pick(no_perder, win)
    if not es_local and nivel == "ALTA":
        nivel = "MEDIA"
    if cautela and nivel == "ALTA" and no_perder < 80.0:
        nivel = "MEDIA"
    return nivel


def mejores_picks_estrategico(
    pronosticos: Sequence[Dict[str, Any]],
    equipos_usados: Optional[Sequence[str]] = None,
    motivacion: Optional[Dict[str, Dict[str, Any]]] = None,
    partidos_jugados_torneo: Optional[int] = None,
    n: int = 3,
    vida_empate_consumida: bool = False,
) -> Dict[str, Any]:
    """Pick de Survivor con estrategia anti-sorpresa, vida de empate y cautela.

    Cuando ``vida_empate_consumida`` es True, el empate ya equivale a eliminación:
    la probabilidad de supervivencia y el score se derivan exclusivamente de
    ganar, usando ``metricas_candidato`` del módulo de reglas oficiales.
    """
    from src.survivor_reglas import metricas_candidato

    cautela = (partidos_jugados_torneo is None) or (partidos_jugados_torneo < UMBRAL_CAUTELA_PARTIDOS)
    pen = PEN_VISITANTE_CAUTELA if cautela else PEN_VISITANTE
    peso_victoria = PESO_VICTORIA_PICK_CAUTELA if cautela else PESO_VICTORIA_PICK

    base = list(mejores_picks_survivor(pronosticos, equipos_usados, motivacion, n=10_000, uno_por_partido=False))
    for c in base:
        es_local = c.get("condicion") == "Local"
        no_perder = float(c.get("no_perder_pct") or 0.0)
        victoria = float(c.get("prob_victoria_pct") or 0.0)
        pen_crowd = _penalizacion_crowd(c.get("equipo", ""))

        if vida_empate_consumida:
            metas = metricas_candidato(c, True)
            survival = metas["supervivencia_pct"]
            score_oficial = metas["score_oficial"]
        else:
            survival = no_perder
            score_oficial = no_perder + peso_victoria * victoria

        c["_score"] = score_oficial - (0.0 if es_local else pen) - pen_crowd
        c["supervivencia_pct"] = survival
        c["nivel"] = _nivel_estrategico(no_perder, c.get("prob_victoria_pct"), es_local, cautela)
        c["razon"] = _razon_pick(c, es_local, cautela, vida_empate_consumida)
        if pen_crowd > 0:
            crowd_pct = _CROWD_DIST.get(c.get("equipo", ""), 0.0)
            c["razon"] += (
                " ⚠️ OJO: es un pick de manada ("
                + str(round(crowd_pct, 1))
                + "% del publico lo picka). PERO el bot lo prioriza porque su probabilidad de no perder es tan alta que una sorpresa seria un upset historico: estadisticamente vale mas la pena arriesgarse con el favorito que ir contra la logica por miedo a la manada."
            )
    base.sort(
        key=lambda c: (c["_score"], c.get("prob_victoria_pct") or 0.0, _rank_motivacion(c.get("rival_motivacion"))),
        reverse=True,
    )
    base = _uno_por_partido(base)
    for c in base:
        c.pop("_score", None)

    advertencia = None
    if cautela:
        advertencia = (
            "⚠️ Arranque de torneo (pocos datos aún): priorizo LOCALES, evito favoritos "
            "visitantes y guardo a los equipos fuertes para jornadas difíciles. "
            "Las primeras semanas traen sorpresas."
        )
    if vida_empate_consumida:
        vida_aviso = "⚠️ Vida de empate CONSUMIDA: el empate ya NO salva; solo ganar sobrevive."
        advertencia = f"{advertencia}\n{vida_aviso}" if advertencia else vida_aviso
    return {
        "cautela": cautela,
        "partidos_jugados_torneo": partidos_jugados_torneo,
        "advertencia": advertencia,
        "vida_empate_consumida": vida_empate_consumida,
        "picks": base[: max(0, n)],
    }


_RANK_MOTIVACION = {"baja": 3.0, "n/a": 2.0, "media": 1.0, "alta": 0.0}


def _rank_motivacion(nivel: Optional[str]) -> float:
    if nivel is None:
        return 1.5
    return _RANK_MOTIVACION.get(str(nivel).lower(), 1.5)


def motivacion_por_equipo() -> Dict[str, Dict[str, Any]]:
    from src import tabla_posiciones as tabla_mod
    try:
        data = tabla_mod.obtener_tabla()
    except Exception:
        return {}
    salida: Dict[str, Dict[str, Any]] = {}
    for fila in data.get("tabla", []):
        salida[_norm(fila.get("equipo", ""))] = {
            "motivacion_nivel": fila.get("motivacion_nivel"),
            "zona": fila.get("zona"),
        }
    return salida


def guardar_pronosticos(resultado: Dict[str, Any], path: Path = PRONOSTICOS_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(resultado, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    print("🧠 Generando pronósticos Liga MX (datos reales de ESPN)...")
    resultado = generar_pronosticos()
    guardar_pronosticos(resultado)
    print(f"✅ Fuente: {resultado['fuente_datos']} | histórico: {resultado['total_resultados_historicos']} | pronósticos: {resultado['total_pronosticos']}")
    for p in resultado["pronosticos"]:
        print(f"  {p['local']} vs {p['visitante']}: {p['pick_1x2']} (L{p['prob_local_pct']}/E{p['prob_empate_pct']}/V{p['prob_visitante_pct']}) | {p['pick_ou']} 2.5 | marcador {p['marcador_mas_probable']}")
    pick = mejor_pick_survivor(resultado["pronosticos"])
    if pick:
        print(f"🎯 Survivor sugerido: {pick['equipo']} ({pick['condicion']} vs {pick['rival']}) — no perder {pick['no_perder_pct']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
