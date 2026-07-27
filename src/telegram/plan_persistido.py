from __future__ import annotations

import html
import logging
from typing import Any, Dict, List, Optional

from . import envio as envio_mod
from .utils import _pct

logger = logging.getLogger(__name__)

DISCLAIMER = "ℹ️ Informativo / revisión humana. No es consejo de apuesta."
_ESTADOS_CERRADOS = {"confirmado", "bloqueado", "resuelto"}


def _cargar_historial_cerrado() -> List[Dict[str, Any]]:
    """Lee las selecciones reales sin impedir el plan si la BD no está disponible."""
    try:
        from src import database as db

        temporada = db.temporada_survivor_actual()
        picks = db.get_survivor_picks(temporada)
    except Exception:
        logger.warning("No se pudo leer el historial cerrado de Survivor", exc_info=True)
        return []

    historial: List[Dict[str, Any]] = []
    for pick in picks:
        estado = str(pick.get("estado") or "").lower()
        if estado not in _ESTADOS_CERRADOS:
            continue
        try:
            jornada = int(str(pick.get("jornada")))
        except (TypeError, ValueError):
            continue
        historial.append(
            {
                "jornada": jornada,
                "equipo": str(pick.get("equipo") or ""),
                "estado": estado,
                "resultado": str(pick.get("resultado") or "").lower() or None,
            }
        )
    historial.sort(key=lambda item: item["jornada"])
    return historial


def _plan_temporada(
    equipos_usados: Optional[List[str]],
    peso_victoria: float = 0.5,
    usar_momios: bool = True,
    jornada_desde: Optional[int] = None,
    permitir_descarga: bool = True,
) -> Dict[str, Any]:
    """Planifica solo las jornadas que aún no tienen una selección real cerrada."""
    from src import fuentes_datos
    from src import planificador_survivor as plan_mod
    from src import poisson_model as pm

    historial = _cargar_historial_cerrado()
    jornadas_cerradas = {int(item["jornada"]) for item in historial}
    calendario = plan_mod.cargar_calendario()
    if not calendario:
        return {"calendario_incompleto": True, "plan": [], "historial_cerrado": historial}

    jornada_desde = jornada_desde if jornada_desde is not None else envio_mod._jornada_actual_num()
    if jornada_desde is None:
        return {
            "calendario_incompleto": False,
            "temporada_finalizada": True,
            "plan": [],
            "historial_cerrado": historial,
        }

    calendario_filtrado: List[Dict[str, Any]] = []
    for bloque in calendario:
        try:
            jornada = int(str(bloque.get("jornada")))
        except (TypeError, ValueError):
            logger.warning("Se ignoró una jornada inválida del calendario: %r", bloque.get("jornada"))
            continue
        if jornada >= jornada_desde and jornada not in jornadas_cerradas:
            calendario_filtrado.append(bloque)

    if not calendario_filtrado:
        return {
            "calendario_incompleto": False,
            "temporada_finalizada": True,
            "plan": [],
            "historial_cerrado": historial,
        }

    try:
        resultados = fuentes_datos.leer_cache()
        if not resultados and permitir_descarga:
            datos = fuentes_datos.obtener_resultados(meses=18)
            datos_resultados = datos.get("resultados")
            resultados = datos_resultados if isinstance(datos_resultados, list) else []
        if not resultados:
            raise ValueError("No hay resultados históricos en caché para calcular fuerzas")
        fuerzas = pm.calcular_fuerzas(resultados)
        odds = plan_mod.construir_odds_por_partido(calendario_filtrado) if usar_momios else None
        resultado = plan_mod.planificar(
            calendario_filtrado,
            fuerzas,
            equipos_usados=equipos_usados,
            peso_victoria=peso_victoria,
            odds_por_partido=odds,
        )
        if not isinstance(resultado, dict):
            resultado = {"calendario_incompleto": True, "plan": []}
    except Exception as exc:
        logger.warning("No se pudo construir el plan restante de temporada", exc_info=True)
        resultado = {"calendario_incompleto": True, "plan": [], "error": str(exc)}

    resultado["historial_cerrado"] = historial
    resultado["jornada_plan_desde"] = min(int(bloque["jornada"]) for bloque in calendario_filtrado)
    return resultado


def _fecha_inicio_torneo(calendario: List[Dict[str, Any]]) -> Optional[str]:
    """Extrae la fecha del primer partido del calendario como inicio del torneo."""
    fechas: List[str] = []
    for bloque in calendario:
        for partido in bloque.get("partidos") or []:
            fecha = str(partido.get("fecha") or "")[:10]
            if fecha:
                fechas.append(fecha)
    return min(fechas) if fechas else None


