#!/usr/bin/env python3
"""
telegram_webhook.py — Comandos del bot por Telegram (webhook).

Permite operar el Survivor desde el celular respondiéndole al bot, sin llamar a
la API a mano:

  /usado <equipo>   -> marca un equipo como YA usado (se excluye del pick/plan)
  /usados           -> lista los equipos usados
  /quitar <equipo>  -> quita un equipo de la lista de usados
  /reset            -> vacía la lista (nueva temporada)
  /pick             -> genera y envía el pronóstico + pick recomendado ahora
  /ayuda            -> muestra esta ayuda

Seguridad: el endpoint solo atiende mensajes del TELEGRAM_CHAT_ID configurado
(el dueño) y, si se define TELEGRAM_WEBHOOK_SECRET, valida el header secreto de
Telegram. Este módulo NO decide picks ni apuesta: solo registra estado y dispara
el envío informativo. INFORMATIVO / REVISIÓN HUMANA.
"""

from __future__ import annotations

from html import escape
from typing import Any, Dict, Optional, Tuple

AYUDA = (
    "🤖 <b>Comandos Survivor</b>\n"
    "/pick (o /picks) — pronóstico + pick recomendado de la jornada\n"
    "/plan — ANÁLISIS INTELIGENTE + plan de temporada (revisa TODO lo que pasa y te dice qué hacer)\n"
    "/mipick — tu estado actual, racha, usados e historial\n"
    "/confirmar &lt;jornada&gt; &lt;equipo&gt; — confirma tu selección real\n"
    "/bloquear &lt;jornada&gt; — protege el pick confirmado contra cambios\n"
    "/resolver &lt;jornada&gt; &lt;gano|empate|perdio&gt; — registra el resultado\n"
    "/momios — baja los momios (1X2/OU/hándicap) y muestra la cobertura\n"
    "/seguir — lista de seguimiento: candidatos por hora para decidir secuencial\n"
    "/analisis — análisis post-partido: goles, tarjetas, alineaciones y conclusión IA\n"
    "/prueba — pone a prueba la estrategia con torneos pasados (¿sobrevive?)\n"
    "/confianza — revisa si la confianza del bot es honesta o exagerada\n"
    "/derrotas — aprende de las derrotas pasadas (en qué partido cayó y por qué)\n"
    "/ganadores — el 'Survivor perfecto' (con diario del futuro) vs el bot\n"
    "/racha — tu track-record: jornadas sobrevividas, victorias y si sigues vivo\n"
    "\n"
    "/usado &lt;equipo&gt; — marca un equipo como usado (lo excluye)\n"
    "/usados — lista tus equipos usados\n"
    "/quitar &lt;equipo&gt; — quita un equipo de la lista\n"
    "/reset — limpia usados manuales; conserva picks confirmados\n"
    "/ayuda — esta ayuda\n\n"
    "ℹ️ Informativo / revisión humana. No es consejo de apuesta."
)

# Comandos que disparan la generación/envío del pronóstico (pesado -> background).
CMDS_PICK = {"pick", "picks", "survivor", "jornada", "pronostico", "pronosticos"}

# Comandos del plan de temporada (pesado -> background).
CMDS_PLAN = {"plan", "temporada", "calendario"}

# Comandos para actualizar/ver momios (pesado -> background).
CMDS_MOMIOS = {"momios", "cuotas", "odds", "mercado"}

# Comandos de la lista de seguimiento secuencial (pesado -> background).
CMDS_SEGUIMIENTO = {"seguir", "seguimiento", "candidatos", "watchlist"}

# "Prueba" de la estrategia = backtest sobre temporadas pasadas (pesado -> background).
CMDS_PRUEBA = {"prueba", "probar", "backtest", "historial", "simular"}

# "Confianza" del bot = calibración del modelo (pesado -> background).
CMDS_CONFIANZA = {"confianza", "honestidad", "calibracion", "calibrar", "revisar"}

# "Derrotas" = postmortem del backtest: en qué partido cayó y por qué (pesado).
CMDS_DERROTAS = {"derrotas", "aprender", "errores", "postmortem"}

# "Ganadores" = Survivor perfecto (oráculo) vs el bot (pesado -> background).
CMDS_GANADORES = {"ganadores", "perfecto", "oraculo", "ideal"}

# "Racha" = track-record REAL del pick de Survivor del bot (ligero -> lee la BD).
CMDS_RACHA = {"racha", "rachas", "vivo", "trackrecord", "mirracha"}

