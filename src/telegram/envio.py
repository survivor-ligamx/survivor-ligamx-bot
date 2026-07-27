from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from src import motor_pronosticos as motor
from .contexto import _ajustar_pick_top, _contexto_top_pick
from .formato import construir_mensaje, construir_mensaje_seguimiento
from .formato_partidos import construir_mensaje_momios, construir_recordatorio
from .formato_pick import (
    construir_mensaje_confianza,
    construir_mensaje_derrotas,
    construir_mensaje_ganadores,
    construir_mensaje_plan,
    construir_mensaje_prueba,
    construir_mensaje_rentabilidad,
)

logger = logging.getLogger(__name__)

_MAX_PARTIDOS = 9
_CALENDARIO_PATH = Path(__file__).resolve().parents[2] / "data" / "calendario.json"
_TELEGRAM_LIMITE = 4000
DISCLAIMER = "ℹ️ Informativo / revisión humana. No es consejo de apuesta."


def _usados_persistidos() -> Optional[List[str]]:
    """Equipos usados guardados en la BD (para excluir del pick/plan). None si falla."""
    try:
        from src.database import get_equipos_usados

        return list(get_equipos_usados())
    except Exception:  # pragma: no cover - BD no disponible
        return None


def _partidos_jugados_torneo() -> Optional[int]:
    """Partidos jugados del torneo actual (para la cautela de arranque). None si falla."""
    try:
        from src import ligamx_api as lmx

        finished_matches = lmx.estado_temporada().get("finished_matches")
        if finished_matches is None:
            return None
        return int(finished_matches)
    except (RuntimeError, TypeError, ValueError):  # pragma: no cover - API no disponible
        logger.warning("No se pudo obtener el total de partidos jugados", exc_info=True)
        return None


def _cerca_de_jornada(pronosticos, dias: int = 2) -> bool:
    """
    True si el partido más próximo de la jornada arranca dentro de `dias` (día de
    jornada). En ese caso vale la pena DESPERTAR la API hermana y esperar por los
    extras; lejos de la jornada, mejor responder rápido y sin enriquecer.
    """
    hoy = datetime.now(timezone.utc).date()
    fechas = []
    for p in pronosticos or []:
        s = str(p.get("fecha", ""))[:10]
        try:
            y, m, d = s.split("-")
            fechas.append(date(int(y), int(m), int(d)))
        except (ValueError, TypeError):
            continue
    if not fechas:
        return False
    return (min(fechas) - hoy).days <= dias


def _dividir_mensaje(texto: str, limite: int = _TELEGRAM_LIMITE) -> List[str]:
    """
    Parte un mensaje largo en trozos <= `limite`, cortando SIEMPRE en saltos de
    línea (nunca a media línea) para respetar el tope de Telegram (~4096) y no
    romper etiquetas HTML (cada línea abre y cierra las suyas).
    """
    if len(texto) <= limite:
        return [texto]
    partes: List[str] = []
    actual = ""
    for linea in texto.split("\n"):
        while len(linea) > limite:
            if actual:
                partes.append(actual)
                actual = ""
            partes.append(linea[:limite])
            linea = linea[limite:]
        if actual and len(actual) + 1 + len(linea) > limite:
            partes.append(actual)
            actual = linea
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        partes.append(actual)
    return partes