def _aplicar_tendencias(
    plan: Dict[str, Any],
    equipos_usados: Optional[List[str]],
    peso_victoria: float,
    usar_momios: bool,
    permitir_descarga: bool,
) -> bool:
    """Aplica tendencias del torneo al plan si hay datos disponibles. No bloqueante."""
    pasos = plan.get("plan")
    if not isinstance(pasos, list) or not pasos:
        return False

    try:
        from src import fuentes_datos
        from src import planificador_survivor as plan_mod
        from src import poisson_model as pm
        from src import tendencias_torneo as tt

        calendario = plan_mod.cargar_calendario()
        if not calendario:
            return False

        fecha_inicio = _fecha_inicio_torneo(calendario)
        datos_torneo = tt.cargar_resultados_torneo_actual(fecha_inicio)
        resultados_torneo = datos_torneo.get("resultados")
        if not isinstance(resultados_torneo, list) or not resultados_torneo:
            return False

        tendencias = tt.calcular_tendencias(resultados_torneo, None)
        if not tendencias:
            return False

        historial = plan.get("historial_cerrado") or []
        jornadas_cerradas = {int(item["jornada"]) for item in historial}
        jornada_desde = plan.get("jornada_plan_desde")
        if jornada_desde is None:
            return False

        calendario_filtrado: List[Dict[str, Any]] = []
        for bloque in calendario:
            try:
                jornada = int(str(bloque.get("jornada")))
            except (TypeError, ValueError):
                continue
            if jornada >= jornada_desde and jornada not in jornadas_cerradas:
                calendario_filtrado.append(bloque)
        if not calendario_filtrado:
            return False

        resultados = fuentes_datos.leer_cache()
        if not resultados and permitir_descarga:
            datos = fuentes_datos.obtener_resultados(meses=18)
            datos_resultados = datos.get("resultados")
            resultados = datos_resultados if isinstance(datos_resultados, list) else []
        if not resultados:
            return False

        fuerzas = tt.ajustar_fuerzas(pm.calcular_fuerzas(resultados), tendencias)
        odds = plan_mod.construir_odds_por_partido(calendario_filtrado) if usar_momios else None
        nuevo = plan_mod.planificar(
            calendario_filtrado,
            fuerzas,
            equipos_usados=equipos_usados,
            peso_victoria=peso_victoria,
            odds_por_partido=odds,
        )
        if isinstance(nuevo, dict):
            plan["plan"] = nuevo.get("plan", plan.get("plan"))
            plan["prob_supervivencia_total_pct"] = nuevo.get(
                "prob_supervivencia_total_pct", plan.get("prob_supervivencia_total_pct")
            )
            plan["victorias_esperadas"] = nuevo.get("victorias_esperadas", plan.get("victorias_esperadas"))

        logger.info("Tendencias del torneo aplicadas: %d equipos con señal", len(tendencias))
        return True
    except Exception:
        logger.debug("Capa de tendencias no disponible", exc_info=True)
        return False


