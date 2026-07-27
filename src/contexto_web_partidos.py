"""Contexto web acumulado para todos los partidos de Liga MX.

Tavily, GNews y Serper se consultan de forma escalonada. Cada partido queda
identificado por jornada, local, visitante y fase. Las previas son temporales;
los postpartidos se conservan durante el torneo para describir la forma reciente
de los equipos sin volver a gastar créditos.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

import requests

from src.team_normalizer import canonical_team_key, clean_team_name, team_aliases

logger = logging.getLogger(__name__)

_TABLA = "contexto_web_jornadas"
_CACHE_VERSION = "v2"
_TTL_PREVIA_HORAS = 12
_TTL_POST_HORAS = 24 * 240
_TTL_SIN_RESULTADOS_HORAS = 6
_MIN_RESULTADOS = 4
_MAX_RESULTADOS = 6
_CONTEXTO_EXCLUIDO_TITULO = (
    "femenil",
    "women",
    "sub 23",
    "sub 20",
    "sub 18",
    "sub 17",
)
_PREVIA_ACCIONABLE = (
    "lesion",
    "lesionado",
    "sera baja",
    "causa baja",
    "baja confirmada",
    "ausencia confirmada",
    "no estara",
    "se pierde el partido",
    "suspension",
    "suspendido",
    "alineacion probable",
    "alineacion confirmada",
    "rotacion",
    "convocados",
    "convocatoria",
    "parte medico",
)
_PREVIA_PARTIDO = ("previa", " vs ", " contra ", "recibe a", "visita a", "enfrenta a", "partido", "jornada")
_POST_PARTIDO = (
    "resultado",
    "resumen",
    "cronica",
    "gana",
    "gano",
    "victoria",
    "vencio",
    "derroto",
    "empato",
    "empate",
    "final",
)
_uso_lock = threading.Lock()
_uso_fecha = ""
_uso_busquedas = 0


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _habilitado() -> bool:
    valor = os.getenv("WEB_CONTEXT_ENABLED", "1").strip().lower()
    claves = any(os.getenv(nombre, "").strip() for nombre in ("TAVILY_API_KEY", "GNEWS_API_KEY", "SERPER_API_KEY"))
    return valor not in {"0", "false", "off", "no"} and claves


def _reclamar_credito() -> bool:
    global _uso_busquedas, _uso_fecha
    hoy = _ahora().date().isoformat()
    limite = max(1, int(os.getenv("WEB_CONTEXT_MAX_DAILY", "30") or "30"))
    with _uso_lock:
        if _uso_fecha != hoy:
            _uso_fecha = hoy
            _uso_busquedas = 0
        if _uso_busquedas >= limite:
            return False
        _uso_busquedas += 1
        return True


def _jornada_valida(jornada: Optional[int]) -> int:
    return int(jornada) if jornada is not None else -1


def _clave(local: str, visitante: str, fase: str, jornada: Optional[int] = None) -> str:
    return (
        f"{_CACHE_VERSION}|j{_jornada_valida(jornada)}|"
        f"{canonical_team_key(local)}|{canonical_team_key(visitante)}|{fase}"
    )


def _asegurar_tabla() -> None:
    from src import database as db

    with db.get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {_TABLA} (
                clave TEXT PRIMARY KEY,
                jornada INTEGER NOT NULL,
                local TEXT NOT NULL,
                visitante TEXT NOT NULL,
                fase TEXT NOT NULL,
                fecha_partido TEXT,
                resumen TEXT,
                hallazgos TEXT,
                fuentes TEXT,
                proveedores TEXT,
                actualizado_en TEXT NOT NULL,
                expira_en TEXT NOT NULL
            )"""
        )
        conn.commit()


