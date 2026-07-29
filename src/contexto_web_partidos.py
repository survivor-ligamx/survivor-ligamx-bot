"""Contexto web de partidos con fuentes curadas y señales conservadoras."""

from __future__ import annotations

# Este módulo conserva la implementación estable y concentra las mejoras de
# disponibilidad en funciones pequeñas para no crear un sistema paralelo.
import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlparse

import requests

from src.fuentes_equipos import dominios_equipo, nivel_url
from src.team_normalizer import canonical_team_key, clean_team_name, team_aliases

logger = logging.getLogger(__name__)
_TABLA = "contexto_web_jornadas"
_CACHE_VERSION = "v2"
_TTL_PREVIA_HORAS = 12
_TTL_POST_HORAS = 24 * 240
_TTL_SIN_RESULTADOS_HORAS = 6
_MIN_RESULTADOS = 4
_MAX_RESULTADOS = 6
_CONTEXTO_EXCLUIDO_TITULO = ("femenil", "women", "sub 23", "sub 20", "sub 18", "sub 17")
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
    "regresa",
    "disponible",
    "podria debutar",
    "puede debutar",
    "visa de trabajo",
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
    claves = any(os.getenv(n, "").strip() for n in ("TAVILY_API_KEY", "GNEWS_API_KEY", "SERPER_API_KEY"))
    return valor not in {"0", "false", "off", "no"} and claves


def _dias_previa() -> int:
    try:
        valor = int(os.getenv("WEB_CONTEXT_PREMATCH_DAYS", "5") or "5")
    except ValueError:
        valor = 5
    return max(1, min(10, valor))


def _reclamar_credito() -> bool:
    global _uso_busquedas, _uso_fecha
    hoy = _ahora().date().isoformat()
    try:
        limite = max(1, int(os.getenv("WEB_CONTEXT_MAX_DAILY", "30") or "30"))
    except ValueError:
        limite = 30
    with _uso_lock:
        if _uso_fecha != hoy:
            _uso_fecha, _uso_busquedas = hoy, 0
        if _uso_busquedas >= limite:
            return False
        _uso_busquedas += 1
        return True


def _jornada_valida(jornada: Optional[int]) -> int:
    return int(jornada) if jornada is not None else -1


def _clave(local: str, visitante: str, fase: str, jornada: Optional[int] = None) -> str:
    return f"{_CACHE_VERSION}|j{_jornada_valida(jornada)}|{canonical_team_key(local)}|{canonical_team_key(visitante)}|{fase}"


