import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

# Cargar .env en local (en Render/prod las vars vienen del entorno; esto es no-op).
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv es opcional
    logger.debug("dotenv opcional no disponible", exc_info=True)

from src.database import init_db, get_metrics, get_history, settle_pick
from src.rate_limit import limiter

from pydantic import BaseModel
from typing import Dict, List


class HealthResponse(BaseModel):
    """Respuesta del healthcheck del sistema."""

    status: str  # "ok" o "degradado"
    version: str
    timestamp: str
    dependencias: Dict[str, str]


class ErrorResponse(BaseModel):
    """Respuesta de error estándar."""

    detail: str
    error_code: Optional[str] = None
    timestamp: Optional[str] = None


class UsadosResponse(BaseModel):
    """Lista de equipos usados en el Survivor."""

    usados: List[str]
    total: int
    temporada: Optional[str] = None
    decision: str = "INFORMATIVO / REVISIÓN HUMANA"
    error: Optional[str] = None


class UsadoResponse(BaseModel):
    """Resultado de agregar/quitar un equipo usado."""

    equipo: str
    agregado: Optional[bool] = None
    quitado: Optional[bool] = None
    ya_estaba: Optional[bool] = None
    usados: List[str]
    temporada: Optional[str] = None


class SurvivorPickResponse(BaseModel):
    """Selección Survivor persistida para una temporada y jornada."""

    temporada: str
    jornada: int
    fecha: Optional[str] = None
    equipo: str
    rival: Optional[str] = None
    condicion: Optional[str] = None
    local: Optional[str] = None
    visitante: Optional[str] = None
    no_perder_pct: float = 0.0
    prob_victoria_pct: float = 0.0
    estado: str
    resultado: Optional[str] = None
    marcador_real: Optional[str] = None
    origen: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    confirmado_at: Optional[datetime] = None
    bloqueado_at: Optional[datetime] = None
    resuelto_at: Optional[datetime] = None


class MiSurvivorResponse(BaseModel):
    """Estado completo de la participación Survivor del dueño."""

    temporada: str
    sigue_vivo: bool
    racha: int
    victorias: int
    empates: int
    derrotas: int
    usados: List[str]
    pick_actual: Optional[SurvivorPickResponse] = None
    picks: List[SurvivorPickResponse]


class MetricsResponse(BaseModel):
    """Métricas de rendimiento del modelo."""

    total_picks: int
    accuracy_1x2: Optional[float] = None
    accuracy_marcador: Optional[float] = None
    brier_score: Optional[float] = None
    accuracy_por_jornada: List[dict]
    latencia_espn_promedio_ms: Optional[float] = None
    total_predicciones: int
    ultima_actualizacion: Optional[str] = None


class PrediccionItem(BaseModel):
    """Una predicción individual."""

    equipo_local: str
    equipo_visitante: str
    probabilidad_local: float
    probabilidad_empate: float
    probabilidad_visitante: float
    no_perder_pct: Optional[float] = None
    over_pct: Optional[float] = None
    under_pct: Optional[float] = None
    btts_pct: Optional[float] = None


class PrediccionesResponse(BaseModel):
    """Lista de predicciones."""

    pronosticos: List[PrediccionItem]
    fuente_datos: Optional[str] = None
    generado_utc: Optional[str] = None
    decision: str = "INFORMATIVO / REVISIÓN HUMANA"
    error: Optional[str] = None


class CronResponse(BaseModel):
    """Respuesta de un endpoint CRON."""

    status: str = "ok"
    message: Optional[str] = None
    timestamp: str


# Autenticación por API key (X-API-Key): la lógica vive en src/auth.py para que
# los routers puedan reutilizarla sin import circular (api.py importa los routers).
# La clave DEBE venir del entorno (Render / GitHub secret); si no está configurada,
# los endpoints protegidos fallan en cerrado (503).
from src.auth import verify_api_key  # noqa: E402


app = FastAPI(title="Survivor LigaMX API Premium", version="2.1.0", docs_url="/docs")
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard_ui"
# CORS configurable: en producción usa CORS_ORIGINS="https://tudominio.com,https://otro.com"
# En desarrollo deja vacío (solo localhost) o define CORS_ORIGINS=* explícitamente.
_cors_raw = os.getenv("CORS_ORIGINS", "").strip()
if _cors_raw:
    allow_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()]