def _decodificar_json(valor: Any) -> List[Any]:
    try:
        data = json.loads(str(valor or "[]"))
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _leer_cache(
    local: str,
    visitante: str,
    fase: str,
    jornada: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    try:
        from src import database as db

        _asegurar_tabla()
        with db.get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT jornada, fecha_partido, resumen, hallazgos, fuentes,
                           proveedores, actualizado_en, expira_en
                    FROM {_TABLA} WHERE clave={db.PH}""",
                (_clave(local, visitante, fase, jornada),),
            )
            fila = cur.fetchone()
        if not fila:
            return None
        expira = datetime.fromisoformat(str(fila[7]).replace("Z", "+00:00"))
        if expira.tzinfo is None:
            expira = expira.replace(tzinfo=timezone.utc)
        if expira <= _ahora():
            return None
        return {
            "disponible": bool(fila[2]),
            "jornada": int(fila[0]),
            "fecha_partido": str(fila[1] or ""),
            "local": local,
            "visitante": visitante,
            "fase": fase,
            "resumen": str(fila[2] or ""),
            "hallazgos": _decodificar_json(fila[3]),
            "fuentes": _decodificar_json(fila[4]),
            "proveedores": _decodificar_json(fila[5]),
            "actualizado_en": str(fila[6]),
            "cache": True,
        }
    except Exception:
        logger.debug("No se pudo leer la caché de contexto por jornada", exc_info=True)
        return None


def _guardar_cache(
    local: str,
    visitante: str,
    fase: str,
    contexto: Mapping[str, Any],
    jornada: Optional[int] = None,
    fecha_partido: Optional[str] = None,
) -> None:
    try:
        from src import database as db

        _asegurar_tabla()
        ahora = _ahora()
        ttl = (
            (_TTL_POST_HORAS if fase == "post" else _TTL_PREVIA_HORAS)
            if contexto.get("resumen")
            else _TTL_SIN_RESULTADOS_HORAS
        )
        valores = (
            _clave(local, visitante, fase, jornada),
            _jornada_valida(jornada),
            local,
            visitante,
            fase,
            str(fecha_partido or "")[:10],
            str(contexto.get("resumen") or ""),
            json.dumps(contexto.get("hallazgos") or [], ensure_ascii=False),
            json.dumps(contexto.get("fuentes") or [], ensure_ascii=False),
            json.dumps(contexto.get("proveedores") or [], ensure_ascii=False),
            ahora.isoformat(),
            (ahora + timedelta(hours=ttl)).isoformat(),
        )
        with db.get_db() as conn:
            cur = conn.cursor()
            marcadores = ", ".join([db.PH] * 12)
            cur.execute(
                f"""INSERT INTO {_TABLA}
                    (clave, jornada, local, visitante, fase, fecha_partido,
                     resumen, hallazgos, fuentes, proveedores, actualizado_en, expira_en)
                    VALUES ({marcadores})
                    ON CONFLICT (clave) DO UPDATE SET
                    jornada=excluded.jornada, local=excluded.local,
                    visitante=excluded.visitante, fase=excluded.fase,
                    fecha_partido=excluded.fecha_partido, resumen=excluded.resumen,
                    hallazgos=excluded.hallazgos, fuentes=excluded.fuentes,
                    proveedores=excluded.proveedores,
                    actualizado_en=excluded.actualizado_en,
                    expira_en=excluded.expira_en""",
                valores,
            )
            conn.commit()
    except Exception:
        logger.debug("No se pudo guardar el contexto acumulado", exc_info=True)


def _historial_equipo(equipo: str, jornada_actual: int, limite: int = 2) -> List[Dict[str, Any]]:
    try:
        from src import database as db

        _asegurar_tabla()
        params = ("post", jornada_actual, equipo, equipo, max(1, limite))
        with db.get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT jornada, resumen FROM {_TABLA}
                    WHERE fase={db.PH} AND jornada < {db.PH}
                      AND (local={db.PH} OR visitante={db.PH})
                      AND resumen IS NOT NULL AND resumen <> ''
                    ORDER BY jornada DESC LIMIT {db.PH}""",
                params,
            )
            filas = cur.fetchall()
        return [{"jornada": int(fila[0]), "resumen": str(fila[1])} for fila in filas]
    except Exception:
        logger.debug("No se pudo leer la forma web reciente", exc_info=True)
        return []


def _menciona_equipo(texto_normalizado: str, equipo: str) -> bool:
    """Detecta al equipo por alias completos, evitando coincidencias parciales."""
    for alias in team_aliases(equipo):
        alias_limpio = clean_team_name(alias)
        if alias_limpio and re.search(rf"\b{re.escape(alias_limpio)}\b", texto_normalizado):
            return True
    return False


