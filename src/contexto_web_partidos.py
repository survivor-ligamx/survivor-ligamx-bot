"""Contexto web reciente para partidos de Liga MX.

Consulta Tavily, GNews y Serper de forma escalonada, con caché persistente y
presupuesto diario. Esta capa nunca es requisito para generar un pick o plan y
por ahora solo aporta contexto informativo; no modifica probabilidades.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

import requests

from src.team_normalizer import canonical_team_key

logger = logging.getLogger(__name__)

_TTL_PREVIA_HORAS = 12
_TTL_POST_HORAS = 36
_MIN_RESULTADOS = 4
_MAX_RESULTADOS = 6
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
    """Límite defensivo en memoria; la caché persistente evita repeticiones tras reinicios."""
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


def _clave(local: str, visitante: str, fase: str) -> str:
    return f"{canonical_team_key(local)}|{canonical_team_key(visitante)}|{fase}"


def _asegurar_tabla() -> None:
    from src import database as db

    with db.get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS contexto_web_partidos (
                clave TEXT PRIMARY KEY,
                local TEXT NOT NULL,
                visitante TEXT NOT NULL,
                fase TEXT NOT NULL,
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


def _leer_cache(local: str, visitante: str, fase: str) -> Optional[Dict[str, Any]]:
    try:
        from src import database as db

        _asegurar_tabla()
        with db.get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT resumen, hallazgos, fuentes, proveedores, actualizado_en, expira_en
                    FROM contexto_web_partidos WHERE clave={db.PH}""",
                (_clave(local, visitante, fase),),
            )
            fila = cur.fetchone()
        if not fila:
            return None
        expira = datetime.fromisoformat(str(fila[5]).replace("Z", "+00:00"))
        if expira.tzinfo is None:
            expira = expira.replace(tzinfo=timezone.utc)
        if expira <= _ahora():
            return None
        return {
            "disponible": bool(fila[0]),
            "local": local,
            "visitante": visitante,
            "fase": fase,
            "resumen": str(fila[0] or ""),
            "hallazgos": _decodificar_json(fila[1]),
            "fuentes": _decodificar_json(fila[2]),
            "proveedores": _decodificar_json(fila[3]),
            "actualizado_en": str(fila[4]),
            "cache": True,
        }
    except Exception:
        logger.debug("No se pudo leer la caché de contexto web", exc_info=True)
        return None


def _guardar_cache(local: str, visitante: str, fase: str, contexto: Mapping[str, Any]) -> None:
    try:
        from src import database as db

        _asegurar_tabla()
        ahora = _ahora()
        ttl = _TTL_POST_HORAS if fase == "post" else _TTL_PREVIA_HORAS
        valores = (
            _clave(local, visitante, fase),
            local,
            visitante,
            fase,
            str(contexto.get("resumen") or ""),
            json.dumps(contexto.get("hallazgos") or [], ensure_ascii=False),
            json.dumps(contexto.get("fuentes") or [], ensure_ascii=False),
            json.dumps(contexto.get("proveedores") or [], ensure_ascii=False),
            ahora.isoformat(),
            (ahora + timedelta(hours=ttl)).isoformat(),
        )
        with db.get_db() as conn:
            cur = conn.cursor()
            marcadores = ", ".join([db.PH] * 10)
            cur.execute(
                f"""INSERT INTO contexto_web_partidos
                    (clave, local, visitante, fase, resumen, hallazgos, fuentes,
                     proveedores, actualizado_en, expira_en)
                    VALUES ({marcadores})
                    ON CONFLICT (clave) DO UPDATE SET
                    local=excluded.local, visitante=excluded.visitante, fase=excluded.fase,
                    resumen=excluded.resumen, hallazgos=excluded.hallazgos,
                    fuentes=excluded.fuentes, proveedores=excluded.proveedores,
                    actualizado_en=excluded.actualizado_en, expira_en=excluded.expira_en""",
                valores,
            )
            conn.commit()
    except Exception:
        logger.debug("No se pudo guardar la caché de contexto web", exc_info=True)


def _item(titulo: Any, url: Any, texto: Any, fecha: Any, proveedor: str) -> Optional[Dict[str, str]]:
    titulo_limpio = " ".join(str(titulo or "").split())[:220]
    url_limpia = str(url or "").strip()[:500]
    if not titulo_limpio or not url_limpia.startswith(("http://", "https://")):
        return None
    return {
        "titulo": titulo_limpio,
        "url": url_limpia,
        "texto": " ".join(str(texto or "").split())[:700],
        "fecha": str(fecha or "")[:40],
        "proveedor": proveedor,
    }


def _tavily(query: str) -> List[Dict[str, str]]:
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
            "days": 7,
            "include_answer": False,
            "include_raw_content": False,
        },
        timeout=20,
    )
    respuesta.raise_for_status()
    data = respuesta.json()
    salida: List[Dict[str, str]] = []
    for raw in data.get("results", []) if isinstance(data, dict) else []:
        if not isinstance(raw, dict):
            continue
        normalizado = _item(
            raw.get("title"),
            raw.get("url"),
            raw.get("content"),
            raw.get("published_date"),
            "tavily",
        )
        if normalizado:
            salida.append(normalizado)
    return salida