else:
    # Fallback seguro: no abrir CORS a "*" por defecto en prod.
    allow_origins = (
        ["*"]
        if os.getenv("CORS_ALLOW_ALL", "false").lower() == "true"
        else ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8000", "null"]
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.state.limiter = limiter


# Autoprogramado semanal del análisis de jornada (solo si SCHEDULER_ENABLED=1).
# Arranca un hilo daemon; es idempotente (no se duplica con reload de uvicorn).
try:
    if not getattr(app.state, "_scheduler_started", False):
        from src.scheduler import arrancar as _arrancar_scheduler

        _arrancar_scheduler()
        app.state._scheduler_started = True
except Exception:  # pragma: no cover - el scheduler es opcional
    logger.debug("Exception silenciada en arranque del scheduler", exc_info=True)


app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]  # firma de slowapi mas estrecha que la anotacion de Starlette
from src.routers.cron_router import router as cron_router

app.include_router(cron_router)
from src.routers.predicciones import router as predicciones_router

app.include_router(predicciones_router)
from src.routers.api_ligamx import router as api_ligamx_router

app.include_router(api_ligamx_router)
init_db()

# Render repara idempotentemente el webhook y sincroniza el menú de comandos
# sin bloquear el arranque del servicio.
try:
    from src.telegram.configuracion import iniciar_sincronizacion_telegram

    iniciar_sincronizacion_telegram()
except Exception:  # pragma: no cover - Telegram es una integración externa
    logger.exception("No se pudo iniciar la sincronización de Telegram")

# NOTA: el viejo path de "picks de alto EV" leia un parquet de momios scrapeados
# (data_kiro/ligamx_odds_clean.parquet) que NO existe en produccion (Render) y
# dependia de momios. El proyecto pivoto a predicciones reales (ESPN + Poisson).
# Los endpoints /predicciones y /survivor (src/routers/predicciones.py) son la
# fuente real; /picks/latest queda como alias DEPRECADO que reexpone esa data.


def _predicciones_reales() -> dict:
    """Obtiene las predicciones reales (ESPN + Poisson) usando la cache del router."""
    try:
        from src.routers.predicciones import _obtener as _obtener_predicciones

        return _obtener_predicciones()
    except Exception as exc:  # pragma: no cover - fallback defensivo
        return {
            "pronosticos": [],
            "fuente_datos": None,
            "generado_utc": None,
            "decision": "INFORMATIVO / REVISIÓN HUMANA",
            "error": str(exc),
        }


@app.get("/health", response_model=HealthResponse, summary="Estado del sistema", tags=["Status"])
def health():
    """
    Healthcheck del sistema con estado de cada dependencia.
    - base_de_datos: ok si responde, error si no
    - espn: ok si responde, error si no
    - ligamx_api: ok si responde, error si no
    """
    import requests

    deps = {
        "base_de_datos": "error",
        "espn": "error",
        "ligamx_api": "error",
        "telegram_webhook": "error",
    }
    status_global = "ok"

    # 1) Base de datos
    try:
        from src.database import get_equipos_usados

        get_equipos_usados()
        deps["base_de_datos"] = "ok"
    except Exception as e:
        status_global = "degradado"
        deps["base_de_datos"] = f"error: {e}"

    # 2) ESPN
    try:
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/soccer/mex.1/scoreboard", timeout=10)
        if r.status_code == 200:
            deps["espn"] = "ok"
        else:
            deps["espn"] = f"error: HTTP {r.status_code}"
            status_global = "degradado"
    except Exception as e:
        deps["espn"] = f"error: {e}"
        status_global = "degradado"

    # 3) ligamx-api (hermana)
    try:
        r = requests.get("https://ligamx-api.onrender.com/health", timeout=10)
        if r.status_code == 200:
            deps["ligamx_api"] = "ok"
        else:
            deps["ligamx_api"] = f"error: HTTP {r.status_code}"
            status_global = "degradado"
    except Exception as e:
        deps["ligamx_api"] = f"error: {e}"
        status_global = "degradado"

    # 4) Telegram: configuración local + último resultado remoto de sincronización.
    try:
        from src.telegram.configuracion import estado_sincronizacion_telegram, obtener_secreto_webhook

        token_configurado = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
        chat_configurado = bool(os.getenv("TELEGRAM_CHAT_ID", "").strip())
        telegram_local = token_configurado and chat_configurado and bool(obtener_secreto_webhook())
        estado_telegram = estado_sincronizacion_telegram()
        if telegram_local and (not os.getenv("RENDER") or estado_telegram.get("ok")):
            deps["telegram_webhook"] = "ok"
        elif telegram_local and estado_telegram.get("estado") == "sincronizando":
            deps["telegram_webhook"] = "sincronizando"
            status_global = "degradado"
        elif os.getenv("RENDER") or token_configurado or chat_configurado:
            detalle = str(estado_telegram.get("error") or "configuración incompleta")[:160]
            deps["telegram_webhook"] = f"error: {detalle}"
            status_global = "degradado"
        else:
            deps["telegram_webhook"] = "deshabilitado"
    except Exception as e:
        deps["telegram_webhook"] = f"error: {e}"
        status_global = "degradado"

    return {
        "status": status_global,
        "version": "2.1.0-premium",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dependencias": deps,
    }