def _es_relevante(titulo: str, texto: str, local: str, visitante: str, fase: str = "previa") -> bool:
    """Acepta solo contexto del partido o alertas concretas de disponibilidad."""
    titulo_normalizado = clean_team_name(titulo)
    texto_crudo = f"{titulo} {texto}"
    texto_completo = f" {clean_team_name(texto_crudo)} "
    if not texto_completo.strip() or any(termino in titulo_normalizado for termino in _CONTEXTO_EXCLUIDO_TITULO):
        return False

    menciona_local = _menciona_equipo(texto_completo, local)
    menciona_visitante = _menciona_equipo(texto_completo, visitante)
    if menciona_local and menciona_visitante:
        marcadores = _POST_PARTIDO if fase == "post" else _PREVIA_PARTIDO + _PREVIA_ACCIONABLE
        tiene_marcador = bool(re.search(r"\b\d+\s*[-:]\s*\d+\b", texto_crudo))
        return tiene_marcador or any(termino in texto_completo for termino in marcadores)

    # Una nota de un solo equipo solo aporta valor antes del partido cuando
    # comunica una baja, suspensión, alineación o rotación concreta.
    if fase == "previa" and (menciona_local or menciona_visitante):
        return any(termino in texto_completo for termino in _PREVIA_ACCIONABLE)
    return False


def _item(
    titulo: Any,
    url: Any,
    texto: Any,
    fecha: Any,
    proveedor: str,
    local: str = "",
    visitante: str = "",
    fase: str = "previa",
) -> Optional[Dict[str, str]]:
    titulo_limpio = " ".join(str(titulo or "").split())[:220]
    url_limpia = str(url or "").strip()[:500]
    texto_limpio = " ".join(str(texto or "").split())[:700]
    if not titulo_limpio or not url_limpia.startswith(("http://", "https://")):
        return None
    if local and visitante and not _es_relevante(titulo_limpio, texto_limpio, local, visitante, fase):
        logger.debug("Resultado irrelevante descartado: %s", titulo_limpio[:80])
        return None
    return {
        "titulo": titulo_limpio,
        "url": url_limpia,
        "texto": texto_limpio,
        "fecha": str(fecha or "")[:40],
        "proveedor": proveedor,
    }


def _tavily(query: str, local: str = "", visitante: str = "", fase: str = "previa") -> List[Dict[str, str]]:
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key or not _reclamar_credito():
        return []
    respuesta = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": key,
            "query": query,
            "topic": "news",
            "search_depth": "basic",
            "max_results": _MAX_RESULTADOS,
            "days": 30,
            "include_answer": False,
            "include_raw_content": False,
        },
        timeout=20,
    )
    respuesta.raise_for_status()
    data = respuesta.json()
    salida: List[Dict[str, str]] = []
    for raw in data.get("results", []) if isinstance(data, dict) else []:
        if isinstance(raw, dict):
            normalizado = _item(
                raw.get("title"),
                raw.get("url"),
                raw.get("content"),
                raw.get("published_date"),
                "tavily",
                local,
                visitante,
                fase,
            )
            if normalizado:
                salida.append(normalizado)
    return salida


def _gnews(query: str, local: str = "", visitante: str = "", fase: str = "previa") -> List[Dict[str, str]]:
    key = os.getenv("GNEWS_API_KEY", "").strip()
    if not key or not _reclamar_credito():
        return []
    respuesta = requests.get(
        "https://gnews.io/api/v4/search",
        params={"q": query, "lang": "es", "country": "mx", "max": _MAX_RESULTADOS, "apikey": key},
        timeout=20,
    )
    respuesta.raise_for_status()
    data = respuesta.json()
    salida: List[Dict[str, str]] = []
    for raw in data.get("articles", []) if isinstance(data, dict) else []:
        if isinstance(raw, dict):
            normalizado = _item(
                raw.get("title"),
                raw.get("url"),
                raw.get("description") or raw.get("content"),
                raw.get("publishedAt"),
                "gnews",
                local,
                visitante,
                fase,
            )
            if normalizado:
                salida.append(normalizado)
    return salida


