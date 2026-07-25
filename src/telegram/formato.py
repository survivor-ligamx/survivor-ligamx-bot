from __future__ import annotations
from typing import Any, Dict, List, Optional
from src import calendario_contexto as calctx
from .formato_pick import render_survivor, DISCLAIMER
from .formato_partidos import render_partidos
from .oracion import oracion_para_pick
from .totales import calcular_totales_jornada
from .utils import _fecha_mx, _pct


def construir_mensaje(
    resultado: Dict[str, Any],
    equipos_usados: Optional[List[str]] = None,
    motivacion: Optional[Dict[str, Dict[str, Any]]] = None,
    contexto_pick: Optional[Dict[str, Any]] = None,
    tops: Optional[List[Dict[str, Any]]] = None,
    advertencia: Optional[str] = None,
    goleadores_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    porteros_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """Arma el mensaje (HTML) de pronósticos a partir de la salida del motor."""
    pronosticos = resultado.get("pronosticos", [])
    fecha = _fecha_mx(resultado.get("generado_utc", ""))
    div = "━━━━━━━━━━"

    lineas = [
        "🔮 <b>PRONÓSTICOS LIGA MX</b>",
        "<i>Modelo ESPN + Poisson</i>",
        f"🕒 <i>{fecha}</i>",
        div,
    ]

    # 1. Survivor
    survivor_lineas = render_survivor(pronosticos, equipos_usados, motivacion, tops, advertencia, contexto_pick)
    lineas.extend(survivor_lineas)
    if survivor_lineas:
        lineas.append("")
        lineas.append(oracion_para_pick(tops))

    # 2. Contexto de calendario
    try:
        cal_lineas = calctx.resumen_jornada(pronosticos)
    except Exception:
        cal_lineas = []
    if cal_lineas:
        lineas.append(div)
        lineas.append("🗓️ <b>CONTEXTO DE CALENDARIO</b>")
        lineas.append("<i>Afecta disponibilidad/desgaste:</i>")
        for c in cal_lineas:
            lineas.append(f"• {c}")

    # 3. Partidos de la jornada
    lineas.extend(render_partidos(pronosticos, goleadores_map, porteros_map))

    # 4. Totales de la jornada
    if pronosticos:
        lineas.append(div)
        lineas.append("📊 <b>TOTALES DE LA JORNADA</b>")
        totales = calcular_totales_jornada(pronosticos)
        lineas.append(
            f"📋 Cobertura: {totales['partidos']}/{totales['partidos_esperados']} partidos · "
            f"modelo de goles {totales['partidos_con_xg']}/{totales['partidos_esperados']}"
        )
        if totales["goles_desempate"] is not None:
            lineas.append(f"🎯 <b>Pronóstico para desempate: {totales['goles_desempate']} goles</b>")
        else:
            lineas.append(
                f"⚠️ Total provisional: faltan {totales['partidos_sin_xg']} partidos con modelo. "
                "No lo uses todavía como desempate definitivo."
            )
        lineas.append(f"⚽ xG acumulado disponible: {totales['goles_esperados_total']}")
        lineas.append(f"📊 Promedio por partido modelado: {totales['promedio_goles_partido']}")
        lineas.append(f"🔺 Over 2.5: {totales['over_25_count']} partidos")
        lineas.append(f"🔻 Under 2.5: {totales['under_25_count']} partidos")
        lineas.append(f"✅ BTTS Sí: {totales['btts_si_count']} partidos")
        lineas.append(f"❌ BTTS No: {totales['btts_no_count']} partidos")

    lineas += [div, DISCLAIMER]
    return "\n".join(lineas)


def construir_mensaje_seguimiento(
    items: List[Dict[str, Any]],
    descartados: Optional[List[str]] = None,
    recomendado: Optional[Dict[str, Any]] = None,
    nota_plan: Optional[str] = None,
) -> str:
    """
    Mensaje (HTML) centrado en UN pick claro y UN momento para actuar (no un menú).
    El respaldo solo se menciona; el día del partido, el veredicto del XI decide
    si se mantiene o se cambia.
    """
    if not items:
        return f"📋 <b>LISTA DE SEGUIMIENTO</b>\n\nAún no hay candidatos (faltan datos de la jornada).\n\n{DISCLAIMER}"

    def _sede(c: Dict[str, Any]) -> str:
        return "🏠 local" if c.get("condicion") == "Local" else "✈️ visita"

    rec = recomendado or items[0]
    rec_item = next((it for it in items if it.get("equipo") == rec.get("equipo")), rec)
    cuando = rec_item.get("cuando") or ""
    ver = rec_item.get("veredicto") or {}
    gana = rec.get("prob_victoria_pct")
    gtxt = f" · gana {_pct(gana)}%" if gana is not None else ""

    lineas = [
        "🎯 <b>TU PICK DE SURVIVOR</b>",
        f"✅ <b>{rec['equipo']}</b>",
        f"{_sede(rec)} vs {rec['rival']}",
        f"Sobrevive {_pct(rec['no_perder_pct'])}%{gtxt}",
        f"Confianza <b>{rec.get('nivel', '—')}</b>",
    ]
    if nota_plan:
        lineas.append(nota_plan)
    if cuando:
        lineas.append(f"📅 Juega: <b>{cuando}</b>")
    lineas.append("")

    estado = ver.get("estado", "PENDIENTE")
    if estado == "CONFIRMA":
        lineas.append("✅ <b>Alineación confirmada y completa.</b> Este es tu pick — mételo en PlayDoit.")
    elif estado in ("DESCARTA", "DUDA"):
        alt = next((it["equipo"] for it in items if it.get("equipo") != rec.get("equipo")), None)
        lineas.append(f"{ver.get('emoji', '⚠️')} <b>Ojo:</b> {ver.get('texto', '')}")
        if alt:
            lineas.append(f"👉 Mejor alternativa: <b>{alt}</b>. Manda /seguir para verla.")
    else:
        momento = f"el <b>{cuando.split()[0]}</b> " if cuando else ""
        lineas.append(
            f"👉 <b>Qué hacer:</b> manda <code>/seguir</code> {momento}~1h antes de su partido "
            "y te confirmo su alineación. Antes de eso no necesitas hacer nada."
        )

    otras = [it["equipo"] for it in items if it.get("equipo") != rec.get("equipo")][:2]
    if otras:
        lineas.append("")
        lineas.append(f"🔁 <i>Respaldo (solo si su XI sale mal): {', '.join(otras)}.</i>")

    try:
        from src import seguimiento_jornada as _seg

        alt_resp = _seg.alternativa_con_respaldo(items, rec)
        if alt_resp:
            lineas.append("")
            lineas.append(
                f"⚠️ <b>Ojo con el timing:</b> {rec['equipo']} juega de los últimos"
                f"{(' (' + cuando + ')') if cuando else ''}. Si su alineación sale mal, "
                "casi no quedan partidos de respaldo."
            )
            alt_cuando = f" ({alt_resp['cuando']})" if alt_resp.get("cuando") else ""
            lineas.append(
                f"🛡️ Opción CON respaldo: <b>{alt_resp['equipo']}</b>{alt_cuando} — "
                f"sobrevive {_pct(alt_resp['no_perder_pct'])}%. Si su XI sale bien lo aseguras "
                "temprano; si no, aún te quedan partidos por jugar."
            )
            alt_ver = alt_resp.get("veredicto") or {}
            if alt_ver.get("estado") and alt_ver["estado"] != "PENDIENTE":
                lineas.append(f"{alt_ver.get('emoji', '')} {alt_resp['equipo']}: {alt_ver.get('texto', '')}")
    except Exception:
        pass

    lineas.append("")
    lineas.append(
        "💡 <i>Si te preocupa el internet, puedes meter tu pick en PlayDoit desde ya "
        "y cambiarlo solo si su alineación sale mermada.</i>"
    )
    lineas += ["", DISCLAIMER]
    return "\n".join(lineas)