def _asegurar_tabla() -> None:
    from src import database as db

    with db.get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {_TABLA} (clave TEXT PRIMARY KEY, jornada INTEGER NOT NULL, local TEXT NOT NULL, visitante TEXT NOT NULL, fase TEXT NOT NULL, fecha_partido TEXT, resumen TEXT, hallazgos TEXT, fuentes TEXT, proveedores TEXT, actualizado_en TEXT NOT NULL, expira_en TEXT NOT NULL)"""
        )
        conn.commit()


def _decodificar_json(valor: Any) -> List[Any]:
    try:
        data = json.loads(str(valor or "[]"))
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _edad_horas(fecha: Any) -> Optional[float]:
    try:
        instante = datetime.fromisoformat(str(fecha).replace("Z", "+00:00"))
        if instante.tzinfo is None:
            instante = instante.replace(tzinfo=timezone.utc)
        return max(0.0, (_ahora() - instante).total_seconds() / 3600)
    except (TypeError, ValueError):
        return None


def _frescura(actualizado_en: Any) -> str:
    edad = _edad_horas(actualizado_en)
    if edad is None:
        return "DESCONOCIDA"
    if edad <= _TTL_PREVIA_HORAS:
        return "FRESCA"
    return "REVISAR" if edad <= 24 else "CADUCADA"


def _menciona_equipo(texto_normalizado: str, equipo: str) -> bool:
    for alias in team_aliases(equipo):
        alias_limpio = clean_team_name(alias)
        if alias_limpio and re.search(rf"\b{re.escape(alias_limpio)}\b", texto_normalizado):
            return True
    return False


def _equipo_evento(texto: str, local: str, visitante: str) -> str:
    normalizado = f" {clean_team_name(texto)} "
    if _menciona_equipo(normalizado, local):
        return local
    if _menciona_equipo(normalizado, visitante):
        return visitante
    return ""


def clasificar_evento(resultado: Mapping[str, Any], local: str, visitante: str) -> Dict[str, Any]:
    titulo = str(resultado.get("titulo") or "")
    texto = clean_team_name(f"{titulo} {resultado.get('texto') or ''}")
    tipo = "OTRO"
    if "alineacion confirmada" in texto or "xi confirmado" in texto:
        tipo = "ALINEACION_CONFIRMADA"
    elif "alineacion probable" in texto or "posible alineacion" in texto:
        tipo = "ALINEACION_PROBABLE"
    elif "suspendido" in texto or "suspension" in texto:
        tipo = "SUSPENSION"
    elif any(x in texto for x in ("baja confirmada", "ausencia confirmada", "no estara", "se pierde el partido")):
        tipo = "BAJA_CONFIRMADA"
    elif "lesion" in texto or "lesionado" in texto:
        tipo = "LESION"
    elif "rotacion" in texto:
        tipo = "ROTACION"
    elif "convoc" in texto:
        tipo = "CONVOCATORIA"
    elif any(x in texto for x in ("podria debutar", "puede debutar", "debut posible")):
        tipo = "DEBUT_POSIBLE"
    elif any(x in texto for x in ("visa de trabajo", "regresa", "disponible", "alta medica")):
        tipo = "DISPONIBLE"
    elif "duda" in texto or "podria perderse" in texto:
        tipo = "DUDA"
    url = str(resultado.get("url") or "")
    equipo = _equipo_evento(texto, local, visitante)
    nivel = nivel_url(url, equipo) if equipo else "desconocida"
    confirmacion = "NO_CONFIRMADA"
    if tipo == "ALINEACION_CONFIRMADA" or (
        nivel == "oficial" and tipo in {"BAJA_CONFIRMADA", "SUSPENSION", "CONVOCATORIA", "DISPONIBLE"}
    ):
        confirmacion = "CONFIRMADA"
    return {
        "equipo": equipo,
        "tipo": tipo,
        "confirmacion": confirmacion,
        "titularidad": "CONFIRMADA" if tipo == "ALINEACION_CONFIRMADA" else "NO_CONFIRMADA",
        "fecha": str(resultado.get("fecha") or "")[:40],
        "url": url,
        "proveedor": str(resultado.get("proveedor") or ""),
        "dominio": urlparse(url).netloc.lower().removeprefix("www."),
        "nivel_fuente": nivel,
        "titulo": titulo,
    }


def _estado_eventos(eventos: Sequence[Mapping[str, Any]]) -> str:
    utiles = [e for e in eventos if e.get("tipo") != "OTRO"]
    if any(e.get("tipo") == "ALINEACION_CONFIRMADA" and e.get("confirmacion") == "CONFIRMADA" for e in utiles):
        return "CONFIRMADO_CON_XI"
    if any(e.get("confirmacion") == "CONFIRMADA" for e in utiles):
        return "CONFIRMADO"
    grupos: Dict[tuple[str, str], set[str]] = {}
    for evento in utiles:
        grupos.setdefault((str(evento.get("equipo")), str(evento.get("tipo"))), set()).add(str(evento.get("dominio")))
    if any(len({d for d in ds if d}) >= 2 for ds in grupos.values()):
        return "PROBABLE"
    return "REVISAR" if utiles else "PROVISIONAL"


def _leer_cache(local: str, visitante: str, fase: str, jornada: Optional[int] = None) -> Optional[Dict[str, Any]]:
    try:
        from src import database as db

        _asegurar_tabla()
        with db.get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT jornada, fecha_partido, resumen, hallazgos, fuentes, proveedores, actualizado_en, expira_en FROM {_TABLA} WHERE clave={db.PH}",
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
        hallazgos, fuentes = _decodificar_json(fila[3]), _decodificar_json(fila[4])
        resultados = [
            {"titulo": t, "url": fuentes[i] if i < len(fuentes) else "", "proveedor": "cache"}
            for i, t in enumerate(hallazgos)
        ]
        eventos = [clasificar_evento(r, local, visitante) for r in resultados]
        return {
            "disponible": bool(fila[2]),
            "jornada": int(fila[0]),
            "fecha_partido": str(fila[1] or ""),
            "local": local,
            "visitante": visitante,
            "fase": fase,
            "resumen": str(fila[2] or ""),
            "hallazgos": hallazgos,
            "fuentes": fuentes,
            "proveedores": _decodificar_json(fila[5]),
            "eventos": eventos,
            "estado": _estado_eventos(eventos),
            "actualizado_en": str(fila[6]),
            "frescura": _frescura(fila[6]),
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
            cur.execute(
                f"""INSERT INTO {_TABLA} (clave,jornada,local,visitante,fase,fecha_partido,resumen,hallazgos,fuentes,proveedores,actualizado_en,expira_en) VALUES ({", ".join([db.PH] * 12)}) ON CONFLICT (clave) DO UPDATE SET jornada=excluded.jornada,local=excluded.local,visitante=excluded.visitante,fase=excluded.fase,fecha_partido=excluded.fecha_partido,resumen=excluded.resumen,hallazgos=excluded.hallazgos,fuentes=excluded.fuentes,proveedores=excluded.proveedores,actualizado_en=excluded.actualizado_en,expira_en=excluded.expira_en""",
                valores,
            )
            conn.commit()
    except Exception:
        logger.debug("No se pudo guardar el contexto acumulado", exc_info=True)


def _historial_equipo(equipo: str, jornada_actual: int, limite: int = 2) -> List[Dict[str, Any]]:
    try:
        from src import database as db

        _asegurar_tabla()
        with db.get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT jornada,resumen FROM {_TABLA} WHERE fase={db.PH} AND jornada < {db.PH} AND (local={db.PH} OR visitante={db.PH}) AND resumen IS NOT NULL AND resumen <> '' ORDER BY jornada DESC LIMIT {db.PH}",
                ("post", jornada_actual, equipo, equipo, max(1, limite)),
            )
            filas = cur.fetchall()
        return [{"jornada": int(f[0]), "resumen": str(f[1])} for f in filas]
    except Exception:
        return []


def _es_relevante(titulo: str, texto: str, local: str, visitante: str, fase: str = "previa") -> bool:
    titulo_n = clean_team_name(titulo)
    crudo, completo = f"{titulo} {texto}", f" {clean_team_name(f'{titulo} {texto}')} "
    if not completo.strip() or any(t in titulo_n for t in _CONTEXTO_EXCLUIDO_TITULO):
        return False
    ml, mv = _menciona_equipo(completo, local), _menciona_equipo(completo, visitante)
    if ml and mv:
        marcadores = _POST_PARTIDO if fase == "post" else _PREVIA_PARTIDO + _PREVIA_ACCIONABLE
        return bool(re.search(r"\b\d+\s*[-:]\s*\d+\b", crudo)) or any(t in completo for t in marcadores)
    return fase == "previa" and (ml or mv) and any(t in completo for t in _PREVIA_ACCIONABLE)


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
    item = {
        "titulo": " ".join(str(titulo or "").split())[:220],
        "url": str(url or "").strip()[:500],
        "texto": " ".join(str(texto or "").split())[:700],
        "fecha": str(fecha or "")[:40],
        "proveedor": proveedor,
    }
    if not item["titulo"] or not item["url"].startswith(("http://", "https://")):
        return None
    if local and visitante and not _es_relevante(item["titulo"], item["texto"], local, visitante, fase):
        return None
    return item


def _tavily(query: str, local: str = "", visitante: str = "", fase: str = "previa") -> List[Dict[str, str]]:
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key or not _reclamar_credito():
        return []
    payload: Dict[str, Any] = {
        "api_key": key,
        "query": query,
        "topic": "news",
        "search_depth": "basic",
        "max_results": _MAX_RESULTADOS,
        "days": 30,
        "include_answer": False,
        "include_raw_content": False,
    }
    dominios = list(dict.fromkeys(dominios_equipo(local) + dominios_equipo(visitante)))[:8]
    if dominios:
        payload["include_domains"] = dominios
    respuesta = requests.post("https://api.tavily.com/search", json=payload, timeout=20)
    respuesta.raise_for_status()
    salida = []
    for raw in respuesta.json().get("results", []):
        if isinstance(raw, dict) and (
            normalizado := _item(
                raw.get("title"),
                raw.get("url"),
                raw.get("content"),
                raw.get("published_date"),
                "tavily",
                local,
                visitante,
                fase,
            )
        ):
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
    salida = []
    for raw in respuesta.json().get("articles", []):
        if isinstance(raw, dict) and (
            normalizado := _item(
                raw.get("title"),
                raw.get("url"),
                raw.get("description") or raw.get("content"),
                raw.get("publishedAt"),
                "gnews",
                local,
                visitante,
                fase,
            )
        ):
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
    salida = []
    for raw in respuesta.json().get("news", []):
        if isinstance(raw, dict) and (
            normalizado := _item(
                raw.get("title"), raw.get("link"), raw.get("snippet"), raw.get("date"), "serper", local, visitante, fase
            )
        ):
            salida.append(normalizado)
    return salida


def _consulta(
    local: str, visitante: str, fase: str, jornada: Optional[int] = None, fecha_partido: Optional[str] = None
) -> str:
    referencia = " ".join(x for x in (f"Jornada {jornada}" if jornada is not None else "", fecha_partido or "") if x)
    if fase == "post":
        return f'"{local}" "{visitante}" {referencia} resultado resumen goles lesiones expulsiones Liga MX'
    return f'"{local}" "{visitante}" {referencia} lesiones suspensiones bajas alineación rotaciones convocatoria regreso Liga MX'


def _buscar(
    local: str, visitante: str, fase: str, jornada: Optional[int] = None, fecha_partido: Optional[str] = None
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
    unicos, urls = [], set()
    for resultado in resultados:
        url = resultado["url"].split("#", 1)[0]
        if url in urls:
            continue
        urls.add(url)
        unicos.append(resultado)
        if len(unicos) >= _MAX_RESULTADOS:
            break
    return unicos


def _resumir(resultados: Sequence[Mapping[str, str]], local: str = "", visitante: str = "") -> Dict[str, Any]:
    seleccion = list(resultados[:3])
    hallazgos = [str(r.get("titulo") or "") for r in seleccion if r.get("titulo")]
    fuentes = [str(r.get("url") or "") for r in seleccion if r.get("url")]
    proveedores = list(dict.fromkeys(str(r.get("proveedor") or "") for r in seleccion if r.get("proveedor")))
    eventos = [clasificar_evento(r, local, visitante) for r in seleccion] if local and visitante else []
    resumen = "; ".join(hallazgos)[:500]
    return {
        "disponible": bool(resumen),
        "resumen": resumen,
        "hallazgos": hallazgos,
        "fuentes": fuentes,
        "proveedores": proveedores,
        "eventos": eventos,
        "estado": _estado_eventos(eventos),
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
    if not forzar and (cache := _leer_cache(local, visitante, fase, jornada)) is not None:
        return cache
    ahora = _ahora().isoformat()
    contexto = {
        "local": local,
        "visitante": visitante,
        "fase": fase,
        "jornada": _jornada_valida(jornada),
        "fecha_partido": str(fecha_partido or "")[:10],
        "actualizado_en": ahora,
        "frescura": "FRESCA",
        "cache": False,
        **_resumir(_buscar(local, visitante, fase, jornada, fecha_partido), local, visitante),
    }
    _guardar_cache(local, visitante, fase, contexto, jornada, fecha_partido)
    return contexto


def contexto_cache_partido(local: str, visitante: str, jornada: Optional[int]) -> Optional[Dict[str, Any]]:
    """Ruta no bloqueante para Telegram: nunca consulta proveedores externos."""
    return _leer_cache(local, visitante, "previa", jornada)


def contextos_para_plan(plan: Mapping[str, Any], limite: int = 3) -> List[Dict[str, Any]]:
    salida: List[Dict[str, Any]] = []
    pasos = plan.get("plan")
    if not isinstance(pasos, list):
        return salida
    for paso in pasos[: max(0, limite)]:
        if not isinstance(paso, dict):
            continue
        jornada_valor = paso.get("jornada")
        if jornada_valor is None:
            continue
        try:
            jornada = int(jornada_valor)
        except (TypeError, ValueError):
            continue
        equipo, rival = str(paso.get("equipo") or ""), str(paso.get("rival") or "")
        local, visitante = (
            (equipo, rival) if str(paso.get("condicion") or "").lower().startswith("local") else (rival, equipo)
        )
        previa = _leer_cache(local, visitante, "previa", jornada)
        if previa and previa.get("disponible"):
            salida.append(
                {
                    "jornada": jornada,
                    "equipo": equipo,
                    "resumen": str(previa.get("resumen") or "")[:500],
                    "fuentes": previa.get("fuentes", []),
                    "estado": previa.get("estado", "PROVISIONAL"),
                    "frescura": previa.get("frescura", "DESCONOCIDA"),
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
    return [p for p in partidos if isinstance(p, dict)] if isinstance(partidos, list) else []


def actualizar_contexto_automatico(hoy: Optional[date] = None) -> Dict[str, Any]:
    if not _habilitado():
        return {"ejecutado": False, "motivo": "sin proveedores configurados"}
    from src.planificador_survivor import cargar_calendario

    hoy, calendario, tareas = hoy or _ahora().date(), cargar_calendario() or [], []
    for bloque in calendario:
        inicio = _fecha(bloque.get("fecha_inicio"))
        if inicio is None or not 0 <= (inicio - hoy).days <= _dias_previa():
            continue
        jornada_valor = bloque.get("jornada")
        if jornada_valor is None:
            continue
        try:
            jornada = int(jornada_valor)
        except (TypeError, ValueError):
            continue
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
    completadas = [b for b in calendario if (fin := _fecha(b.get("fecha_fin"))) is not None and fin < hoy]
    completadas.sort(key=lambda b: int(b.get("jornada") or 0))
    for bloque in completadas:
        jornada_valor = bloque.get("jornada")
        if jornada_valor is None:
            continue
        try:
            jornada = int(jornada_valor)
        except (TypeError, ValueError):
            continue
        fin = _fecha(bloque.get("fecha_fin"))
        for partido in _partidos_bloque(bloque):
            tareas.append(
                (
                    jornada,
                    "post",
                    str(partido.get("home_team") or ""),
                    str(partido.get("away_team") or ""),
                    fin.isoformat() if fin else "",
                )
            )
    try:
        limite = max(1, min(18, int(os.getenv("WEB_CONTEXT_MATCH_LIMIT", "9") or "9")))
    except ValueError:
        limite = 9
    consultas = disponibles = revisados = 0
    jornadas: set[int] = set()
    for jornada, fase, local, visitante, fecha_partido in tareas:
        if not local or not visitante:
            continue
        contexto = investigar_partido(local, visitante, fase=fase, jornada=jornada, fecha_partido=fecha_partido)
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