def _serper(query: str, local: str = "", visitante: str = "", fase: str = "previa") -> List[Dict[str, str]]:
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key or not _reclamar_credito():
        return []
    respuesta = requests.post(
        "https://google.serper.dev/news",
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        json={"q": query, "gl": "mx", "hl": "es-419", "num": _MAX_RESULTADOS},
        timeout=20,
    )
    respuesta.raise_for_status()
    data = respuesta.json()
    salida: List[Dict[str, str]] = []
    for raw in data.get("news", []) if isinstance(data, dict) else []:
        if isinstance(raw, dict):
            normalizado = _item(
                raw.get("title"),
                raw.get("link"),
                raw.get("snippet"),
                raw.get("date"),
                "serper",
                local,
                visitante,
                fase,
            )
            if normalizado:
                salida.append(normalizado)
    return salida


def _consulta(
    local: str,
    visitante: str,
    fase: str,
    jornada: Optional[int] = None,
    fecha_partido: Optional[str] = None,
) -> str:
    referencia = " ".join(
        parte for parte in (f"Jornada {jornada}" if jornada is not None else "", fecha_partido or "") if parte
    )
    if fase == "post":
        return f'"{local}" "{visitante}" {referencia} resultado resumen goles lesiones expulsiones Liga MX'
    return (
        f'"{local}" "{visitante}" {referencia} lesiones suspensiones bajas alineación rotaciones forma reciente Liga MX'
    )


def _buscar(
    local: str,
    visitante: str,
    fase: str,
    jornada: Optional[int] = None,
    fecha_partido: Optional[str] = None,
) -> List[Dict[str, str]]:
    query = _consulta(local, visitante, fase, jornada, fecha_partido)
    resultados: List[Dict[str, str]] = []
    for nombre, proveedor in (("tavily", _tavily), ("gnews", _gnews), ("serper", _serper)):
        if len(resultados) >= _MIN_RESULTADOS:
            break
        try:
            resultados.extend(proveedor(query, local, visitante, fase))
        except Exception as exc:
            logger.warning("Proveedor web %s no disponible (%s)", nombre, type(exc).__name__)
    unicos: List[Dict[str, str]] = []
    urls: set[str] = set()
    for resultado in resultados:
        url = resultado["url"].split("#", 1)[0]
        if url in urls:
            continue
        urls.add(url)
        unicos.append(resultado)
        if len(unicos) >= _MAX_RESULTADOS:
            break
    return unicos


def _resumir(resultados: Sequence[Mapping[str, str]]) -> Dict[str, Any]:
    hallazgos: List[str] = []
    fuentes: List[str] = []
    proveedores: List[str] = []
    for resultado in resultados[:3]:
        titulo = str(resultado.get("titulo") or "")
        if titulo:
            hallazgos.append(titulo)
        url = str(resultado.get("url") or "")
        if url:
            fuentes.append(url)
        proveedor = str(resultado.get("proveedor") or "")
        if proveedor and proveedor not in proveedores:
            proveedores.append(proveedor)
    resumen = "; ".join(hallazgos)[:500]
    return {
        "disponible": bool(resumen),
        "resumen": resumen,
        "hallazgos": hallazgos,
        "fuentes": fuentes,
        "proveedores": proveedores,
    }


def investigar_partido(
    local: str,
    visitante: str,
    fase: str = "previa",
    forzar: bool = False,
    jornada: Optional[int] = None,
    fecha_partido: Optional[str] = None,
) -> Dict[str, Any]:
    fase = "post" if fase == "post" else "previa"
    if not _habilitado():
        return {"disponible": False, "motivo": "sin proveedores configurados"}
    if not forzar:
        cache = _leer_cache(local, visitante, fase, jornada)
        if cache is not None:
            return cache
    resultados = _buscar(local, visitante, fase, jornada, fecha_partido)
    contexto = {
        "local": local,
        "visitante": visitante,
        "fase": fase,
        "jornada": _jornada_valida(jornada),
        "fecha_partido": str(fecha_partido or "")[:10],
        "actualizado_en": _ahora().isoformat(),
        "cache": False,
        **_resumir(resultados),
    }
    _guardar_cache(local, visitante, fase, contexto, jornada, fecha_partido)
    return contexto