def _gnews(query: str) -> List[Dict[str, str]]:
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
        if not isinstance(raw, dict):
            continue
        normalizado = _item(
            raw.get("title"),
            raw.get("url"),
            raw.get("description") or raw.get("content"),
            raw.get("publishedAt"),
            "gnews",
        )
        if normalizado:
            salida.append(normalizado)
    return salida


def _serper(query: str) -> List[Dict[str, str]]:
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
        if not isinstance(raw, dict):
            continue
        normalizado = _item(
            raw.get("title"),
            raw.get("link"),
            raw.get("snippet"),
            raw.get("date"),
            "serper",
        )
        if normalizado:
            salida.append(normalizado)
    return salida


def _consulta(local: str, visitante: str, fase: str) -> str:
    if fase == "post":
        return f'"{local}" "{visitante}" resultado resumen lesiones expulsiones Liga MX últimas 48 horas'
    return f'"{local}" "{visitante}" lesiones suspensiones bajas alineación rotaciones Liga MX últimas 72 horas'


def _buscar(local: str, visitante: str, fase: str) -> List[Dict[str, str]]:
    query = _consulta(local, visitante, fase)
    resultados: List[Dict[str, str]] = []
    for nombre, proveedor in (("tavily", _tavily), ("gnews", _gnews), ("serper", _serper)):
        if len(resultados) >= _MIN_RESULTADOS:
            break
        try:
            resultados.extend(proveedor(query))
        except Exception as exc:
            logger.warning("Proveedor web %s no disponible (%s)", nombre, type(exc).__name__)
    unicos: List[Dict[str, str]] = []
    urls = set()
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
) -> Dict[str, Any]:
    """Investiga un partido con fallback entre proveedores y guarda el resultado."""
    fase = "post" if fase == "post" else "previa"
    if not _habilitado():
        return {"disponible": False, "motivo": "sin proveedores configurados"}
    if not forzar:
        cache = _leer_cache(local, visitante, fase)
        if cache is not None:
            return cache
    resultados = _buscar(local, visitante, fase)
    contexto = {
        "local": local,
        "visitante": visitante,
        "fase": fase,
        "actualizado_en": _ahora().isoformat(),
        "cache": False,
        **_resumir(resultados),
    }
    _guardar_cache(local, visitante, fase, contexto)
    return contexto


def contextos_para_plan(plan: Mapping[str, Any], limite: int = 3) -> List[Dict[str, Any]]:
    """Carga contexto ya cacheado para los primeros picks; nunca hace búsquedas aquí."""
    salida: List[Dict[str, Any]] = []
    pasos = plan.get("plan")
    if not isinstance(pasos, list):
        return salida
    for paso in pasos[: max(0, limite)]:
        if not isinstance(paso, dict):
            continue
        equipo = str(paso.get("equipo") or "")
        rival = str(paso.get("rival") or "")
        es_local = str(paso.get("condicion") or "").lower().startswith("local")
        local, visitante = (equipo, rival) if es_local else (rival, equipo)
        cache = _leer_cache(local, visitante, "previa")
        if cache and cache.get("disponible"):
            cache["jornada"] = paso.get("jornada")
            cache["equipo"] = equipo
            salida.append(cache)
    return salida


def _fecha(valor: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(valor or "")[:10])
    except ValueError:
        return None


def actualizar_contexto_automatico(hoy: Optional[date] = None) -> Dict[str, Any]:
    """Investiga la jornada próxima y la recién terminada sin repetir consultas cacheadas."""
    if not _habilitado():
        return {"ejecutado": False, "motivo": "sin proveedores configurados"}
    from src.planificador_survivor import cargar_calendario

    hoy = hoy or _ahora().date()
    candidatos: List[tuple[int, str, Mapping[str, Any]]] = []
    for bloque in cargar_calendario() or []:
        inicio = _fecha(bloque.get("fecha_inicio"))
        fin = _fecha(bloque.get("fecha_fin"))
        if inicio is None or fin is None:
            continue
        if 0 <= (inicio - hoy).days <= 2:
            candidatos.append(((inicio - hoy).days, "previa", bloque))
        if 0 <= (hoy - fin).days <= 1:
            candidatos.append(((hoy - fin).days, "post", bloque))
    if not candidatos:
        return {"ejecutado": True, "partidos": 0, "consultas": 0}
    candidatos.sort(key=lambda item: (item[0], item[1]))
    _, fase, bloque_seleccionado = candidatos[0]
    limite = max(1, min(9, int(os.getenv("WEB_CONTEXT_MATCH_LIMIT", "9") or "9")))
    consultas = 0
    disponibles = 0
    for partido in (bloque_seleccionado.get("partidos") or [])[:limite]:
        if not isinstance(partido, dict):
            continue
        local = str(partido.get("home_team") or "")
        visitante = str(partido.get("away_team") or "")
        if not local or not visitante:
            continue
        contexto = investigar_partido(local, visitante, fase=fase)
        consultas += int(not contexto.get("cache", False))
        disponibles += int(bool(contexto.get("disponible")))
    return {
        "ejecutado": True,
        "fase": fase,
        "jornada": bloque_seleccionado.get("jornada"),
        "partidos": min(limite, len(bloque_seleccionado.get("partidos") or [])),
        "consultas": consultas,
        "disponibles": disponibles,
    }