# "Analisis" = análisis post-partido de la jornada (pesado -> background).
CMDS_ANALISIS = {"analisis", "analizar", "resultados", "post", "postpartido"}


def parsear_comando(texto: str) -> Tuple[Optional[str], str]:
    """
    Extrae (comando, argumento) de un texto de Telegram. Devuelve (None, "") si
    no es un comando (no empieza con '/'). Soporta sufijo @bot y colapsa espacios.
    """
    t = (texto or "").strip()
    if not t.startswith("/"):
        return (None, "")
    partes = t.split(maxsplit=1)
    cmd = partes[0].lstrip("/").split("@")[0].lower()
    arg = partes[1].strip() if len(partes) > 1 else ""
    return (cmd, arg)


def _db():
    from src import database as db

    return db


def _jornada_y_valor(arg: str, uso: str) -> Tuple[Optional[int], str, Optional[str]]:
    """Separa ``<jornada> <valor>`` y devuelve un mensaje de uso si es inválido."""
    partes = str(arg or "").split(maxsplit=1)
    if len(partes) != 2:
        return None, "", uso
    try:
        jornada = int(partes[0])
    except ValueError:
        return None, "", uso
    if not 1 <= jornada <= 17 or not partes[1].strip():
        return None, "", uso
    return jornada, partes[1].strip(), None


def _formatear_mi_survivor(resumen: Dict[str, Any]) -> str:
    """Resumen Telegram con la vida única de empate y múltiples pendientes."""
    from src.survivor_reglas import evaluar_temporada

    temporada = escape(str(resumen.get("temporada") or ""))
    estado_oficial = evaluar_temporada(resumen.get("picks") or [])
    vivo = bool(estado_oficial.get("sigue_vivo", resumen.get("sigue_vivo", True)))
    estado = "🟢 VIVO" if vivo else "🔴 ELIMINADO"
    usados = [escape(str(equipo)) for equipo in resumen.get("usados", [])]
    victorias = int(estado_oficial.get("victorias", resumen.get("victorias", 0)) or 0)
    empates = int(estado_oficial.get("empates", resumen.get("empates", 0)) or 0)
    racha = int(estado_oficial.get("racha", resumen.get("racha", 0)) or 0)
    vida_consumida = bool(estado_oficial.get("vida_empate_consumida", False))
    vida = "⚠️ CONSUMIDA — otro empate elimina" if vida_consumida else "✅ DISPONIBLE"
    lineas = [
        f"🏆 <b>MI SURVIVOR · {temporada}</b>",
        f"Estado: <b>{estado}</b>",
        f"🔥 Racha: <b>{racha}</b>",
        f"✅ Victorias: <b>{victorias}</b>",
        f"🤝 Empates: <b>{empates}</b>",
        f"🫀 Vida de empate: <b>{vida}</b>",
        f"🔒 Usados ({len(usados)}): {', '.join(usados) or '—'}",
    ]
    eliminado_en = estado_oficial.get("eliminado_en")
    if eliminado_en is not None:
        lineas.append(f"💀 Eliminado en: <b>J{int(eliminado_en)}</b>")
        aviso = estado_oficial.get("aviso_finalista")
        if aviso:
            lineas.append(f"ℹ️ {escape(str(aviso))}")

    pendientes = estado_oficial.get("picks_pendientes") or []
    if pendientes:
        lineas.append("")
        lineas.append(f"⏳ <b>Picks pendientes ({len(pendientes)}):</b>")
        for pick in pendientes:
            lineas.append(
                f"• J{pick.get('jornada')}: <b>{escape(str(pick.get('equipo') or ''))}</b> "
                f"({escape(str(pick.get('estado') or ''))})"
            )
    else:
        actual = resumen.get("pick_actual")
        if actual:
            lineas.append(
                f"🎯 J{actual.get('jornada')}: <b>{escape(str(actual.get('equipo') or ''))}</b> "
                f"({escape(str(actual.get('estado') or ''))})"
            )

    lineas.append("\nℹ️ Informativo / revisión humana. No es consejo de apuesta.")
    return "\n".join(lineas)