def contextos_para_plan(plan: Mapping[str, Any], limite: int = 3) -> List[Dict[str, Any]]:
    salida: List[Dict[str, Any]] = []
    pasos = plan.get("plan")
    if not isinstance(pasos, list):
        return salida
    for paso in pasos[: max(0, limite)]:
        if not isinstance(paso, dict):
            continue
        equipo = str(paso.get("equipo") or "")
        rival = str(paso.get("rival") or "")
        jornada_valor = paso.get("jornada")
        if jornada_valor is None:
            continue
        try:
            jornada = int(jornada_valor)
        except (TypeError, ValueError):
            continue
        es_local = str(paso.get("condicion") or "").lower().startswith("local")
        local, visitante = (equipo, rival) if es_local else (rival, equipo)
        previa = _leer_cache(local, visitante, "previa", jornada)
        if previa and previa.get("disponible"):
            salida.append(
                {
                    "jornada": jornada,
                    "equipo": equipo,
                    "resumen": str(previa.get("resumen") or "")[:500],
                    "fuentes": previa.get("fuentes", []),
                }
            )
    return salida


def _fecha(valor: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(valor or "")[:10])
    except ValueError:
        return None


def _partidos_bloque(bloque: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    partidos = bloque.get("partidos")
    if not isinstance(partidos, list):
        return []
    return [partido for partido in partidos if isinstance(partido, dict)]


def actualizar_contexto_automatico(hoy: Optional[date] = None) -> Dict[str, Any]:
    if not _habilitado():
        return {"ejecutado": False, "motivo": "sin proveedores configurados"}
    from src.planificador_survivor import cargar_calendario

    hoy = hoy or _ahora().date()
    calendario = cargar_calendario() or []
    tareas: List[tuple[int, str, str, str, str]] = []

    for bloque in calendario:
        inicio = _fecha(bloque.get("fecha_inicio"))
        if inicio is None or not 0 <= (inicio - hoy).days <= 2:
            continue
        jornada_valor = bloque.get("jornada")
        if jornada_valor is None:
            continue
        jornada = int(jornada_valor)
        for partido in _partidos_bloque(bloque):
            tareas.append(
                (
                    jornada,
                    "previa",
                    str(partido.get("home_team") or ""),
                    str(partido.get("away_team") or ""),
                    inicio.isoformat(),
                )
            )

    completadas: List[Mapping[str, Any]] = []
    for bloque in calendario:
        fin = _fecha(bloque.get("fecha_fin"))
        if fin is not None and fin < hoy:
            completadas.append(bloque)
    completadas.sort(key=lambda item: int(item.get("jornada") or 0))
    for bloque_completado in completadas:
        jornada_valor = bloque_completado.get("jornada")
        if jornada_valor is None:
            continue
        jornada = int(jornada_valor)
        fin = _fecha(bloque_completado.get("fecha_fin"))
        for partido in _partidos_bloque(bloque_completado):
            tareas.append(
                (
                    jornada,
                    "post",
                    str(partido.get("home_team") or ""),
                    str(partido.get("away_team") or ""),
                    fin.isoformat() if fin else "",
                )
            )

    limite = max(1, min(18, int(os.getenv("WEB_CONTEXT_MATCH_LIMIT", "9") or "9")))
    consultas = 0
    disponibles = 0
    jornadas: set[int] = set()
    revisados = 0
    for jornada, fase, local, visitante, fecha_partido in tareas:
        if not local or not visitante:
            continue
        contexto = investigar_partido(
            local,
            visitante,
            fase=fase,
            jornada=jornada,
            fecha_partido=fecha_partido,
        )
        revisados += 1
        jornadas.add(jornada)
        if not contexto.get("cache", False):
            consultas += 1
        disponibles += int(bool(contexto.get("disponible")))
        if consultas >= limite:
            break
    return {
        "ejecutado": True,
        "jornadas": sorted(jornadas),
        "partidos_revisados": revisados,
        "consultas": consultas,
        "disponibles": disponibles,
        "backfill_completo": consultas == 0,
    }