@app.get("/health/telegram", summary="Diagnóstico seguro del webhook de Telegram", tags=["Status"])
@limiter.limit("10/minute")
def health_telegram(request: Request):
    """Muestra URL/menú/error remoto sin exponer token, chat ID ni secreto."""
    from src.telegram.configuracion import diagnosticar_telegram

    return diagnosticar_telegram()


@app.get("/picks/latest", summary="(Deprecado) Predicciones reales ESPN+Poisson", tags=["Picks"])
@limiter.limit("10/minute")
def get_picks(request: Request, api_key: str = Depends(verify_api_key)):
    """
    DEPRECADO: el viejo path de 'picks de alto EV' dependia de momios scrapeados
    inexistentes en produccion. Ahora reexpone las predicciones REALES del modelo
    (ESPN + Poisson). Usa directamente /predicciones y /survivor.
    """
    data = _predicciones_reales()
    return {
        "status": "deprecated",
        "message": "Usa /predicciones (1X2/OU/BTTS por partido) y /survivor (mejor no-perder). Datos reales de ESPN + modelo Poisson.",
        "last_update": data.get("generado_utc"),
        "fuente_datos": data.get("fuente_datos"),
        "predicciones": data.get("pronosticos", []),
        "decision": data.get("decision"),
    }


@app.post("/alerts/pronosticos", summary="Enviar pronósticos reales por Telegram", tags=["Alerts"])
@limiter.limit("6/minute")
def alerts_pronosticos(request: Request, api_key: str = Depends(verify_api_key)):
    """Genera predicciones reales (ESPN + Poisson) y las envía por Telegram."""
    from src import telegram_pronosticos

    clave = f"cron:pronosticos:{datetime.now(timezone.utc).date().isoformat()}"
    return telegram_pronosticos.enviar_pronosticos(idempotency_key=clave)


@app.post("/alerts/high-ev", summary="(Deprecado) Alias → pronósticos reales", tags=["Alerts"])
@limiter.limit("6/minute")
def alerts_high_ev(request: Request, api_key: str = Depends(verify_api_key)):
    """
    Compatibilidad con el workflow auto-alerts. El viejo 'EV>5%' se basaba en
    momios inventados; ahora envía PRONÓSTICOS REALES (ESPN + Poisson).
    """
    from src import telegram_pronosticos

    res = telegram_pronosticos.enviar_pronosticos()
    res["nota"] = "Endpoint deprecado: usa /alerts/pronosticos. Envía predicciones reales."
    return res


@app.post("/alerts/plan", summary="Enviar el plan de temporada Survivor por Telegram", tags=["Alerts"])
@limiter.limit("6/minute")
def alerts_plan(request: Request, api_key: str = Depends(verify_api_key)):
    """
    Construye el plan ÓPTIMO de Survivor para la temporada (qué equipo usar en
    cada jornada) y lo envía por Telegram. Requiere data/calendario.json.
    """
    from src import telegram_pronosticos

    return telegram_pronosticos.enviar_plan()


@app.post("/alerts/resumen", summary="Enviar resumen de rentabilidad (track-record) por Telegram", tags=["Alerts"])
@limiter.limit("6/minute")
def alerts_resumen(request: Request, api_key: str = Depends(verify_api_key)):
    """Envía por Telegram el track-record del modelo (aciertos 1X2 y marcador)."""
    from src import telegram_pronosticos

    return telegram_pronosticos.enviar_resumen_rentabilidad()


