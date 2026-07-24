from __future__ import annotations
from typing import Any, Dict, List, Optional

from src import calendario_contexto as calctx
from .formato_pick import _pick_club
from .contexto import _jugadores_seguir_partido, _porteros_partido
from .utils import _pct

DISCLAIMER = "ℹ️ Informativo / revisión humana. No es consejo de apuesta."
_MAX_PARTIDOS = 9


def _resumen_mercado(mercado: Optional[Dict[str, Any]]) -> Optional[str]:
    """Línea concisa con lo que ve el mercado (favorito, O/U, hándicap, valor)."""
    if not mercado:
        return None
    partes: List[str] = []
    o = mercado.get("1x2")
    if o and o.get("favorito_mercado"):
        partes.append(f"fav {o['favorito_mercado']}")
        if o.get("hay_valor") and o.get("valor_en"):
            partes.append(f"valor {o['valor_en']}")
    ou = mercado.get("over_under")
    if ou and ou.get("mercado_ve"):
        partes.append(ou["mercado_ve"])  # explosivo / cauteloso
        if ou.get("hay_valor") and ou.get("valor_en"):
            partes.append(f"valor {ou['valor_en']}")
    h = mercado.get("handicap")
    if h and h.get("favorito"):
        partes.append(f"hcp {h['favorito']} {h['linea']}")
    return " · ".join(partes) if partes else None


def _lineas_mercado(p: Dict[str, Any]) -> List[str]:
    """Líneas con los momios reales del mercado (si hay) + lectura del mercado."""
    mercado = p.get("mercado")
    if not mercado:
        return []
    local = p.get("local", "Local")
    visita = p.get("visitante", "Visita")
    out: List[str] = []
    o = mercado.get("1x2") or {}
    m = o.get("momios") or {}
    if m.get("local") and m.get("empate") and m.get("visita"):
        out.append(f"💰 Momios: {local} {m['local']} · Empate {m['empate']} · {visita} {m['visita']}")
    ou = mercado.get("over_under") or {}
    mou = ou.get("momios") or {}
    if mou.get("over") and mou.get("under"):
        linea = ou.get("linea", 2.5)
        out.append(f"⚖️ O/U {linea}: Over {mou['over']} · Under {mou['under']}")
    resumen = _resumen_mercado(mercado)
    if resumen:
        out.append(f"📈 Mercado ve: {resumen}")
    return out


def _linea_goles(p: Dict[str, Any]) -> str:
    """Línea de goles: pick Over/Under con su %, BTTS y marcador más probable."""
    pick_ou = p.get("pick_ou", "")
    over = p.get("prob_over_pct")
    # % del lado elegido: si el pick es Over, es prob_over; si Under, el complemento.
    pct_txt = ""
    if over is not None:
        pct = float(over) if pick_ou == "Over" else round(100.0 - float(over), 1)
        pct_txt = f" ({_pct(pct)}%)"
    # BTTS solo si hay dato (evita mostrar 'None').
    btts = p.get("pick_btts")
    btts_txt = f" · BTTS {btts}" if btts else ""
    marcador = str(p.get("marcador_pick") or p.get("marcador_mas_probable", ""))
    # Partido sin datos de goles (p.ej. pick solo-momios): no hay línea que mostrar.
    if not pick_ou and not marcador:
        return ""
    partes = []
    if pick_ou:
        partes.append(f"⚽ Goles: {pick_ou} 2.5{pct_txt}{btts_txt}")
    if marcador:
        partes.append(f"🔢 Marcador probable: {marcador}")
    linea = "\n".join(partes)
    # ¿Choca la moda con el pick Over/Under?
    total = None
    if "-" in marcador:
        try:
            gl, gv = (int(x) for x in marcador.split("-", 1))
            total = gl + gv
        except (TypeError, ValueError):
            total = None
    if total is not None:
        if pick_ou == "Over" and total <= 2:
            linea += (
                "\nℹ️ <i>La moda (2 goles) es baja, pero el grueso de "
                "escenarios apunta a más goles: por eso el pick es Over.</i>"
            )
        elif pick_ou == "Under" and total >= 3:
            linea += (
                "\nℹ️ <i>Ese marcador exacto es el más probable, pero el "
                "grueso de escenarios queda por debajo: por eso el pick es Under.</i>"
            )
    return linea