def responder(cmd: Optional[str], arg: str) -> str:
    """
    Ejecuta un comando (que NO sea de pick) y devuelve el texto de respuesta.
    Las acciones de estado (usados) tocan la BD. Tolerante a fallos.
    """
    if cmd in (None, "start", "ayuda", "help"):
        return AYUDA

    db = _db()

    if cmd in ("mipick", "mio", "miestado") or cmd in CMDS_RACHA:
        try:
            return _formatear_mi_survivor(db.resumen_mi_survivor())
        except Exception as exc:  # pragma: no cover - BD no disponible
            return f"⚠️ No se pudo leer Mi Survivor: {escape(str(exc))}"

    if cmd in ("confirmar", "elegir", "seleccionar"):
        jornada, equipo, error = _jornada_y_valor(
            arg,
            "Uso: <code>/confirmar &lt;jornada&gt; &lt;equipo&gt;</code> (ej. /confirmar 3 América)",
        )
        if error or jornada is None:
            return str(error)
        try:
            pick = db.confirmar_survivor_pick(db.temporada_survivor_actual(), jornada, equipo)
        except Exception as exc:
            return f"⚠️ No se pudo confirmar: {escape(str(exc))}"
        return (
            f"✅ Pick confirmado para J{jornada}: <b>{escape(str(pick['equipo']))}</b>\n"
            "Ya quedó excluido de las próximas jornadas."
        )

    if cmd in ("bloquear", "lock"):
        try:
            jornada = int(str(arg).strip())
        except (TypeError, ValueError):
            return "Uso: <code>/bloquear &lt;jornada&gt;</code> (ej. /bloquear 3)"
        try:
            pick = db.bloquear_survivor_pick(db.temporada_survivor_actual(), jornada)
        except Exception as exc:
            return f"⚠️ No se pudo bloquear: {escape(str(exc))}"
        return f"🔐 Pick bloqueado J{jornada}: <b>{escape(str(pick['equipo']))}</b>"

    if cmd in ("resolver", "resultado"):
        jornada, resultado, error = _jornada_y_valor(
            arg,
            "Uso: <code>/resolver &lt;jornada&gt; &lt;gano|empate|perdio&gt;</code>",
        )
        if error or jornada is None:
            return str(error)
        try:
            pick = db.resolver_survivor_pick(db.temporada_survivor_actual(), jornada, resultado)
        except Exception as exc:
            return f"⚠️ No se pudo resolver: {escape(str(exc))}"
        return (
            f"🏁 J{jornada} resuelta: <b>{escape(str(pick['equipo']))}</b> — "
            f"<b>{escape(str(pick['resultado']).upper())}</b>"
        )

    if cmd in ("usado", "uso", "use"):
        if not arg:
            return "Uso: <code>/usado &lt;equipo&gt;</code> (ej. /usado América)"
        try:
            agregado = db.add_equipo_usado(arg)
            usados = db.get_equipos_usados()
        except Exception as exc:  # pragma: no cover - BD no disponible
            return f"⚠️ No se pudo registrar: {exc}"
        cab = "✅ Registrado" if agregado else "ℹ️ Ya estaba"
        return f"{cab}: <b>{arg}</b>\nUsados ({len(usados)}): {', '.join(usados) or '—'}"

    if cmd in ("usados", "lista", "list"):
        try:
            usados = db.get_equipos_usados()
        except Exception as exc:  # pragma: no cover
            return f"⚠️ No se pudo leer: {exc}"
        return f"🔒 Equipos usados ({len(usados)}): {', '.join(usados) or '—'}"

    if cmd in ("quitar", "borrar", "remove"):
        if not arg:
            return "Uso: <code>/quitar &lt;equipo&gt;</code>"
        try:
            filas = db.remove_equipo_usado(arg)
            usados = db.get_equipos_usados()
        except Exception as exc:  # pragma: no cover
            return f"⚠️ No se pudo quitar: {exc}"
        cab = "✅ Quitado" if filas else "ℹ️ No estaba en la lista"
        return f"{cab}: <b>{arg}</b>\nUsados ({len(usados)}): {', '.join(usados) or '—'}"

    if cmd in ("reset", "reiniciar"):
        try:
            borrados = db.clear_equipos_usados()
        except Exception as exc:  # pragma: no cover
            return f"⚠️ No se pudo reiniciar: {exc}"
        return f"♻️ Lista manual reiniciada ({borrados}). Los picks confirmados se conservan."

    return "❓ Comando no reconocido. Usa /ayuda"


def extraer_mensaje(update: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """
    De un update de Telegram saca (chat_id, texto). Soporta 'message' y
    'edited_message'. Devuelve (None, "") si no hay mensaje de texto.
    """
    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    texto = msg.get("text", "") or ""
    try:
        chat_id = int(chat_id) if chat_id is not None else None
    except (TypeError, ValueError):
        chat_id = None
    return (chat_id, texto)