@app.post("/alerts/momios", summary="Bajar momios (odds-api.io) y reportar cobertura por Telegram", tags=["Alerts"])
@limiter.limit("6/minute")
def alerts_momios(request: Request, solo_si_hay: bool = False, api_key: str = Depends(verify_api_key)):
    """
    Baja los momios de Liga MX (1X2/OU/hándicap), los guarda como caché y envía
    por Telegram un resumen de cobertura. El pick y el plan los usan al instante.

    `solo_si_hay=true` (para el cron): refresca en silencio y solo avisa por
    Telegram si YA hay líneas (evita spam en pretemporada).
    """
    from src import telegram_pronosticos

    clave = f"cron:momios:{datetime.now(timezone.utc).date().isoformat()}" if solo_si_hay else None
    return telegram_pronosticos.enviar_momios_estado(solo_si_hay=solo_si_hay, idempotency_key=clave)


@app.post("/alerts/recordatorio", summary="Recordar por Telegram que se acerca la jornada", tags=["Alerts"])
@limiter.limit("6/minute")
def alerts_recordatorio(request: Request, dias_antes: int = 1, api_key: str = Depends(verify_api_key)):
    """
    Envía un recordatorio SOLO si la próxima jornada arranca dentro de `dias_antes`
    días. Pensado para un cron diario (no spamea: solo dispara al acercarse).
    """
    from src import telegram_pronosticos

    return telegram_pronosticos.enviar_recordatorio_si_aplica(dias_antes=dias_antes)


@app.post("/fichajes", summary="Importar altas/bajas de un equipo (asistido, sin scraping)", tags=["Datos"])
@limiter.limit("30/minute")
def set_fichajes(
    request: Request, equipo: str, altas: str = "", bajas: str = "", api_key: str = Depends(verify_api_key)
):
    """
    Guarda altas/bajas de un equipo (datos de Transfermarkt que TÚ validas; no se
    scrapea). `altas`/`bajas` separadas por coma. Ej:
    POST /fichajes?equipo=America&altas=Jugador A,Jugador B&bajas=Jugador C
    """
    from src import fichajes

    a = [x for x in altas.split(",") if x.strip()]
    b = [x for x in bajas.split(",") if x.strip()]
    guardado = fichajes.guardar_equipo(equipo, a, b)
    return {"equipo": equipo, "guardado": guardado}


@app.get("/fichajes", summary="Ver altas/bajas de un equipo", tags=["Datos"])
@limiter.limit("30/minute")
def get_fichajes(request: Request, equipo: str):
    """Devuelve las altas/bajas guardadas de un equipo (informativo, público)."""
    from src import fichajes

    return {"equipo": equipo, **fichajes.resumen_equipo(equipo)}


# ---------------------------------------------------------------------------
# Equipos usados en el Survivor (persisten en la BD; el pick los excluye).
# ---------------------------------------------------------------------------
@app.get(
    "/survivor/usados",
    response_model=UsadosResponse,
    summary="Lista de equipos ya usados en el Survivor",
    tags=["Survivor"],
)
@limiter.limit("30/minute")
def survivor_usados_listar(request: Request, temporada: Optional[str] = None):
    """Equipos que ya gastaste (se excluyen automáticamente del pick y del plan)."""
    try:
        from src.database import get_equipos_usados, temporada_survivor_actual

        temporada = temporada or temporada_survivor_actual()
        usados = get_equipos_usados(temporada)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        return {"usados": [], "total": 0, "temporada": temporada, "error": str(exc)}
    return {
        "usados": usados,
        "total": len(usados),
        "temporada": temporada,
        "decision": "INFORMATIVO / REVISIÓN HUMANA",
    }


@app.post("/survivor/usados", response_model=UsadoResponse, summary="Marcar un equipo como usado", tags=["Survivor"])
@limiter.limit("30/minute")
def survivor_usados_agregar(
    request: Request,
    equipo: str,
    temporada: Optional[str] = None,
    jornada: Optional[int] = None,
    api_key: str = Depends(verify_api_key),
):
    """Compatibilidad: registra un usado; para el ciclo completo usa /survivor/picks/confirmar."""
    from src.database import add_equipo_usado, get_equipos_usados, temporada_survivor_actual

    if not equipo or not equipo.strip():
        raise HTTPException(status_code=400, detail="Falta el parámetro 'equipo'.")
    temporada = temporada or temporada_survivor_actual()
    try:
        agregado = add_equipo_usado(equipo, temporada=temporada, jornada=jornada)
        usados = get_equipos_usados(temporada)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "equipo": equipo.strip(),
        "agregado": agregado,
        "ya_estaba": not agregado,
        "usados": usados,
        "temporada": temporada,
    }