def _totales_jornada(pronosticos: list, partidos_esperados: int = _MAX_PARTIDOS) -> Dict[str, Any]:
    """Calcula cobertura y un total utilizable como desempate del Survivor."""
    esperados = max(0, int(partidos_esperados))
    recibidos = len(pronosticos)
    goles_por_partido: List[float] = []
    for pronostico in pronosticos:
        try:
            local = float(pronostico["goles_esperados_local"])
            visitante = float(pronostico["goles_esperados_visitante"])
        except (KeyError, TypeError, ValueError):
            continue
        goles_por_partido.append(local + visitante)

    con_xg = len(goles_por_partido)
    total_goles = sum(goles_por_partido)
    promedio = total_goles / con_xg if con_xg else 0.0
    cobertura_completa = esperados > 0 and con_xg >= esperados
    desempate = int(total_goles + 0.5) if cobertura_completa else None
    over_25 = sum(1 for p in pronosticos if p.get("pick_ou") == "Over")
    under_25 = sum(1 for p in pronosticos if p.get("pick_ou") == "Under")
    btts_si = sum(1 for p in pronosticos if p.get("pick_btts") == "Sí")
    btts_no = sum(1 for p in pronosticos if p.get("pick_btts") == "No")
    return {
        "partidos": recibidos,
        "partidos_esperados": esperados,
        "partidos_con_xg": con_xg,
        "partidos_sin_xg": max(0, esperados - con_xg),
        "cobertura_completa": cobertura_completa,
        "goles_desempate": desempate,
        "goles_esperados_total": round(total_goles, 1),
        "promedio_goles_partido": round(promedio, 2),
        "over_25_count": over_25,
        "under_25_count": under_25,
        "btts_si_count": btts_si,
        "btts_no_count": btts_no,
    }


def construir_mensaje_momios(momios: Dict[str, Any], fuente: Optional[str]) -> str:
    """Mensaje (HTML) con el estado/cobertura de los momios por mercado."""
    if not momios:
        return (
            "💰 <b>MOMIOS</b>\n\n"
            "Todavía no hay líneas publicadas para estos partidos (ni odds-api.io "
            "ni ESPN, ni guardadas). El pick usa solo el modelo por ahora; "
            "vuelve a intentar más cerca de la jornada.\n\n"
            f"{DISCLAIMER}"
        )
    n_ml = sum(1 for m in momios.values() if isinstance(m, dict) and m.get("ml"))
    n_tot = sum(1 for m in momios.values() if isinstance(m, dict) and m.get("totals"))
    n_hdp = sum(1 for m in momios.values() if isinstance(m, dict) and m.get("handicap"))
    lineas = [
        "💰 <b>MOMIOS ACTUALIZADOS</b>",
        f"<i>Fuente: {fuente or '—'} · {len(momios)} partidos</i>",
        "",
        f"🧮 1X2 (mueve el Survivor): <b>{n_ml}</b>",
        f"⚽ Over/Under 2.5: <b>{n_tot}</b>",
        f"⚖️ Hándicap: <b>{n_hdp}</b>",
        "",
        "<i>El pick y el plan ya los mezclan con el modelo.</i>",
        DISCLAIMER,
    ]
    return "\n".join(lineas)