def enviar_mensaje(mensaje: str, idempotency_key: Optional[str] = None) -> bool:
    """
    Envía un mensaje a Telegram. Si excede el tope (~4096), lo parte en varios
    y los manda en orden. Devuelve True solo si TODOS los trozos se enviaron.
    """
    if requests is None:
        print("⚠️ 'requests' no instalado; no se envía.")
        return False
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("⚠️ Telegram no configurado (faltan TELEGRAM_BOT_TOKEN/CHAT_ID).")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for indice, parte in enumerate(_dividir_mensaje(mensaje), start=1):
        clave_parte = None
        if idempotency_key:
            digest = hashlib.sha256(parte.encode("utf-8")).hexdigest()[:16]
            clave_parte = f"{idempotency_key}:parte:{indice}:{digest}"
            try:
                from src.database import reclamar_entrega_telegram

                if not reclamar_entrega_telegram(clave_parte):
                    continue
            except Exception:
                logger.warning("No se pudo reclamar la entrega Telegram", exc_info=True)
                ok = False
                continue
        try:
            resp = requests.post(url, data={"chat_id": chat_id, "text": parte, "parse_mode": "HTML"}, timeout=20)
            if resp.status_code == 200:
                if clave_parte:
                    from src.database import completar_entrega_telegram

                    completar_entrega_telegram(clave_parte)
            else:
                if clave_parte:
                    from src.database import fallar_entrega_telegram

                    fallar_entrega_telegram(clave_parte, f"HTTP {resp.status_code}")
                ok = False
                print(f"Telegram HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:  # pragma: no cover
            if clave_parte:
                try:
                    from src.database import fallar_entrega_telegram

                    fallar_entrega_telegram(clave_parte, type(exc).__name__)
                except Exception:
                    logger.warning("No se pudo marcar la entrega Telegram como fallida", exc_info=True)
            # Nunca registrar URL ni token de Telegram.
            logger.error("Error enviando Telegram (%s)", type(exc).__name__)
            ok = False
    return ok


def _registrar_historial(pronosticos) -> None:
    """Guarda los pronósticos en el track-record (dedup por equipos+fecha). Tolerante."""
    try:
        from src.database import registrar_pronostico
    except Exception:  # pragma: no cover
        return
    for p in pronosticos or []:
        try:
            registrar_pronostico(
                p.get("local", ""),
                p.get("visitante", ""),
                p.get("pick_1x2", ""),
                p.get("prob_local_pct", 0),
                p.get("prob_empate_pct", 0),
                p.get("prob_visitante_pct", 0),
                p.get("marcador_pick") or p.get("marcador_mas_probable", ""),
                fecha=p.get("fecha", ""),
            )
        except Exception:  # pragma: no cover
            continue


def _clave_jornada_historial(jornada: Any, fecha: str = "") -> str:
    """Clave estable por torneo para evitar colisiones entre jornadas 1..17."""
    fecha_ref = _fecha(fecha) or datetime.now(timezone.utc).date()
    torneo = "Apertura" if fecha_ref.month >= 7 else "Clausura"
    return f"{torneo}-{fecha_ref.year}-J{jornada}"


def _registrar_survivor_historial(picks, pronosticos) -> None:
    """Registra el pick #1 de Survivor de la jornada en el track-record."""
    if not picks:
        return
    try:
        from src.database import registrar_pick_recomendado, registrar_survivor_pick
        from src.team_normalizer import canonical_team_key
    except ImportError:  # pragma: no cover - instalación incompleta
        logger.warning("No se pudo importar la persistencia del historial Survivor", exc_info=True)
        return

    pick = picks[0]
    equipo = str(pick.get("equipo") or "").strip()
    if not equipo:
        return

    local = ""
    visitante = ""
    fecha = ""
    partido_match: Dict[str, Any] = {}
    equipo_key = canonical_team_key(equipo)
    for pronostico in pronosticos or []:
        local_candidato = str(pronostico.get("local") or "")
        visitante_candidato = str(pronostico.get("visitante") or "")
        if equipo_key in {
            canonical_team_key(local_candidato),
            canonical_team_key(visitante_candidato),
        }:
            local = local_candidato
            visitante = visitante_candidato
            fecha = str(pronostico.get("fecha") or "")
            partido_match = pronostico
            break

    condicion = str(pick.get("condicion") or "")
    rival = str(pick.get("rival") or "")
    if not local or not visitante:
        if condicion.lower().startswith("local"):
            local, visitante = equipo, rival
        else:
            local, visitante = rival, equipo

    jornada = pick.get("jornada") or _jornada_actual_num()
    if jornada is None:
        logger.warning("No se registró el pick Survivor porque no se pudo determinar la jornada")
        return

    try:
        clave_jornada = _clave_jornada_historial(jornada, fecha)
        registrar_pick_recomendado(
            temporada=clave_jornada.rsplit("-J", 1)[0],
            jornada=int(jornada),
            equipo=equipo,
            rival=rival,
            condicion=condicion,
            local=local,
            visitante=visitante,
            no_perder_pct=float(pick.get("no_perder_pct") or 0.0),
            prob_victoria_pct=float(pick.get("prob_victoria_pct") or pick.get("prob_ganar_pct") or 0.0),
            fecha=fecha,
            espn_event_id=partido_match.get("espn_event_id"),
            match_key=str(partido_match.get("match_key") or ""),
            kickoff_utc=str(partido_match.get("kickoff_utc") or partido_match.get("fecha") or ""),
            probability_snapshot={
                "no_perder_pct": float(pick.get("no_perder_pct") or 0.0),
                "prob_victoria_pct": float(pick.get("prob_victoria_pct") or pick.get("prob_ganar_pct") or 0.0),
            },
            model_version=os.getenv("SURVIVOR_MODEL_VERSION", "survivor-v1"),
            decision_reason=str(pick.get("razon") or pick.get("motivo") or ""),
        )
        registrar_survivor_pick(
            jornada=clave_jornada,
            equipo=equipo,
            rival=rival,
            condicion=condicion,
            local=local,
            visitante=visitante,
            no_perder_pct=float(pick.get("no_perder_pct") or 0.0),
            prob_victoria_pct=float(pick.get("prob_victoria_pct") or pick.get("prob_ganar_pct") or 0.0),
            fecha=fecha,
        )
    except Exception:  # pragma: no cover - BD no disponible
        logger.warning("No se pudo registrar el pick en el historial Survivor", exc_info=True)


def _jornada_actual_num(hoy: Optional[date] = None) -> Optional[int]:
    """Número de la jornada actual o próxima (1..17), según el calendario local."""
    jornada = proxima_jornada(hoy)
    if not jornada:
        return None
    try:
        numero = int(str(jornada.get("jornada")))
    except (TypeError, ValueError):
        logger.warning("El calendario contiene una jornada inválida: %r", jornada.get("jornada"))
        return None
    return numero if 1 <= numero <= 17 else None


def _plan_temporada(
    equipos_usados: Optional[List[str]],
    peso_victoria: float = 0.5,
    usar_momios: bool = True,
    jornada_desde: Optional[int] = None,
    permitir_descarga: bool = True,
) -> Dict[str, Any]:
    """Construye el mismo plan canónico que usa /plan."""
    from . import plan_persistido

    return plan_persistido.construir_plan_persistido(
        equipos_usados,
        peso_victoria=peso_victoria,
        usar_momios=usar_momios,
        jornada_desde=jornada_desde,
        permitir_descarga=permitir_descarga,
    )


def _rec_desde_plan(plan: Dict[str, Any], jornada_num: Optional[int]) -> Optional[Dict[str, Any]]:
    """Extrae el pick recomendado para la jornada X según el plan."""
    if not plan or jornada_num is None:
        return None
    pasos = plan.get("plan")
    if not isinstance(pasos, list):
        return None
    for paso in pasos:
        if not isinstance(paso, dict):
            continue
        try:
            misma_jornada = int(str(paso.get("jornada"))) == jornada_num
        except (TypeError, ValueError):
            continue
        if misma_jornada:
            recomendacion = dict(paso)
            if "prob_victoria_pct" not in recomendacion and recomendacion.get("prob_ganar_pct") is not None:
                recomendacion["prob_victoria_pct"] = recomendacion["prob_ganar_pct"]
            return recomendacion
    return None


def _cargar_calendario_local() -> List[Dict[str, Any]]:
    """Lee data/calendario.json."""
    try:
        if _CALENDARIO_PATH.exists():
            with open(_CALENDARIO_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
    except Exception:  # pragma: no cover
        pass
    return []


def _fecha(valor: Any) -> Optional[date]:
    try:
        return datetime.strptime(str(valor)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def proxima_jornada(hoy: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """Jornada cuya fecha cubre a hoy (o la más próxima futura)."""
    hoy = hoy or datetime.now(timezone.utc).date()
    candidatas = []
    for j in _cargar_calendario_local():
        ini = _fecha(j.get("fecha_inicio"))
        fin = _fecha(j.get("fecha_fin"))
        if ini is not None and fin is not None:
            if ini <= hoy <= fin:
                return j
            if ini > hoy:
                candidatas.append((ini, j))
    if candidatas:
        candidatas.sort(key=lambda t: t[0])
        return candidatas[0][1]
    return None


def enviar_pronosticos(
    equipos_usados: Optional[List[str]] = None,
    incluir_contexto: bool = True,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Genera pronósticos reales y los envía por Telegram."""
    resultado = motor.generar_pronosticos()
    pronosticos = resultado.get("pronosticos", [])
    _registrar_historial(pronosticos)

    if equipos_usados is None:
        equipos_usados = _usados_persistidos()

    con_momios = 0
    try:
        from src import comparador_mercado as cm
        from src import ligamx_api as lmx

        try:
            momios, fuente_m = cm.momios_para_uso()
        except Exception:
            momios, fuente_m = {}, None
        pron_anotados = cm.anotar_pronosticos(pronosticos, momios)
        resultado["pronosticos"] = cm.mezclar_pronosticos_con_mercado(pron_anotados, momios=momios)
        con_momios = sum(1 for m in (momios or {}).values() if isinstance(m, dict) and m.get("ml"))

        # Fallback fixtures_sin_modelo
        try:
            faltantes = resultado.get("fixtures_sin_modelo") or []
            extra = []
            for fx in faltantes:
                merc = cm.buscar_mercado_partido(fx.get("home_team", ""), fx.get("away_team", ""), momios or {})
                pr = cm.pronostico_desde_momios(
                    fx.get("home_team", ""), fx.get("away_team", ""), merc, fx.get("fecha", "")
                )
                if pr:
                    extra.append(pr)
            if extra:
                resultado["pronosticos"] = list(resultado.get("pronosticos", [])) + extra
        except Exception:
            pass

        # Archivo momios
        try:
            snaps = cm.construir_snapshots_momios(pron_anotados, momios, source=fuente_m)
            if snaps:
                lmx.archivar_momios(snaps)
        except Exception:
            pass
    except Exception:
        pass

    try:
        motivacion = motor.motivacion_por_equipo()
    except Exception:
        motivacion = {}

    cerca = _cerca_de_jornada(resultado.get("pronosticos", []))
    api_ok = False
    try:
        from src import ligamx_api as _lmx_probe

        api_ok = _lmx_probe.disponible(timeout=45 if cerca else 5)
    except Exception:
        api_ok = False

    est = motor.mejores_picks_estrategico(
        resultado.get("pronosticos", []),
        equipos_usados,
        motivacion,
        partidos_jugados_torneo=_partidos_jugados_torneo() if api_ok else None,
        n=3,
    )

    # Unificación con plan
    try:
        from src.team_normalizer import canonical_team_key as _kp

        _picks = est.get("picks") or []
        _rec_plan = _rec_desde_plan(
            _plan_temporada(equipos_usados, permitir_descarga=False),
            _jornada_actual_num(),
        )
        if _rec_plan and _picks:
            _miope = str(_picks[0].get("equipo") or "")
            _equipo_plan = str(_rec_plan.get("equipo") or "")
            if _equipo_plan and _kp(_equipo_plan) != _kp(_miope):
                _rec_plan["razon"] = (
                    f"el plan de temporada guarda a {_miope} para una jornada más difícil y usa a {_equipo_plan} aquí."
                )
            else:
                _rec_plan["razon"] = "es el equipo de esta jornada según el plan de toda la temporada."
            _resto = [p for p in _picks if _kp(str(p.get("equipo") or "")) != _kp(_equipo_plan)]
            est["picks"] = [_rec_plan] + _resto
    except Exception:
        logger.warning("No se pudo unificar el pick de jornada con el plan de temporada", exc_info=True)

    contexto_pick = None
    if incluir_contexto and api_ok:
        _top = (est.get("picks") or [None])[0]
        contexto_pick = _contexto_top_pick(
            resultado.get("pronosticos", []), equipos_usados, motivacion, pick_override=_top
        )

    try:
        _ajustar_pick_top(est.get("picks") or [], resultado.get("pronosticos", []), contexto_pick)
    except Exception:
        pass

    try:
        _registrar_survivor_historial(est.get("picks") or [], resultado.get("pronosticos", []))
    except Exception:
        pass

    goleadores_map = None
    porteros_map = None
    if api_ok:
        try:
            from src import ligamx_api as lmx

            goleadores_map = lmx.goleadores_por_equipo()
            porteros_map = lmx.porteros_por_equipo()
        except Exception:
            pass

    mensaje = construir_mensaje(
        resultado,
        equipos_usados,
        motivacion,
        contexto_pick,
        tops=est.get("picks"),
        advertencia=est.get("advertencia"),
        goleadores_map=goleadores_map,
        porteros_map=porteros_map,
    )
    enviado = enviar_mensaje(mensaje, idempotency_key=idempotency_key)
    return {
        "enviado": enviado,
        "total_pronosticos": resultado.get("total_pronosticos", 0),
        "partidos_con_momios": con_momios,
        "fuente": resultado.get("fuente_datos"),
        "cautela": est.get("cautela"),
    }


def enviar_resumen_rentabilidad() -> Dict[str, Any]:
    """Envía por Telegram el resumen de aciertos del modelo."""
    try:
        from src.database import rentabilidad_pronosticos

        data = rentabilidad_pronosticos()
    except Exception as exc:
        return {"enviado": False, "error": str(exc)}
    enviado = enviar_mensaje(construir_mensaje_rentabilidad(data))
    return {"enviado": enviado, "resueltos": data.get("resueltos"), "pendientes": data.get("pendientes")}


def enviar_momios_estado(solo_si_hay: bool = False, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
    """Envía por Telegram un resumen de cobertura de momios."""
    try:
        from src import comparador_mercado as cm

        momios, fuente = cm.momios_para_uso(guardar_si_hay=True, incluir_gratis=True)
    except Exception as exc:
        return {"enviado": False, "error": str(exc)}
    if solo_si_hay and not momios:
        return {"enviado": False, "silencioso": True, "partidos_con_momios": 0}
    enviado = enviar_mensaje(construir_mensaje_momios(momios, fuente), idempotency_key=idempotency_key)
    return {"enviado": enviado, "partidos_con_momios": len(momios), "fuente": fuente}


def enviar_recordatorio_si_aplica(dias_antes: int = 1, hoy: Optional[date] = None) -> Dict[str, Any]:
    """Envía un recordatorio si la jornada está cerca."""
    hoy = hoy or datetime.now(timezone.utc).date()
    j = proxima_jornada(hoy)
    if not j:
        return {"enviado": False, "motivo": "sin próxima jornada"}
    ini = _fecha(j.get("fecha_inicio"))
    if ini is None:
        return {"enviado": False, "motivo": "fecha inválida"}
    dias = (ini - hoy).days
    if not (0 <= dias <= dias_antes):
        return {"enviado": False, "motivo": f"faltan {dias} días", "jornada": j.get("jornada")}
    clave = f"recordatorio:{hoy.isoformat()}:J{j.get('jornada')}:{dias_antes}"
    enviado = enviar_mensaje(construir_recordatorio(j, dias), idempotency_key=clave)
    return {"enviado": enviado, "jornada": j.get("jornada"), "dias": dias}


def enviar_analisis_jornada() -> Dict[str, Any]:
    """Actualiza la memoria postpartido y envía solo una confirmación compacta."""
    try:
        from src import analista_resultados as ar
    except Exception as exc:
        return {"enviado": False, "error": str(exc)}

    picks_anteriores = []
    try:
        from src.database import get_survivor_picks_recientes

        picks_anteriores = get_survivor_picks_recientes(limit=10)
    except Exception:
        pass

    resultado = ar.analizar_jornada(picks_anteriores=picks_anteriores)
    total = len(resultado.get("partidos", []))
    guardados = int(resultado.get("aprendizajes_guardados") or 0)
    alertas = resultado.get("alertas_internas") or []
    lineas = [
        "🧠 <b>MEMORIA POSTPARTIDO ACTUALIZADA</b>",
        f"✅ {total} partidos procesados",
        f"💾 {guardados} conclusiones internas nuevas o actualizadas",
        f"📉 {len(alertas)} sorpresa(s) o favorito(s) frenado(s) detectados con evidencia previa",
        "<i>Estos datos alimentan tendencias, forma y etiquetas del próximo /plan.</i>",
    ]
    enviado = enviar_mensaje("\n".join(lineas))
    return {
        "enviado": enviado,
        "total_partidos": total,
        "aprendizajes_guardados": guardados,
        "alertas_internas": len(alertas),
        "resumen": resultado.get("resumen", ""),
    }


def _mapa_horarios(lmx) -> Dict[str, str]:
    """{clave_equipo: match_date_iso} desde /matches/upcoming."""
    from src.team_normalizer import canonical_team_key as _k

    out: Dict[str, str] = {}
    try:
        for m in lmx.partidos_proximos(limit=20) or []:
            if not isinstance(m, dict):
                continue
            fecha = m.get("match_date")
            for lado in ("home_team", "away_team"):
                eq = m.get(lado) or {}
                nombre = eq.get("name") if isinstance(eq, dict) else eq
                if nombre and fecha:
                    out[_k(str(nombre))] = str(fecha)
    except Exception:
        pass
    return out


def enviar_seguimiento(equipos_usados: Optional[List[str]] = None, n: int = 5) -> Dict[str, Any]:
    """Envía por Telegram la lista de seguimiento para decidir el Survivor."""
    from src import seguimiento_jornada as seg
    from src import ligamx_api as lmx

    resultado = motor.generar_pronosticos()
    pronosticos = resultado.get("pronosticos", [])
    if equipos_usados is None:
        equipos_usados = _usados_persistidos()
    try:
        motivacion = motor.motivacion_por_equipo()
    except Exception:
        motivacion = {}
    est = motor.mejores_picks_estrategico(
        pronosticos, equipos_usados, motivacion, partidos_jugados_torneo=_partidos_jugados_torneo(), n=max(n, 5)
    )
    picks = est.get("picks") or []
    horarios = _mapa_horarios(lmx)

    fuerza_xi: Dict[str, float] = {}
    from src.team_normalizer import canonical_team_key as _k

    for pk in picks[:n]:
        es_local = pk.get("condicion") == "Local"
        home, away = (pk["equipo"], pk["rival"]) if es_local else (pk["rival"], pk["equipo"])
        try:
            imp = lmx.lineup_impact_partido(home, away)
            if isinstance(imp, dict) and imp.get("disponible"):
                for eq, info in (imp.get("equipos") or {}).items():
                    if isinstance(info, dict) and info.get("fuerza_xi_pct") is not None:
                        fuerza_xi[_k(eq)] = info["fuerza_xi_pct"]
        except Exception:
            pass

    items = seg.lista_seguimiento(picks, horarios=horarios, fuerza_xi=fuerza_xi, n=n)
    usados_set = {_k(e) for e in (equipos_usados or [])}
    seguidos = {_k(it["equipo"]) for it in items}
    descartados = [
        p.get("local", "")
        for p in pronosticos
        if _k(p.get("local", "")) not in seguidos and _k(p.get("local", "")) not in usados_set
    ][:6]

    recomendado = picks[0] if picks else None
    nota_plan = None
    try:
        plan = _plan_temporada(equipos_usados, permitir_descarga=False)
        rec_plan = _rec_desde_plan(plan, _jornada_actual_num())
        if rec_plan:
            miope = picks[0]["equipo"] if picks else None
            if miope and _k(rec_plan["equipo"]) != _k(miope):
                nota_plan = f"📅 Plan: usa <b>{rec_plan['equipo']}</b> esta jornada y GUARDA a {miope}."
            else:
                nota_plan = f"📅 Plan: <b>{rec_plan['equipo']}</b> es tu equipo de esta jornada."
            recomendado = rec_plan
            if not any(_k(it["equipo"]) == _k(rec_plan["equipo"]) for it in items):
                items = seg.lista_seguimiento([rec_plan], horarios, fuerza_xi, n=1) + items
    except Exception:
        pass

    mensaje = construir_mensaje_seguimiento(
        items, descartados=descartados, recomendado=recomendado, nota_plan=nota_plan
    )
    enviado = enviar_mensaje(mensaje)
    return {"enviado": enviado, "candidatos": len(items)}


def enviar_plan(
    equipos_usados: Optional[List[str]] = None,
    peso_victoria: float = 0.5,
    usar_momios: bool = True,
) -> Dict[str, Any]:
    """Construye y envía el plan inteligente de toda la temporada."""
    if equipos_usados is None:
        equipos_usados = _usados_persistidos()

    plan: Dict[str, Any] = _plan_temporada(
        equipos_usados,
        peso_victoria=peso_victoria,
        usar_momios=usar_momios,
    )
    mensaje_resumen = (
        "🧠 <b>ANÁLISIS INTELIGENTE</b>\n"
        "<i>Plan optimizado para sobrevivir sin repetir equipo y priorizar victorias.</i>\n"
    )
    plan_texto = construir_mensaje_plan(plan)
    enviado = enviar_mensaje(mensaje_resumen + "\n" + plan_texto)
    pasos = plan.get("plan")
    jornadas = len(pasos) if isinstance(pasos, list) else 0
    return {
        "enviado": enviado,
        "jornadas": jornadas,
        "calendario_incompleto": bool(plan.get("calendario_incompleto")),
    }


def enviar_prueba() -> Dict[str, Any]:
    """Backtest en lenguaje simple."""
    from src import fuentes_datos, backtest_estrategias as be

    try:
        datos = fuentes_datos.obtener_historico_largo()
        comp = be.comparar_estrategias(datos["resultados"])
    except Exception:
        comp = {}
    enviado = enviar_mensaje(construir_mensaje_prueba(comp))
    return {"enviado": enviado, "mejor": (comp or {}).get("mejor")}


def enviar_confianza() -> Dict[str, Any]:
    """Calibración en lenguaje simple."""
    from src import fuentes_datos, calibracion as cal

    try:
        datos = fuentes_datos.obtener_historico_largo()
        rep = cal.evaluar_calibracion(datos["resultados"])
    except Exception:
        rep = {}
    enviado = enviar_mensaje(construir_mensaje_confianza(rep))
    return {"enviado": enviado, "alpha": (rep or {}).get("alpha_sugerido")}


def enviar_derrotas() -> Dict[str, Any]:
    """Análisis de derrotas en lenguaje simple."""
    from src import fuentes_datos, backtest_estrategias as be

    try:
        datos = fuentes_datos.obtener_historico_largo()
        rep = be.analizar_derrotas(datos["resultados"])
    except Exception:
        rep = {}
    enviado = enviar_mensaje(construir_mensaje_derrotas(rep))
    return {"enviado": enviado, "derrotas": (rep or {}).get("total_derrotas")}


def enviar_ganadores() -> Dict[str, Any]:
    """Perfect Survivor vs Bot."""
    from src import fuentes_datos, backtest_estrategias as be

    try:
        datos = fuentes_datos.obtener_historico_largo()
        rep = be.analizar_ganadores(datos["resultados"])
    except Exception:
        rep = {}
    enviado = enviar_mensaje(construir_mensaje_ganadores(rep))
    return {"enviado": enviado, "torneos": (rep or {}).get("torneos")}
