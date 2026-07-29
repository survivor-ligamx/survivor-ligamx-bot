from __future__ import annotations

from html import escape
from typing import Any, Dict, List, Optional

from src import calendario_contexto as calctx
from .formato_pick import DISCLAIMER, render_survivor
from .formato_partidos import render_partidos
from .oracion import oracion_para_pick
from .totales import calcular_totales_jornada
from .utils import _fecha_mx, _pct


def _contexto_web_top(tops: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Lee exclusivamente la caché del top pick; nunca hace búsquedas externas."""
    if not tops or not isinstance(tops[0], dict):
        return None
    pick = tops[0]
    equipo, rival = str(pick.get("equipo") or ""), str(pick.get("rival") or "")
    if not equipo or not rival:
        return None
    local, visitante = (equipo, rival) if pick.get("condicion") == "Local" else (rival, equipo)
    try:
        jornada = int(pick.get("jornada"))
    except (TypeError, ValueError):
        jornada = None
    try:
        from src.contexto_web_partidos import contexto_cache_partido

        return contexto_cache_partido(local, visitante, jornada)
    except Exception:
        return None


def _render_contexto_web(contexto: Optional[Dict[str, Any]]) -> List[str]:
    if not contexto or not contexto.get("disponible"):
        return []
    estado = str(contexto.get("estado") or "PROVISIONAL")
    frescura = str(contexto.get("frescura") or "DESCONOCIDA")
    actualizado = str(contexto.get("actualizado_en") or "")[:16].replace("T", " ")
    lineas = ["🌐 <b>DISPONIBILIDAD DEL PICK (caché)</b>", f"Estado: <b>{escape(estado)}</b> · frescura {escape(frescura)}"]
    if actualizado:
        lineas.append(f"Actualizado: {escape(actualizado)} UTC")
    eventos = contexto.get("eventos") if isinstance(contexto.get("eventos"), list) else []
    for evento in eventos[:3]:
        if not isinstance(evento, dict) or evento.get("tipo") == "OTRO":
            continue
        equipo = escape(str(evento.get("equipo") or ""))
        tipo = escape(str(evento.get("tipo") or "REVISAR"))
        titulo = escape(str(evento.get("titulo") or ""))
        lineas.append(f"• ⚠️ {equipo} [{tipo}]: {titulo}" if equipo else f"• ⚠️ [{tipo}]: {titulo}")
    fuentes = contexto.get("fuentes") if isinstance(contexto.get("fuentes"), list) else []
    if fuentes:
        lineas.append(f"🔗 {len(fuentes[:3])} fuente(s) contrastadas")
    if estado not in {"CONFIRMADO", "CONFIRMADO_CON_XI"}:
        lineas.append("<i>No cambia probabilidades: requiere confirmación o XI oficial.</i>")
    return lineas


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
    """Arma el mensaje HTML de pronósticos a partir de la salida del motor."""
    pronosticos = resultado.get("pronosticos", [])
    fecha = _fecha_mx(resultado.get("generado_utc", ""))
    div = "━━━━━━━━━━"
    lineas = ["🔮 <b>PRONÓSTICOS LIGA MX</b>", "<i>Modelo ESPN + Poisson</i>", f"🕒 <i>{fecha}</i>", div]

    survivor_lineas = render_survivor(pronosticos, equipos_usados, motivacion, tops, advertencia, contexto_pick)
    lineas.extend(survivor_lineas)
    contexto_web = _render_contexto_web(_contexto_web_top(tops))
    if contexto_web:
        lineas.append("")
        lineas.extend(contexto_web)
    if survivor_lineas:
        lineas.extend(["", oracion_para_pick(tops)])

    try:
        cal_lineas = calctx.resumen_jornada(pronosticos)
    except Exception:
        cal_lineas = []
    if cal_lineas:
        lineas.extend([div, "🗓️ <b>CONTEXTO DE CALENDARIO</b>", "<i>Afecta disponibilidad/desgaste:</i>"])
        lineas.extend(f"• {c}" for c in cal_lineas)

    lineas.extend(render_partidos(pronosticos, goleadores_map, porteros_map))
    if pronosticos:
        lineas.extend([div, "📊 <b>TOTALES DE LA JORNADA</b>"])
        totales = calcular_totales_jornada(pronosticos)
        lineas.append(f"📋 Cobertura: {totales['partidos']}/{totales['partidos_esperados']} partidos · modelo de goles {totales['partidos_con_xg']}/{totales['partidos_esperados']}")
        if totales["goles_desempate"] is not None:
            lineas.append(f"🎯 <b>Pronóstico para desempate: {totales['goles_desempate']} goles</b>")
        else:
            lineas.append(f"⚠️ Total provisional: faltan {totales['partidos_sin_xg']} partidos con modelo. No lo uses todavía como desempate definitivo.")
        lineas.extend([
            f"⚽ xG acumulado disponible: {totales['goles_esperados_total']}",
            f"📊 Promedio por partido modelado: {totales['promedio_goles_partido']}",
            f"🔺 Over 2.5: {totales['over_25_count']} partidos",
            f"🔻 Under 2.5: {totales['under_25_count']} partidos",
            f"✅ BTTS Sí: {totales['btts_si_count']} partidos",
            f"❌ BTTS No: {totales['btts_no_count']} partidos",
        ])
    lineas += [div, DISCLAIMER]
    return "\n".join(lineas)


def construir_mensaje_seguimiento(
    items: List[Dict[str, Any]],
    descartados: Optional[List[str]] = None,
    recomendado: Optional[Dict[str, Any]] = None,
    nota_plan: Optional[str] = None,
) -> str:
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
    lineas = ["🎯 <b>TU PICK DE SURVIVOR</b>", f"✅ <b>{rec['equipo']}</b>", f"{_sede(rec)} vs {rec['rival']}", f"Sobrevive {_pct(rec['no_perder_pct'])}%{gtxt}", f"Confianza <b>{rec.get('nivel', '—')}</b>"]
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
        lineas.append(f"👉 <b>Qué hacer:</b> manda <code>/seguir</code> {momento}~1h antes de su partido y te confirmo su alineación. Antes de eso no necesitas hacer nada.")
    otras = [it["equipo"] for it in items if it.get("equipo") != rec.get("equipo")][:2]
    if otras:
        lineas.extend(["", f"🔁 <i>Respaldo (solo si su XI sale mal): {', '.join(otras)}.</i>"])
    try:
        from src import seguimiento_jornada as _seg
        alt_resp = _seg.alternativa_con_respaldo(items, rec)
        if alt_resp:
            lineas.extend(["", f"⚠️ <b>Ojo con el timing:</b> {rec['equipo']} juega de los últimos{(' (' + cuando + ')') if cuando else ''}. Si su alineación sale mal, casi no quedan partidos de respaldo."])
            alt_cuando = f" ({alt_resp['cuando']})" if alt_resp.get("cuando") else ""
            lineas.append(f"🛡️ Opción CON respaldo: <b>{alt_resp['equipo']}</b>{alt_cuando} — sobrevive {_pct(alt_resp['no_perder_pct'])}%. Si su XI sale bien lo aseguras temprano; si no, aún te quedan partidos por jugar.")
            alt_ver = alt_resp.get("veredicto") or {}
            if alt_ver.get("estado") and alt_ver["estado"] != "PENDIENTE":
                lineas.append(f"{alt_ver.get('emoji', '')} {alt_resp['equipo']}: {alt_ver.get('texto', '')}")
    except Exception:
        pass
    lineas.extend(["", "💡 <i>Si te preocupa el internet, puedes meter tu pick en PlayDoit desde ya y cambiarlo solo si su alineación sale mermada.</i>", "", DISCLAIMER])
    return "\n".join(lineas)