@app.delete("/survivor/usados", summary="Quitar un equipo usado", tags=["Survivor"])
@limiter.limit("30/minute")
def survivor_usados_quitar(
    request: Request,
    equipo: str,
    temporada: Optional[str] = None,
    api_key: str = Depends(verify_api_key),
):
    """Quita un equipo de usados en la temporada activa; no borra picks resueltos."""
    from src.database import get_equipos_usados, remove_equipo_usado, temporada_survivor_actual

    temporada = temporada or temporada_survivor_actual()
    try:
        filas = remove_equipo_usado(equipo, temporada)
        usados = get_equipos_usados(temporada)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "equipo": equipo.strip(),
        "quitado": bool(filas),
        "usados": usados,
        "temporada": temporada,
    }


@app.post("/survivor/usados/reset", summary="Limpiar marcadores manuales de usados", tags=["Survivor"])
@limiter.limit("10/minute")
def survivor_usados_reset(
    request: Request,
    temporada: Optional[str] = None,
    api_key: str = Depends(verify_api_key),
):
    """Vacía usados solo para la temporada indicada; conserva el historial."""
    from src.database import clear_equipos_usados, get_equipos_usados, temporada_survivor_actual

    temporada = temporada or temporada_survivor_actual()
    try:
        borrados = clear_equipos_usados(temporada)
        usados = get_equipos_usados(temporada)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"borrados": borrados, "usados": usados, "temporada": temporada}