def _cargar_contextos_web(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Lee contexto cacheado; nunca hace búsquedas ni bloquea /plan."""
    try:
        from src.contexto_web_partidos import contextos_para_plan

        return contextos_para_plan(plan, limite=3)
    except Exception:
        logger.debug("Contexto web no disponible para el plan", exc_info=True)
        return []


def construir_plan_persistido(
    equipos_usados: Optional[List[str]],
    peso_victoria: float = 0.5,
    usar_momios: bool = True,
    jornada_desde: Optional[int] = None,
    permitir_descarga: bool = True,
    incluir_contextos: bool = False,
) -> Dict[str, Any]:
    """Construye el plan canónico que comparten /plan y /pick."""
    plan = _plan_temporada(
        equipos_usados,
        peso_victoria=peso_victoria,
        usar_momios=usar_momios,
        jornada_desde=jornada_desde,
        permitir_descarga=permitir_descarga,
    )
    plan["tendencias_aplicadas"] = _aplicar_tendencias(
        plan,
        equipos_usados,
        peso_victoria,
        usar_momios,
        permitir_descarga,
    )
    if incluir_contextos:
        plan["contextos_web"] = _cargar_contextos_web(plan)
    return plan


def _estado_historial(item: Dict[str, Any]) -> str:
    estado = str(item.get("estado") or "").lower()
    resultado = str(item.get("resultado") or "").lower()
    if estado == "resuelto":
        if resultado == "gano":
            return "🔒 ✅ Ganó"
        if resultado == "empate":
            return "🔒 🤝 Empató"
        if resultado == "perdio":
            return "🔒 ❌ Perdió"
        return "🔒 Resuelto"
    if estado == "bloqueado":
        return "🔒 ⏳ Bloqueado"
    return "✅ Confirmado"


def construir_mensaje_plan_persistido(plan: Dict[str, Any]) -> str:
    """Muestra picks reales, plan pendiente y contexto web cacheado."""
    historial = plan.get("historial_cerrado") or []
    futuro = plan.get("plan") or []
    if not historial and (plan.get("calendario_incompleto") or not futuro):
        return (
            "📅 <b>PLAN SURVIVOR</b>\n\n"
            "Aún no hay calendario completo (data/calendario.json). "
            "Córrelo de nuevo cuando se publique el calendario del torneo.\n\n"
            f"{DISCLAIMER}"
        )

    lineas = ["📅 <b>PLAN SURVIVOR — temporada</b> (modelo · datos ESPN)"]
    if historial:
        lineas.extend(["", "<b>Historial cerrado</b>"])
        for item in historial:
            jornada = int(item.get("jornada") or 0)
            equipo = str(item.get("equipo") or "—")
            lineas.append(f"<b>J{jornada} · {equipo}</b> {_estado_historial(item)}")

    if futuro:
        desde = plan.get("jornada_plan_desde") or min(int(pick["jornada"]) for pick in futuro)
        lineas.extend(
            [
                "",
                f"<b>Plan restante desde J{desde}</b>",
                (
                    f"<i>🛡️ Sobrevivir el tramo restante: "
                    f"{_pct(plan.get('prob_supervivencia_total_pct'))}% · "
                    f"🏆 victorias esperadas: {plan.get('victorias_esperadas')}</i>"
                ),
                "<i>Los picks cerrados no se recalculan ni consumen otro equipo.</i>",
                "",
            ]
        )
        if plan.get("tendencias_aplicadas"):
            lineas.append(
                "<i>🔍 Tendencias del torneo en vivo aplicadas al plan (rachas, forma, etiquetas automatizadas).</i>\n"
            )
        contextos = plan.get("contextos_web") or []
        if contextos:
            lineas.append("<i>🌐 Alertas web de previa (titulares filtrados):</i>")
            for contexto in contextos:
                jornada_contexto = html.escape(str(contexto.get("jornada") or "—"))
                equipo = html.escape(str(contexto.get("equipo") or "—"))
                resumen = html.escape(str(contexto.get("resumen") or ""))[:300]
                lineas.append(f"• J{jornada_contexto} <b>{equipo}</b>: {resumen}")
            lineas.append("")
        for pick in futuro:
            lineas.append(f"<b>J{pick['jornada']} · {pick['equipo']}</b> ({pick['condicion']} vs {pick['rival']})")
            lineas.append(
                f"🏆 gana {_pct(pick['prob_ganar_pct'])}% · "
                f"🛡️ sobrevive {_pct(pick['no_perder_pct'])}% [{pick['nivel']}]"
            )
        riesgosas = plan.get("jornadas_riesgosas") or []
        if riesgosas:
            jornadas_texto = ", ".join("J" + str(jornada) for jornada in riesgosas)
            lineas.extend(["", f"⚠️ Jornadas riesgosas: {jornadas_texto}"])
    elif plan.get("temporada_finalizada"):
        lineas.extend(["", "No quedan jornadas pendientes por planificar."])

    lineas.extend(["", DISCLAIMER])
    return "\n".join(lineas)


def enviar_plan(
    equipos_usados: Optional[List[str]] = None,
    peso_victoria: float = 0.5,
    usar_momios: bool = True,
) -> Dict[str, Any]:
    """Envía historial, plan y contexto web cacheado sin ampliar la ruta crítica."""
    if equipos_usados is None:
        equipos_usados = envio_mod._usados_persistidos()
    plan = construir_plan_persistido(
        equipos_usados,
        peso_victoria=peso_victoria,
        usar_momios=usar_momios,
        incluir_contextos=True,
    )

    mensaje = (
        "🧠 <b>ANÁLISIS INTELIGENTE</b>\n"
        "<i>Plan optimizado para sobrevivir sin repetir equipo y priorizar victorias.</i>\n\n"
        + construir_mensaje_plan_persistido(plan)
    )
    enviado = envio_mod.enviar_mensaje(mensaje)
    pasos = plan.get("plan")
    contextos = plan.get("contextos_web")
    return {
        "enviado": enviado,
        "jornadas": len(pasos) if isinstance(pasos, list) else 0,
        "calendario_incompleto": bool(plan.get("calendario_incompleto")),
        "tendencias_aplicadas": bool(plan.get("tendencias_aplicadas")),
        "contextos_web": len(contextos) if isinstance(contextos, list) else 0,
    }