def construir_recordatorio(jornada: Dict[str, Any], dias: int) -> str:
    """Mensaje (HTML) de recordatorio de que se acerca una jornada."""
    n = jornada.get("jornada", "?")
    ini = jornada.get("fecha_inicio", "")
    cuando = "hoy" if dias == 0 else ("mañana" if dias == 1 else f"en {dias} días")
    lineas = [
        f"⏰ <b>SE ACERCA LA JORNADA {n}</b>",
        f"<i>Arranca {cuando} ({ini}).</i>",
        "",
        "Mándame <b>/picks</b> ~1 hora antes de los partidos y te doy el pick de "
        "Survivor + los pronósticos de la jornada con datos frescos.",
    ]
    partidos = jornada.get("partidos") or []
    if partidos:
        lineas.append("")
        lineas.append("📋 Partidos:")
        for p in partidos[:_MAX_PARTIDOS]:
            h = p.get("home_team", "")
            a = p.get("away_team", "")
            if h and a:
                lineas.append(f"  • {h} vs {a}")
    lineas += ["", DISCLAIMER]
    return "\n".join(lineas)


def render_partidos(
    pronosticos: List[Dict[str, Any]],
    goleadores_map: Optional[Dict[str, List[Dict[str, Any]]]],
    porteros_map: Optional[Dict[str, Dict[str, Any]]],
) -> List[str]:
    """Genera las líneas HTML para la sección de Partidos."""
    lineas = []
    div = "━━━━━━━━━━"
    if pronosticos:
        lineas.append(div)
        lineas.append("📋 <b>PARTIDOS DE LA JORNADA</b>")
        nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
        for idx, p in enumerate(pronosticos[:_MAX_PARTIDOS]):
            lineas.append("")
            n = nums[idx] if idx < len(nums) else "•"
            conf = f" · confianza <b>{p['nivel_confianza']}</b>" if p.get("nivel_confianza") else ""
            prob_pick = p.get("prob_pick_pct")
            pptxt = f" ({_pct(prob_pick)}%)" if prob_pick is not None else ""
            lineas.append(f"{n} <b>{p['local']}</b> 🏠 vs <b>{p['visitante']}</b> ✈️")
            lineas.append(f"🎯 Pick: <b>{_pick_club(p)}</b>{pptxt}{conf}")
            lineas.append(
                f"📊 Local {_pct(p['prob_local_pct'])}% · Empate {_pct(p['prob_empate_pct'])}% · Visita {_pct(p['prob_visitante_pct'])}%"
            )
            _lg = _linea_goles(p)
            if _lg:
                lineas.append(_lg)
            if p.get("explicacion_1x2"):
                lineas.append(f"💡 {p['explicacion_1x2']}")
            if p.get("explicacion_ou"):
                lineas.append(f"💡 {p['explicacion_ou']}")
            if p.get("nota_handicap"):
                lineas.append(f"🔻 {p['nota_handicap']}")
            if p.get("precaucion") and p.get("motivos_alerta"):
                lineas.append(f"{p['nivel_alerta']}: {' '.join(p['motivos_alerta'])}")
            if p.get("h2h_nota"):
                lineas.append(f"🐆 H2H: {p['h2h_nota']}")
            lineas.extend(_lineas_mercado(p))
            try:
                cal_ev = calctx.eventos_para_fecha(p.get("fecha"), [p.get("local", ""), p.get("visitante", "")])
            except Exception:
                cal_ev = []
            if cal_ev:
                nombres = " · ".join(f"{e.get('emoji', '🗓️')} {e.get('nombre')}" for e in cal_ev)
                lineas.append(f"🗓️ Calendario: {nombres}")
            if goleadores_map:
                estrellas = _jugadores_seguir_partido(p, goleadores_map)
                if estrellas:
                    lineas.append(f"⭐ A seguir: {estrellas}")
            if porteros_map:
                muro = _porteros_partido(p, porteros_map)
                if muro:
                    lineas.append(f"🧤 Muro: {muro}")
    else:
        lineas.append(div)
        lineas.append("Sin pronósticos disponibles (faltan datos de ESPN o fixtures).")
    return lineas