@app.get(
    "/survivor/mio",
    response_model=MiSurvivorResponse,
    summary="Mi participación Survivor por temporada",
    tags=["Survivor"],
)
@limiter.limit("30/minute")
def mi_survivor(
    request: Request,
    temporada: Optional[str] = None,
    api_key: str = Depends(verify_api_key),
):
    """Devuelve racha, usados, pick actual e historial; es la vista principal del producto."""
    from src.database import resumen_mi_survivor

    try:
        return resumen_mi_survivor(temporada)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/survivor/picks/confirmar",
    response_model=SurvivorPickResponse,
    summary="Confirmar el pick real de una jornada",
    tags=["Survivor"],
)
@limiter.limit("20/minute")
def survivor_pick_confirmar(
    request: Request,
    jornada: int,
    equipo: str,
    temporada: Optional[str] = None,
    rival: str = "",
    condicion: str = "",
    local: str = "",
    visitante: str = "",
    fecha: str = "",
    api_key: str = Depends(verify_api_key),
):
    """Confirma la decisión humana y excluye el equipo de futuras recomendaciones."""
    from src.database import confirmar_survivor_pick, temporada_survivor_actual

    try:
        return confirmar_survivor_pick(
            temporada or temporada_survivor_actual(),
            jornada,
            equipo,
            rival=rival,
            condicion=condicion,
            local=local,
            visitante=visitante,
            fecha=fecha,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/survivor/picks/{jornada}/bloquear",
    response_model=SurvivorPickResponse,
    summary="Bloquear el pick confirmado",
    tags=["Survivor"],
)
@limiter.limit("20/minute")
def survivor_pick_bloquear(
    request: Request,
    jornada: int,
    temporada: Optional[str] = None,
    api_key: str = Depends(verify_api_key),
):
    """Bloquea una selección confirmada para evitar cambios accidentales."""
    from src.database import bloquear_survivor_pick, temporada_survivor_actual

    try:
        return bloquear_survivor_pick(temporada or temporada_survivor_actual(), jornada)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/survivor/picks/{jornada}/resolver",
    response_model=SurvivorPickResponse,
    summary="Resolver el resultado del pick",
    tags=["Survivor"],
)
@limiter.limit("20/minute")
def survivor_pick_resolver(
    request: Request,
    jornada: int,
    resultado: str,
    temporada: Optional[str] = None,
    marcador_real: str = "",
    api_key: str = Depends(verify_api_key),
):
    """Cierra la jornada como gano, empate o perdio, conservando el historial."""
    from src.database import resolver_survivor_pick, temporada_survivor_actual

    try:
        return resolver_survivor_pick(
            temporada or temporada_survivor_actual(),
            jornada,
            resultado,
            marcador_real=marcador_real,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Webhook de Telegram: operar el bot por chat (/usado, /usados, /pick, ...).
# ---------------------------------------------------------------------------
@app.post("/telegram/webhook", summary="Webhook de comandos de Telegram", tags=["Telegram"])
@limiter.limit("30/minute")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    """
    Recibe updates de Telegram y responde a todos los comandos del dueño,
    incluidos /mipick, /confirmar, /bloquear y /resolver. Si hay un secreto
    explícito lo usa; si no, deriva uno estable de API_KEY + BOT_TOKEN.
    """
    # 1) Validación del secreto del webhook (fail-closed en producción).
    from src.telegram.configuracion import obtener_secreto_webhook

    secreto = obtener_secreto_webhook()
    if os.getenv("RENDER") and not secreto:
        raise HTTPException(
            status_code=503,
            detail="TELEGRAM_WEBHOOK_SECRET no configurado y no se pudo derivar de API_KEY + BOT_TOKEN",
        )

    if secreto and x_telegram_bot_api_secret_token != secreto:
        raise HTTPException(status_code=403, detail="Secreto de webhook inválido")

    try:
        update = await request.json()
    except Exception:
        return {"ok": True}  # ignora payloads no-JSON sin fallar

    from src import telegram_webhook as tw
    from src import telegram_pronosticos as tp

    chat_id, texto = tw.extraer_mensaje(update)

    # 2) Solo el dueño (chat configurado) puede operar; falla cerrado.
    chat_cfg = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_cfg:
        raise HTTPException(status_code=503, detail="TELEGRAM_CHAT_ID no configurado")
    if str(chat_id) != chat_cfg:
        return {"ok": True}  # ignora mensajes de otros chats

    if not texto:
        return {"ok": True}

    cmd, arg = tw.parsear_comando(texto)
    if cmd is None:
        return {"ok": True}  # texto normal, no comando

    update_id_raw = update.get("update_id")
    update_id = update_id_raw if isinstance(update_id_raw, int) and not isinstance(update_id_raw, bool) else None
    update_reclamado = False
    if update_id is not None:
        from src.database import reclamar_telegram_update

        update_reclamado = reclamar_telegram_update(update_id)
        if not update_reclamado:
            return {"ok": True, "duplicado": True, "update_id": update_id}

    try:
        enviado = True

        def _enviar(mensaje: str) -> bool:
            ok = bool(tp.enviar_mensaje(mensaje))
            if not ok:
                logger.error("Telegram rechazó la respuesta al comando /%s", cmd)
            return ok

        if cmd in tw.CMDS_PICK:
            # Generación pesada (ESPN+modelo) en segundo plano; responde rápido.
            background_tasks.add_task(tp.enviar_pronosticos)
            enviado = _enviar("🔄 Generando tu pronóstico y pick de la jornada...")
        elif cmd in tw.CMDS_PLAN:
            background_tasks.add_task(tp.enviar_plan)
            enviado = _enviar("🔄 Armando tu plan de temporada (las 17 jornadas)...")
        elif cmd in tw.CMDS_MOMIOS:
            background_tasks.add_task(tp.enviar_momios_estado)
            enviado = _enviar("🔄 Bajando momios y revisando cobertura...")
        elif cmd in tw.CMDS_SEGUIMIENTO:
            background_tasks.add_task(tp.enviar_seguimiento)
            enviado = _enviar("🔄 Armando tu lista de seguimiento de la jornada...")
        elif cmd in tw.CMDS_PRUEBA:
            background_tasks.add_task(tp.enviar_prueba)
            enviado = _enviar("🔄 Probando la estrategia con torneos pasados (tarda un poco)...")
        elif cmd in tw.CMDS_CONFIANZA:
            background_tasks.add_task(tp.enviar_confianza)
            enviado = _enviar("🔄 Revisando qué tan honesta es la confianza del bot...")
        elif cmd in tw.CMDS_DERROTAS:
            background_tasks.add_task(tp.enviar_derrotas)
            enviado = _enviar("🔄 Revisando en qué partidos cayó el bot y por qué...")
        elif cmd in tw.CMDS_GANADORES:
            background_tasks.add_task(tp.enviar_ganadores)
            enviado = _enviar("🔄 Calculando el 'Survivor perfecto' y comparándolo con el bot...")
        elif cmd in tw.CMDS_ANALISIS:
            background_tasks.add_task(tp.enviar_analisis_jornada)
            enviado = _enviar("🔄 Analizando la jornada: goles, tarjetas, alineaciones y conclusiones...")
        else:
            enviado = _enviar(tw.responder(cmd, arg))
            if not enviado:
                # Las operaciones ligeras son idempotentes; 502 hace que Telegram
                # reintente el update en lugar de perder /mipick silenciosamente.
                raise HTTPException(status_code=502, detail="No se pudo entregar la respuesta en Telegram")
        if not enviado:
            raise HTTPException(status_code=502, detail="No se pudo entregar la respuesta en Telegram")
    except Exception as exc:
        if update_reclamado and update_id is not None:
            from src.database import fallar_telegram_update

            fallar_telegram_update(update_id, type(exc).__name__)
        raise
    else:
        if update_reclamado and update_id is not None:
            from src.database import completar_telegram_update

            completar_telegram_update(update_id)
        return {"ok": enviado, "comando": cmd, "update_id": update_id}


@app.get("/stats", summary="Métricas de rendimiento", tags=["Analytics"])
@limiter.limit("20/minute")
def premium_stats(request: Request, api_key: str = Depends(verify_api_key)):
    return get_metrics()


@app.get("/history", summary="Historial paginado", tags=["Analytics"])
@limiter.limit("20/minute")
def get_history_endpoint(request: Request, limit: int = 20, offset: int = 0, api_key: str = Depends(verify_api_key)):
    try:
        rows = get_history(limit, offset)
        return {"total": len(rows), "records": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/metrics", response_model=MetricsResponse, summary="Métricas de rendimiento del modelo", tags=["Analytics"])
@limiter.limit("10/minute")
def get_metrics_endpoint(request: Request):
    """
    Métricas de negocio del modelo:
    - Accuracy global (1X2 y marcador exacto)
    - Accuracy por jornada (últimas 5)
    - Total de predicciones
    - Brier score (calibración)
    - Latencia promedio de ESPN
    """
    try:
        from src.database import get_metrics as _get_metrics

        base = _get_metrics()
    except Exception:
        base = {"total_picks": 0, "wins": 0, "win_rate": 0.0, "total_profit": 0.0}

    try:
        from src.validacion_modelo import metricas_rendimiento

        extra = metricas_rendimiento()
    except Exception:
        extra = {}

    return {
        "total_picks": base.get("total_picks", 0),
        "accuracy_1x2": extra.get("accuracy_1x2", None),
        "accuracy_marcador": extra.get("accuracy_marcador", None),
        "brier_score": extra.get("brier_score", None),
        "accuracy_por_jornada": extra.get("accuracy_por_jornada", []),
        "latencia_espn_promedio_ms": extra.get("latencia_espn_promedio_ms", None),
        "total_predicciones": extra.get("total_predicciones", base.get("total_picks", 0)),
        "ultima_actualizacion": extra.get("ultima_actualizacion", None),
    }


@app.post("/backtest/settle/{pick_id}", summary="Validar resultado de pick", tags=["Analytics"])
def settle_pick_endpoint(
    pick_id: int, result: float = 0.0, profit_loss: float = 0.0, api_key: str = Depends(verify_api_key)
):
    try:
        settle_pick(pick_id, result, profit_loss)
        return {"status": "updated", "pick_id": pick_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


DASHBOARD_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self'; script-src 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@app.get("/", include_in_schema=False)
def root():
    """Entrada del producto: lleva directamente a Mi Survivor."""
    return RedirectResponse(url="/dashboard", status_code=307, headers={"Cache-Control": "no-store"})


@app.get("/dashboard/assets/{asset_name}", include_in_schema=False)
def dashboard_asset(asset_name: str):
    """Sirve únicamente los dos assets permitidos; nunca publica el HTML alterno."""
    media_types = {"app.css": "text/css", "app.js": "application/javascript"}
    media_type = media_types.get(asset_name)
    if media_type is None:
        raise HTTPException(status_code=404, detail="Asset no encontrado")
    return FileResponse(
        DASHBOARD_DIR / asset_name,
        media_type=media_type,
        headers=DASHBOARD_SECURITY_HEADERS,
    )


@app.get("/dashboard", summary="Mi Survivor", tags=["Dashboard"])
def dashboard():
    """Shell owner-only: los datos se solicitan con X-API-Key desde la pestaña."""
    return FileResponse(
        DASHBOARD_DIR / "index.html",
        media_type="text/html",
        headers=DASHBOARD_SECURITY_HEADERS,
    )


@app.get("/debug/jornadas", summary="Debug: ver contenido de jornadas.json", tags=["Debug"])
@limiter.limit("5/minute")
def debug_jornadas(request: Request):
    """Muestra el contenido de jornadas.json para debugging"""
    import json

    try:
        jornadas_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "jornadas.json"
        )
        with open(jornadas_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Contar partidos
        if isinstance(data, list):
            count = len(data)
            sample = data[:2] if data else []
        else:
            partidos = data.get("partidos", [])
            count = len(partidos)
            sample = partidos[:2] if partidos else []

        return {
            "status": "success",
            "total_partidos": count,
            "sample": sample,
            "structure": "list" if isinstance(data, list) else "dict",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/debug/scraper", summary="Debug: fixtures reales de ESPN", tags=["Debug"])
@limiter.limit("5/minute")
def debug_scraper(request: Request):
    """Diagnóstico de la fuente real: próximos fixtures de Liga MX desde ESPN."""
    try:
        from src import espn_data

        fixtures = espn_data.obtener_fixtures()
        return {
            "status": "success",
            "fuente": "ESPN (site.api.espn.com, mex.1)",
            "total_fixtures": len(fixtures),
            "sample": fixtures[:5],
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/debug/api-response", summary="Debug: estado de fuentes de datos", tags=["Debug"])
@limiter.limit("5/minute")
def debug_api_response(request: Request):
    """
    The Odds API fue descartada (no cubre Liga MX de forma fiable). La fuente
    real es ESPN. Este endpoint reporta cuántos resultados históricos se
    obtienen de la cadena de fuentes (ESPN → TheSportsDB → caché).
    """
    try:
        from src import fuentes_datos

        datos = fuentes_datos.obtener_resultados(meses=2)
        return {
            "status": "success",
            "nota": "The Odds API descartada; fuente real = ESPN. Ver /debug/scraper y /predicciones.",
            "fuente_usada": datos.get("fuente"),
            "total_resultados": datos.get("total"),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/analisis/jornada", summary="Análisis post-partido de la jornada", tags=["Analisis"])
@limiter.limit("5/minute")
def analisis_jornada(request: Request, fecha: Optional[str] = None, api_key: str = Depends(verify_api_key)):
    """
    Analiza TODOS los partidos YA JUGADOS de la jornada actual (o de `fecha`).
    Incluye: goles, tarjetas, alineaciones, eventos y conclusión IA por partido.
    Compara con picks anteriores del bot.
    Devuelve hasta 2 mensajes para Telegram.
    """
    try:
        from src import analista_resultados as ar
    except ImportError:  # pragma: no cover
        logger.debug("Exception silenciada en analisis_jornada", exc_info=True)

    resultado = ar.analizar_jornada(fecha=fecha)
    historial = ar.cargar_historial_resultados()
    return {
        "status": "success",
        "total_partidos": len(resultado.get("partidos", [])),
        "partidos": resultado.get("partidos", []),
        "resumen_mensaje_1": resultado.get("resumen", ""),
        "resumen_mensaje_2": resultado.get("resumen_2", ""),
        "tabla_posiciones": resultado.get("tabla_posiciones", ""),
        "historial_jornadas": historial.get("jornadas", [])[-5:],
    }


@app.post("/cron/analisis-semanal")
def cron_analisis_semanal(
    background_tasks: BackgroundTasks,
    request: Request,
    api_key: str = Depends(verify_api_key),
):
    """
    Disparador SEMANAL del análisis de jornada.
    Pensado para ser llamado por un Cron Job de Render (o cualquier scheduler)
    cada domingo tras cerrar la jornada. No bloquea: encola el envío por Telegram
    en segundo plano y responde 202 de inmediato.
    """
    background_tasks.add_task(_enviar_analisis_jornada_bg)
    return {
        "status": "accepted",
        "detail": "Análisis de jornada encolado para envío por Telegram.",
    }


def _enviar_analisis_jornada_bg() -> None:
    """Ejecuta enviar_analisis_jornada() en segundo plano (no rompe el request)."""
    try:
        from src.telegram_pronosticos import enviar_analisis_jornada
    except ImportError:  # pragma: no cover
        from telegram_pronosticos import enviar_analisis_jornada  # type: ignore
    try:
        enviar_analisis_jornada()
    except Exception:
        logger.debug("Exception silenciada en _enviar_analisis_jornada_bg", exc_info=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.api:app", host="0.0.0.0", port=8000, reload=True)
