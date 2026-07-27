#!/usr/bin/env python3
"""
database.py — Capa de persistencia unificada (Postgres en prod, SQLite en local).

Una sola puerta de acceso (`get_db`) para TODO el proyecto, evitando la
inconsistencia anterior (algunos endpoints abrían SQLite directamente con la
cadena de conexión de Postgres, lo que rompía en Render).

Backend según `DATABASE_URL`:
- `postgres://...` o `postgresql://...`  -> PostgreSQL (psycopg2), como en Render.
- cualquier otro valor / vacío           -> SQLite local (archivo), para dev/tests.

Las funciones públicas (init_db, save_pick, get_metrics, get_history,
settle_pick) funcionan igual en ambos backends.
"""

import json
import logging
import os
import re
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, Generator, List, Optional, cast

if TYPE_CHECKING:
    from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "") or ""

_pool: Optional["ThreadedConnectionPool"] = None


def _get_pool() -> Optional["ThreadedConnectionPool"]:
    """Inicializa el pool de conexiones para Postgres si es necesario."""
    global _pool
    if _pool is None:
        try:
            from psycopg2.pool import ThreadedConnectionPool
        except ImportError:
            return None

        dsn = DATABASE_URL
        if "sslmode=" not in dsn:
            dsn = dsn + ("?" if "?" not in dsn else "&") + "sslmode=require"

        _pool = ThreadedConnectionPool(minconn=1, maxconn=5, dsn=dsn)
    return _pool


def _es_postgres(url: str) -> bool:
    return url.startswith("postgres://") or url.startswith("postgresql://")


USE_POSTGRES = _es_postgres(DATABASE_URL)
PH = "%s" if USE_POSTGRES else "?"
SQLITE_PATH = DATABASE_URL if (DATABASE_URL and not USE_POSTGRES) else os.path.join("data", "premium_history.db")

SURVIVOR_ESTADOS = {"recomendado", "confirmado", "bloqueado", "resuelto", "cancelado"}
SURVIVOR_RESULTADOS = {"gano", "empate", "perdio"}
SURVIVOR_LEGACY_MIGRATION = "2026-07-survivor-usados-por-temporada-v1"
SURVIVOR_SEED_MIGRATION = "2026-07-survivor-apertura-picks-v1"


def normalizar_temporada(temporada: str) -> str:
    """Devuelve una clave canónica como ``Apertura-2026`` o ``Clausura-2027``."""
    valor = re.sub(r"[_\s]+", "-", str(temporada or "").strip())
    match = re.fullmatch(r"(?i)(apertura|clausura)-?(\d{4})", valor)
    if not match:
        raise ValueError("Temporada inválida. Usa el formato Apertura-AAAA o Clausura-AAAA.")
    return f"{match.group(1).title()}-{match.group(2)}"


def temporada_survivor_actual(referencia: Optional[date] = None) -> str:
    """Temporada activa; ``SURVIVOR_TEMPORADA`` permite fijarla en producción."""
    configurada = os.getenv("SURVIVOR_TEMPORADA", "").strip()
    if configurada:
        return normalizar_temporada(configurada)
    referencia = referencia or date.today()
    torneo = "Apertura" if referencia.month >= 7 else "Clausura"
    return f"{torneo}-{referencia.year}"


@contextmanager
def get_db() -> Generator[Any, None, None]:
    """Conexión al backend activo. Usa pool en Postgres, archivo en SQLite."""
    conn = None
    use_pool = False
    pool = None
    if USE_POSTGRES:
        pool = _get_pool()
        if pool:
            conn = pool.getconn()
            use_pool = True
    else:
        import sqlite3

        carpeta = os.path.dirname(SQLITE_PATH)
        if carpeta:
            os.makedirs(carpeta, exist_ok=True)
        conn = sqlite3.connect(SQLITE_PATH)

    if conn is None:
        raise RuntimeError("No se pudo establecer conexión con la base de datos.")

    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            logger.debug("Exception silenciada en get_db rollback", exc_info=True)
        raise
    finally:
        if use_pool and pool:
            pool.putconn(conn)
        else:
            conn.close()


def init_db() -> None:
    """Crea la tabla `picks` si no existe (sintaxis adaptada por backend)."""
    if USE_POSTGRES:
        id_col = "id SERIAL PRIMARY KEY"
    else:
        id_col = "id INTEGER PRIMARY KEY AUTOINCREMENT"
    with get_db() as conn:
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('survivor_schema_v1'))")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS picks (
                {id_col},
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                match_id TEXT,
                market TEXT,
                true_prob REAL,
                momio REAL,
                ev REAL,
                kelly_pct REAL,
                status TEXT DEFAULT 'pending',
                result REAL DEFAULT 0.0,
                profit_loss REAL DEFAULT 0.0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS survivor_usados (
                equipo_norm TEXT PRIMARY KEY,
                equipo TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS survivor_historial (
                jornada TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fecha TEXT,
                equipo TEXT,
                rival TEXT,
                condicion TEXT,
                local TEXT,
                visitante TEXT,
                no_perder_pct REAL,
                prob_victoria_pct REAL,
                marcador_real TEXT,
                estado TEXT DEFAULT 'pendiente',
                resuelto INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS survivor_picks (
                temporada TEXT NOT NULL,
                jornada INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fecha TEXT,
                equipo_norm TEXT NOT NULL,
                equipo TEXT NOT NULL,
                rival TEXT,
                condicion TEXT,
                local TEXT,
                visitante TEXT,
                no_perder_pct REAL DEFAULT 0,
                prob_victoria_pct REAL DEFAULT 0,
                espn_event_id TEXT,
                match_key TEXT,
                kickoff_utc TEXT,
                probability_snapshot TEXT,
                model_version TEXT,
                decision_reason TEXT,
                selected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                estado TEXT NOT NULL DEFAULT 'recomendado',
                resultado TEXT,
                marcador_real TEXT,
                origen TEXT NOT NULL DEFAULT 'modelo',
                confirmado_at TIMESTAMP,
                bloqueado_at TIMESTAMP,
                resuelto_at TIMESTAMP,
                cancelled_at TIMESTAMP,
                PRIMARY KEY (temporada, jornada)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS telegram_updates (
                update_id BIGINT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'procesando',
                locked_until TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS telegram_deliveries (
                idempotency_key TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'procesando',
                locked_until TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP
            )
        """)
        _asegurar_columnas_survivor(cur)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_survivor_picks_match_activo
            ON survivor_picks (match_key)
            WHERE match_key IS NOT NULL AND match_key <> '' AND estado <> 'cancelado'
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_survivor_picks_equipo_activo
            ON survivor_picks (temporada, equipo_norm)
            WHERE estado IN ('confirmado', 'bloqueado', 'resuelto')
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS survivor_equipos_usados (
                temporada TEXT NOT NULL,
                equipo_norm TEXT NOT NULL,
                equipo TEXT NOT NULL,
                jornada INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (temporada, equipo_norm),
                UNIQUE (temporada, jornada)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_migrations (
                nombre TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pronosticos_historial (
                clave TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fecha TEXT,
                local TEXT,
                visitante TEXT,
                pick_1x2 TEXT,
                prob_local REAL,
                prob_empate REAL,
                prob_visitante REAL,
                marcador_predicho TEXT,
                marcador_real TEXT,
                resultado_real TEXT,
                acierto_1x2 INTEGER,
                acierto_marcador INTEGER,
                resuelto INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS aprendizajes_partidos (
                clave TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fecha TEXT NOT NULL,
                local TEXT NOT NULL,
                visitante TEXT NOT NULL,
                home_goals INTEGER NOT NULL,
                away_goals INTEGER NOT NULL,
                tipo TEXT NOT NULL,
                ganador TEXT,
                perdedor TEXT,
                favorito TEXT,
                prob_ganador REAL,
                prob_favorito REAL,
                fuente_previa TEXT,
                etiquetas TEXT,
                resumen_interno TEXT,
                evidencia TEXT
            )
        """)
        _migrar_y_sembrar_survivor(cur)
        conn.commit()


def _asegurar_columnas_survivor(cur: Any) -> None:
    """Migración aditiva e idempotente para SQLite y PostgreSQL/Neon."""
    columnas = {
        "espn_event_id": "TEXT",
        "match_key": "TEXT",
        "kickoff_utc": "TEXT",
        "probability_snapshot": "TEXT",
        "model_version": "TEXT",
        "decision_reason": "TEXT",
        "selected_at": "TIMESTAMP",
        "cancelled_at": "TIMESTAMP",
    }
    if USE_POSTGRES:
        for nombre, definicion in columnas.items():
            cur.execute(f"ALTER TABLE survivor_picks ADD COLUMN IF NOT EXISTS {nombre} {definicion}")
        cur.execute("UPDATE survivor_picks SET selected_at=created_at WHERE selected_at IS NULL")
        return
    cur.execute("PRAGMA table_info(survivor_picks)")
    existentes = {str(fila[1]) for fila in cur.fetchall()}
    for nombre, definicion in columnas.items():
        if nombre not in existentes:
            cur.execute(f"ALTER TABLE survivor_picks ADD COLUMN {nombre} {definicion}")
    cur.execute("UPDATE survivor_picks SET selected_at=created_at WHERE selected_at IS NULL")


def _snapshot_json(snapshot: Optional[Dict[str, Any]]) -> Optional[str]:
    if snapshot is None:
        return None
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot_decode(valor: Any) -> Optional[Dict[str, Any]]:
    if not valor:
        return None
    try:
        data = json.loads(str(valor))
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _reclamar_idempotencia(
    tabla: str,
    columna: str,
    valor: Any,
    completado: str,
    lease_seconds: int = 300,
) -> bool:
    """Adquiere una llave persistente; permite reintentos fallidos o leases vencidos."""
    if tabla not in {"telegram_updates", "telegram_deliveries"}:
        raise ValueError("Tabla de idempotencia inválida")
    ahora = datetime.now(timezone.utc)
    locked_until = ahora + timedelta(seconds=max(30, int(lease_seconds)))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO {tabla} ({columna}, status, locked_until) VALUES ({PH}, 'procesando', {PH}) "
            f"ON CONFLICT ({columna}) DO NOTHING",
            (valor, locked_until),
        )
        if cur.rowcount:
            conn.commit()
            return True
        cur.execute(f"SELECT status, locked_until FROM {tabla} WHERE {columna}={PH}", (valor,))
        fila = cur.fetchone()
        if not fila or str(fila[0]) == completado:
            conn.commit()
            return False
        estado = str(fila[0])
        lease_original = fila[1]
        lease = lease_original
        if isinstance(lease, str):
            try:
                lease = datetime.fromisoformat(lease.replace("Z", "+00:00"))
            except ValueError:
                lease = None
        if isinstance(lease, datetime) and lease.tzinfo is None:
            lease = lease.replace(tzinfo=timezone.utc)
        vencido = not isinstance(lease, datetime) or lease <= ahora
        if estado != "fallido" and not vencido:
            conn.commit()
            return False
        parametros: tuple[Any, ...]
        if estado == "fallido":
            condicion_reclamo = "status='fallido'"
            parametros = (locked_until, valor, completado)
        else:
            condicion_reclamo = f"locked_until={PH}"
            parametros = (locked_until, valor, completado, lease_original)
        cur.execute(
            f"UPDATE {tabla} SET status='procesando', locked_until={PH}, last_error=NULL, "
            f"updated_at=CURRENT_TIMESTAMP WHERE {columna}={PH} AND status<>{PH} "
            f"AND {condicion_reclamo}",
            parametros,
        )
        adquirido = bool(cur.rowcount)
        conn.commit()
        return adquirido


def _finalizar_idempotencia(tabla: str, columna: str, valor: Any, status: str, error: str = "") -> None:
    if tabla not in {"telegram_updates", "telegram_deliveries"}:
        raise ValueError("Tabla de idempotencia inválida")
    sent_sql = ", sent_at=CURRENT_TIMESTAMP" if tabla == "telegram_deliveries" and status == "enviado" else ""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {tabla} SET status={PH}, locked_until=NULL, last_error={PH}, "
            f"updated_at=CURRENT_TIMESTAMP{sent_sql} WHERE {columna}={PH}",
            (status, str(error or "")[:500] or None, valor),
        )
        conn.commit()


def reclamar_telegram_update(update_id: int, lease_seconds: int = 300) -> bool:
    return _reclamar_idempotencia("telegram_updates", "update_id", int(update_id), "procesado", lease_seconds)


def completar_telegram_update(update_id: int) -> None:
    _finalizar_idempotencia("telegram_updates", "update_id", int(update_id), "procesado")


def fallar_telegram_update(update_id: int, error: str = "") -> None:
    _finalizar_idempotencia("telegram_updates", "update_id", int(update_id), "fallido", error)


def reclamar_entrega_telegram(idempotency_key: str, lease_seconds: int = 300) -> bool:
    clave = str(idempotency_key or "").strip()
    if not clave:
        raise ValueError("La llave de idempotencia es obligatoria")
    return _reclamar_idempotencia("telegram_deliveries", "idempotency_key", clave, "enviado", lease_seconds)


def completar_entrega_telegram(idempotency_key: str) -> None:
    _finalizar_idempotencia("telegram_deliveries", "idempotency_key", str(idempotency_key), "enviado")


def fallar_entrega_telegram(idempotency_key: str, error: str = "") -> None:
    _finalizar_idempotencia("telegram_deliveries", "idempotency_key", str(idempotency_key), "fallido", error)


def _insertar_usado_cursor(cur: Any, temporada: str, equipo: str, jornada: Optional[int] = None) -> bool:
    """Inserta un usado dentro de la transacción actual; es idempotente."""
    norm = _survivor_equipo_key(equipo)
    if not norm:
        return False
    cur.execute(
        f"SELECT jornada FROM survivor_equipos_usados WHERE temporada={PH} AND equipo_norm={PH}",
        (temporada, norm),
    )
    existente = cur.fetchone()
    if existente is not None:
        if jornada is not None and existente[0] is None:
            cur.execute(
                f"UPDATE survivor_equipos_usados SET jornada={PH} WHERE temporada={PH} AND equipo_norm={PH}",
                (jornada, temporada, norm),
            )
        return False
    cur.execute(
        f"INSERT INTO survivor_equipos_usados (temporada, equipo_norm, equipo, jornada) "
        f"VALUES ({PH}, {PH}, {PH}, {PH})",
        (temporada, norm, str(equipo).strip(), jornada),
    )
    return True


def _insertar_pick_historico_cursor(
    cur: Any,
    temporada: str,
    jornada: int,
    equipo: str,
    rival: str,
    condicion: str,
) -> bool:
    """Siembra una victoria declarada por el dueño sin inventar marcador."""
    cur.execute(
        f"SELECT equipo_norm, estado, resultado FROM survivor_picks WHERE temporada={PH} AND jornada={PH}",
        (temporada, jornada),
    )
    existente = cur.fetchone()
    norm = _survivor_equipo_key(equipo)
    if existente is None:
        local, visitante = (equipo, rival) if condicion == "Local" else (rival, equipo)
        cur.execute(
            f"""INSERT INTO survivor_picks
                (temporada, jornada, equipo_norm, equipo, rival, condicion, local, visitante,
                 estado, resultado, origen, confirmado_at, bloqueado_at, resuelto_at)
                VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH},
                        'resuelto', 'gano', 'usuario_historico', CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (temporada, jornada, norm, equipo, rival, condicion, local, visitante),
        )
    elif existente != (norm, "resuelto", "gano"):
        logger.warning(
            "No se sobrescribió el pick histórico %s J%s porque ya existe otro registro",
            temporada,
            jornada,
        )
        return False
    _insertar_usado_cursor(cur, temporada, equipo, jornada)
    return True


def _migrar_y_sembrar_survivor(cur: Any) -> None:
    """Migra usados legacy y, en Render, aplica el historial confirmado por el dueño."""
    cur.execute(f"SELECT 1 FROM app_migrations WHERE nombre={PH}", (SURVIVOR_LEGACY_MIGRATION,))
    if not cur.fetchone():
        temporada = temporada_survivor_actual()
        cur.execute("SELECT equipo FROM survivor_usados ORDER BY created_at")
        for (equipo,) in cur.fetchall():
            _insertar_usado_cursor(cur, temporada, str(equipo))
        cur.execute(f"INSERT INTO app_migrations (nombre) VALUES ({PH})", (SURVIVOR_LEGACY_MIGRATION,))

    sembrar = bool(os.getenv("RENDER")) or os.getenv("SURVIVOR_SEED_APERTURA_2026", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if not sembrar:
        return
    cur.execute(f"SELECT 1 FROM app_migrations WHERE nombre={PH}", (SURVIVOR_SEED_MIGRATION,))
    if cur.fetchone():
        return

    temporada = "Apertura-2026"
    monterrey_ok = _insertar_pick_historico_cursor(cur, temporada, 1, "Monterrey", "Santos", "Local")
    cruz_azul_ok = _insertar_pick_historico_cursor(cur, temporada, 2, "Cruz Azul", "Puebla", "Local")
    if monterrey_ok and cruz_azul_ok:
        cur.execute(f"INSERT INTO app_migrations (nombre) VALUES ({PH})", (SURVIVOR_SEED_MIGRATION,))
    else:
        logger.error("El seed Survivor quedó pendiente por un conflicto con datos existentes")


def _norm_equipo(s: str) -> str:
    """Normaliza un nombre de equipo (minúsculas, sin acentos, espacios colapsados)."""
    base = unicodedata.normalize("NFKD", str(s or "")).lower()
    base = "".join(c for c in base if not unicodedata.combining(c))
    return " ".join(base.split())


def _survivor_equipo_key(equipo: str) -> str:
    """Identidad canónica compartida con el motor (Chivas=Guadalajara, etc.)."""
    from src.team_normalizer import canonical_team_key

    return canonical_team_key(equipo)


def _bloquear_operacion_survivor(cur: Any, temporada: str, jornada: int) -> None:
    """Serializa una transición de jornada en Postgres y SQLite."""
    clave = f"{temporada}:J{jornada}"
    if USE_POSTGRES:
        cur.execute(f"SELECT pg_advisory_xact_lock(hashtext({PH}))", (clave,))
    else:
        cur.execute("BEGIN IMMEDIATE")


def _partido_del_calendario(temporada: str, jornada: int, equipo: str) -> Optional[Dict[str, str]]:
    """Completa rival y localía desde el calendario versionado del torneo."""
    from src.planificador_survivor import cargar_calendario

    calendario = cargar_calendario()
    if not calendario:
        return None
    primera_fecha = str(calendario[0].get("fecha_inicio") or "")[:10]
    try:
        referencia = date.fromisoformat(primera_fecha)
    except ValueError:
        return None
    torneo_calendario = f"{'Apertura' if referencia.month >= 7 else 'Clausura'}-{referencia.year}"
    if temporada != torneo_calendario:
        return None
    equipo_key = _survivor_equipo_key(equipo)
    for bloque in calendario:
        try:
            misma_jornada = int(str(bloque.get("jornada"))) == jornada
        except (TypeError, ValueError):
            continue
        if not misma_jornada:
            continue
        for partido in bloque.get("partidos", []):
            local = str(partido.get("home_team") or "")
            visitante = str(partido.get("away_team") or "")
            if equipo_key == _survivor_equipo_key(local):
                return {
                    "local": local,
                    "visitante": visitante,
                    "rival": visitante,
                    "condicion": "Local",
                    "fecha": str(bloque.get("fecha_inicio") or "")[:10],
                }
            if equipo_key == _survivor_equipo_key(visitante):
                return {
                    "local": local,
                    "visitante": visitante,
                    "rival": local,
                    "condicion": "Visitante",
                    "fecha": str(bloque.get("fecha_inicio") or "")[:10],
                }
    return None


def add_equipo_usado(equipo: str, temporada: Optional[str] = None, jornada: Optional[int] = None) -> bool:
    """Marca un equipo como usado dentro de una temporada, sin mezclar torneos."""
    temporada = normalizar_temporada(temporada or temporada_survivor_actual())
    if jornada is not None and not 1 <= int(jornada) <= 17:
        raise ValueError("La jornada debe estar entre 1 y 17.")
    with get_db() as conn:
        agregado = _insertar_usado_cursor(cur=conn.cursor(), temporada=temporada, equipo=equipo, jornada=jornada)
        conn.commit()
        return agregado


def get_equipos_usados(temporada: Optional[str] = None) -> List[str]:
    """Lista usados manuales y picks cerrados; los picks son la fuente de verdad."""
    temporada = normalizar_temporada(temporada or temporada_survivor_actual())
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT equipo, equipo_norm FROM survivor_equipos_usados "
            f"WHERE temporada={PH} ORDER BY created_at, jornada",
            (temporada,),
        )
        usados: List[str] = []
        claves = set()
        for equipo, equipo_norm in cur.fetchall():
            if equipo_norm not in claves:
                usados.append(str(equipo))
                claves.add(str(equipo_norm))
        cur.execute(
            f"""SELECT equipo, equipo_norm FROM survivor_picks
                WHERE temporada={PH} AND estado IN ('confirmado', 'bloqueado', 'resuelto')
                ORDER BY jornada""",
            (temporada,),
        )
        for equipo, equipo_norm in cur.fetchall():
            if equipo_norm not in claves:
                usados.append(str(equipo))
                claves.add(str(equipo_norm))
        return usados


def remove_equipo_usado(equipo: str, temporada: Optional[str] = None) -> int:
    """Quita un marcador manual; nunca libera un pick confirmado o resuelto."""
    temporada = normalizar_temporada(temporada or temporada_survivor_actual())
    norm = _survivor_equipo_key(equipo)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT jornada FROM survivor_picks
                WHERE temporada={PH} AND equipo_norm={PH}
                  AND estado IN ('confirmado', 'bloqueado', 'resuelto')""",
            (temporada, norm),
        )
        protegido = cur.fetchone()
        if protegido:
            raise ValueError(
                f"{equipo} pertenece al pick de la jornada {protegido[0]}; corrige el pick antes de bloquearlo."
            )
        cur.execute(
            f"DELETE FROM survivor_equipos_usados WHERE temporada={PH} AND equipo_norm={PH}",
            (temporada, norm),
        )
        conn.commit()
        return cast(int, cur.rowcount)


def clear_equipos_usados(temporada: Optional[str] = None) -> int:
    """Limpia solo marcadores manuales; conserva usados ligados a picks reales."""
    temporada = normalizar_temporada(temporada or temporada_survivor_actual())
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""DELETE FROM survivor_equipos_usados
                WHERE temporada={PH} AND NOT EXISTS (
                    SELECT 1 FROM survivor_picks p
                    WHERE p.temporada=survivor_equipos_usados.temporada
                      AND p.equipo_norm=survivor_equipos_usados.equipo_norm
                      AND p.estado IN ('confirmado', 'bloqueado', 'resuelto')
                )""",
            (temporada,),
        )
        conn.commit()
        return cast(int, cur.rowcount)


# ---------------------------------------------------------------------------
# Ciclo de vida de "Mi Survivor" (temporada + jornada como fuente de verdad).
# ---------------------------------------------------------------------------
def _validar_jornada(jornada: int) -> int:
    try:
        numero = int(jornada)
    except (TypeError, ValueError) as exc:
        raise ValueError("La jornada debe ser un número entre 1 y 17.") from exc
    if not 1 <= numero <= 17:
        raise ValueError("La jornada debe estar entre 1 y 17.")
    return numero


def _pick_desde_cursor(cur: Any, temporada: str, jornada: int) -> Optional[Dict[str, Any]]:
    cur.execute(
        f"""SELECT temporada, jornada, fecha, equipo, rival, condicion, local, visitante,
                   no_perder_pct, prob_victoria_pct, espn_event_id, match_key, kickoff_utc,
                   probability_snapshot, model_version, decision_reason, selected_at,
                   estado, resultado, marcador_real, origen, created_at, updated_at,
                   confirmado_at, bloqueado_at, resuelto_at, cancelled_at
            FROM survivor_picks WHERE temporada={PH} AND jornada={PH}""",
        (temporada, jornada),
    )
    fila = cur.fetchone()
    if fila is None:
        return None
    columnas = [descripcion[0] for descripcion in cur.description]
    pick = dict(zip(columnas, fila))
    pick["probability_snapshot"] = _snapshot_decode(pick.get("probability_snapshot"))
    return pick


def get_survivor_pick(temporada: str, jornada: int) -> Optional[Dict[str, Any]]:
    """Obtiene la selección de una jornada concreta."""
    temporada = normalizar_temporada(temporada)
    jornada = _validar_jornada(jornada)
    with get_db() as conn:
        return _pick_desde_cursor(conn.cursor(), temporada, jornada)


def get_survivor_picks(temporada: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lista el historial completo de una temporada en orden de jornada."""
    temporada = normalizar_temporada(temporada or temporada_survivor_actual())
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT temporada, jornada, fecha, equipo, rival, condicion, local, visitante,
                       no_perder_pct, prob_victoria_pct, espn_event_id, match_key, kickoff_utc,
                       probability_snapshot, model_version, decision_reason, selected_at,
                       estado, resultado, marcador_real, origen, created_at, updated_at,
                       confirmado_at, bloqueado_at, resuelto_at, cancelled_at
                FROM survivor_picks WHERE temporada={PH} ORDER BY jornada""",
            (temporada,),
        )
        columnas = [descripcion[0] for descripcion in cur.description]
        picks = [dict(zip(columnas, fila)) for fila in cur.fetchall()]
        for pick in picks:
            pick["probability_snapshot"] = _snapshot_decode(pick.get("probability_snapshot"))
        return picks


def registrar_pick_recomendado(
    temporada: str,
    jornada: int,
    equipo: str,
    rival: str = "",
    condicion: str = "",
    local: str = "",
    visitante: str = "",
    no_perder_pct: float = 0.0,
    prob_victoria_pct: float = 0.0,
    fecha: str = "",
    espn_event_id: Optional[str] = None,
    match_key: str = "",
    kickoff_utc: str = "",
    probability_snapshot: Optional[Dict[str, Any]] = None,
    model_version: str = "",
    decision_reason: str = "",
) -> bool:
    """Crea o refresca una recomendación; nunca pisa una decisión confirmada."""
    temporada = normalizar_temporada(temporada)
    jornada = _validar_jornada(jornada)
    equipo = str(equipo or "").strip()
    if not equipo:
        raise ValueError("El equipo es obligatorio.")
    norm = _survivor_equipo_key(equipo)
    espn_id = str(espn_event_id).strip() if espn_event_id not in (None, "") else None
    match_key = str(match_key or "").strip()
    if not match_key and espn_id:
        match_key = f"espn:{espn_id}"
    snapshot_json = _snapshot_json(probability_snapshot)
    with get_db() as conn:
        cur = conn.cursor()
        _bloquear_operacion_survivor(cur, temporada, jornada)
        existente = _pick_desde_cursor(cur, temporada, jornada)
        if existente and existente["estado"] != "recomendado":
            conn.rollback()
            return False
        valores = (
            str(fecha or "")[:10],
            norm,
            equipo,
            str(rival or ""),
            str(condicion or ""),
            str(local or ""),
            str(visitante or ""),
            float(no_perder_pct or 0),
            float(prob_victoria_pct or 0),
            espn_id,
            match_key or None,
            str(kickoff_utc or ""),
            snapshot_json,
            str(model_version or ""),
            str(decision_reason or ""),
        )
        if existente:
            cur.execute(
                f"""UPDATE survivor_picks SET fecha={PH}, equipo_norm={PH}, equipo={PH}, rival={PH},
                    condicion={PH}, local={PH}, visitante={PH}, no_perder_pct={PH},
                    prob_victoria_pct={PH}, espn_event_id={PH}, match_key={PH}, kickoff_utc={PH},
                    probability_snapshot={PH}, model_version={PH}, decision_reason={PH},
                    updated_at=CURRENT_TIMESTAMP
                    WHERE temporada={PH} AND jornada={PH} AND estado='recomendado'""",
                (*valores, temporada, jornada),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False
        else:
            cur.execute(
                f"""INSERT INTO survivor_picks
                    (temporada, jornada, fecha, equipo_norm, equipo, rival, condicion, local,
                     visitante, no_perder_pct, prob_victoria_pct, espn_event_id, match_key,
                     kickoff_utc, probability_snapshot, model_version, decision_reason, estado, origen)
                    VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH},
                            {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, 'recomendado', 'modelo')""",
                (temporada, jornada, *valores),
            )
        conn.commit()
        return True


def confirmar_survivor_pick(
    temporada: str,
    jornada: int,
    equipo: str,
    rival: str = "",
    condicion: str = "",
    local: str = "",
    visitante: str = "",
    fecha: str = "",
    origen: str = "usuario",
) -> Dict[str, Any]:
    """Confirma o corrige una selección no bloqueada y sincroniza usados."""
    temporada = normalizar_temporada(temporada)
    jornada = _validar_jornada(jornada)
    equipo = str(equipo or "").strip()
    if not equipo:
        raise ValueError("El equipo es obligatorio.")
    norm = _survivor_equipo_key(equipo)
    with get_db() as conn:
        cur = conn.cursor()
        _bloquear_operacion_survivor(cur, temporada, jornada)
        existente = _pick_desde_cursor(cur, temporada, jornada)
        estado = str(existente.get("estado") or "") if existente else ""
        norm_anterior = _survivor_equipo_key(str(existente.get("equipo") or "")) if existente else ""
        mismo_equipo = norm_anterior == norm

        if existente and estado == "cancelado":
            raise ValueError(f"La jornada {jornada} fue cancelada y no puede confirmarse.")
        if existente and estado in {"bloqueado", "resuelto"}:
            if not mismo_equipo:
                raise ValueError(f"La jornada {jornada} ya está {estado} con {existente['equipo']}.")
            _insertar_usado_cursor(cur, temporada, equipo, jornada)
            conn.commit()
            return existente
        if existente and estado == "confirmado" and mismo_equipo:
            _insertar_usado_cursor(cur, temporada, equipo, jornada)
            conn.commit()
            return existente

        partido_calendario = _partido_del_calendario(temporada, jornada, equipo)
        if partido_calendario:
            local = partido_calendario["local"]
            visitante = partido_calendario["visitante"]
            rival = partido_calendario["rival"]
            condicion = partido_calendario["condicion"]
            fecha = fecha or partido_calendario["fecha"]
        elif mismo_equipo and existente and existente.get("local") and existente.get("visitante"):
            local = local or str(existente["local"])
            visitante = visitante or str(existente["visitante"])
            rival = rival or str(existente.get("rival") or "")
            condicion = condicion or str(existente.get("condicion") or "")
            fecha = fecha or str(existente.get("fecha") or "")
        else:
            local_key = _survivor_equipo_key(local)
            visitante_key = _survivor_equipo_key(visitante)
            if not local or not visitante or norm not in {local_key, visitante_key}:
                raise ValueError(
                    "No se encontró al equipo en el calendario de esa jornada; indica local y visitante válidos."
                )
            es_local = norm == local_key
            rival = rival or (visitante if es_local else local)
            condicion = condicion or ("Local" if es_local else "Visitante")

        cur.execute(
            f"""SELECT jornada FROM survivor_picks
                WHERE temporada={PH} AND equipo_norm={PH}
                  AND estado IN ('confirmado', 'bloqueado', 'resuelto') AND jornada<>{PH}""",
            (temporada, norm, jornada),
        )
        repetido = cur.fetchone()
        if repetido:
            raise ValueError(f"{equipo} ya fue utilizado en la jornada {repetido[0]}.")
        cur.execute(
            f"SELECT jornada FROM survivor_equipos_usados WHERE temporada={PH} AND equipo_norm={PH}",
            (temporada, norm),
        )
        equipo_usado = cur.fetchone()
        if equipo_usado:
            if equipo_usado[0] is None:
                raise ValueError(f"{equipo} ya está marcado como usado en {temporada}.")
            if int(equipo_usado[0]) != jornada:
                raise ValueError(f"{equipo} ya fue utilizado en la jornada {equipo_usado[0]}.")

        cur.execute(
            f"SELECT equipo_norm, equipo FROM survivor_equipos_usados WHERE temporada={PH} AND jornada={PH}",
            (temporada, jornada),
        )
        usado_jornada = cur.fetchone()
        correccion = existente is not None and estado == "confirmado" and not mismo_equipo
        if usado_jornada and usado_jornada[0] != norm and not (correccion and usado_jornada[0] == norm_anterior):
            raise ValueError(f"La jornada {jornada} ya tiene registrado a {usado_jornada[1]}.")

        if correccion:
            cur.execute(
                f"DELETE FROM survivor_equipos_usados WHERE temporada={PH} AND jornada={PH} AND equipo_norm={PH}",
                (temporada, jornada, norm_anterior),
            )

        if existente:
            if mismo_equipo:
                rival = rival or str(existente.get("rival") or "")
                condicion = condicion or str(existente.get("condicion") or "")
                local = local or str(existente.get("local") or "")
                visitante = visitante or str(existente.get("visitante") or "")
                fecha = fecha or str(existente.get("fecha") or "")
            cur.execute(
                f"""UPDATE survivor_picks SET fecha={PH}, equipo_norm={PH}, equipo={PH}, rival={PH},
                    condicion={PH}, local={PH}, visitante={PH}, estado='confirmado', origen={PH},
                    confirmado_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE temporada={PH} AND jornada={PH}
                      AND estado IN ('recomendado', 'confirmado')""",
                (fecha[:10], norm, equipo, rival, condicion, local, visitante, origen, temporada, jornada),
            )
            if cur.rowcount != 1:
                raise ValueError("El pick cambió mientras se intentaba confirmar; vuelve a consultar.")
        else:
            cur.execute(
                f"""INSERT INTO survivor_picks
                    (temporada, jornada, fecha, equipo_norm, equipo, rival, condicion, local,
                     visitante, estado, origen, confirmado_at)
                    VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH},
                            'confirmado', {PH}, CURRENT_TIMESTAMP)""",
                (temporada, jornada, fecha[:10], norm, equipo, rival, condicion, local, visitante, origen),
            )
        _insertar_usado_cursor(cur, temporada, equipo, jornada)
        conn.commit()
        pick = _pick_desde_cursor(cur, temporada, jornada)
        if pick is None:  # pragma: no cover - defensa ante un backend inconsistente
            raise RuntimeError("No se pudo recuperar el pick confirmado.")
        return pick


def cancelar_survivor_pick(temporada: str, jornada: int) -> Dict[str, Any]:
    """Cancela de forma idempotente un pick aún no bloqueado y libera el equipo."""
    temporada = normalizar_temporada(temporada)
    jornada = _validar_jornada(jornada)
    with get_db() as conn:
        cur = conn.cursor()
        _bloquear_operacion_survivor(cur, temporada, jornada)
        pick = _pick_desde_cursor(cur, temporada, jornada)
        if pick is None:
            raise ValueError("No existe un pick para esa jornada.")
        if pick["estado"] == "cancelado":
            conn.commit()
            return pick
        if pick["estado"] in {"bloqueado", "resuelto"}:
            raise ValueError(f"Un pick {pick['estado']} no puede cancelarse.")
        cur.execute(
            f"""UPDATE survivor_picks SET estado='cancelado', cancelled_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP WHERE temporada={PH} AND jornada={PH}
                AND estado IN ('recomendado', 'confirmado')""",
            (temporada, jornada),
        )
        cur.execute(
            f"DELETE FROM survivor_equipos_usados WHERE temporada={PH} AND jornada={PH}",
            (temporada, jornada),
        )
        conn.commit()
        actualizado = _pick_desde_cursor(cur, temporada, jornada)
        if actualizado is None:
            raise RuntimeError("No se pudo recuperar el pick cancelado.")
        return actualizado


def bloquear_survivor_pick(temporada: str, jornada: int) -> Dict[str, Any]:
    """Bloquea el pick confirmado para impedir cambios accidentales antes del juego."""
    temporada = normalizar_temporada(temporada)
    jornada = _validar_jornada(jornada)
    with get_db() as conn:
        cur = conn.cursor()
        _bloquear_operacion_survivor(cur, temporada, jornada)
        pick = _pick_desde_cursor(cur, temporada, jornada)
        if pick is None:
            raise ValueError("No existe un pick para esa jornada.")
        if pick["estado"] in {"recomendado", "cancelado"}:
            raise ValueError("Primero debes confirmar un pick activo.")
        if pick["estado"] == "resuelto":
            raise ValueError("El pick ya está resuelto y no puede bloquearse.")
        if pick["estado"] == "confirmado":
            cur.execute(
                f"""UPDATE survivor_picks SET estado='bloqueado', bloqueado_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP WHERE temporada={PH} AND jornada={PH}""",
                (temporada, jornada),
            )
        conn.commit()
        actualizado = _pick_desde_cursor(cur, temporada, jornada)
        if actualizado is None:  # pragma: no cover
            raise RuntimeError("No se pudo recuperar el pick bloqueado.")
        return actualizado


def resolver_survivor_pick(
    temporada: str,
    jornada: int,
    resultado: str,
    marcador_real: str = "",
) -> Dict[str, Any]:
    """Resuelve un pick bloqueado como ganó, empató o perdió."""
    temporada = normalizar_temporada(temporada)
    jornada = _validar_jornada(jornada)
    resultado = _norm_equipo(resultado).replace("ó", "o")
    aliases = {"ganó": "gano", "ganador": "gano", "victoria": "gano", "perdió": "perdio", "derrota": "perdio"}
    resultado = aliases.get(str(resultado).lower(), resultado)
    if resultado not in SURVIVOR_RESULTADOS:
        raise ValueError("Resultado inválido. Usa gano, empate o perdio.")
    with get_db() as conn:
        cur = conn.cursor()
        _bloquear_operacion_survivor(cur, temporada, jornada)
        pick = _pick_desde_cursor(cur, temporada, jornada)
        if pick is None:
            raise ValueError("No existe un pick confirmado para esa jornada.")
        if pick["estado"] in {"recomendado", "cancelado"}:
            raise ValueError("No se puede resolver un pick no confirmado o cancelado.")
        if pick["estado"] == "resuelto":
            if pick["resultado"] != resultado:
                raise ValueError(f"La jornada {jornada} ya fue resuelta como {pick['resultado']}.")
            conn.commit()
            return pick
        if pick["estado"] != "bloqueado":
            raise ValueError("Debes bloquear el pick antes de resolverlo.")
        cur.execute(
            f"""UPDATE survivor_picks SET estado='resuelto', resultado={PH}, marcador_real={PH},
                resuelto_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE temporada={PH} AND jornada={PH}""",
            (resultado, str(marcador_real or "").strip(), temporada, jornada),
        )
        conn.commit()
        actualizado = _pick_desde_cursor(cur, temporada, jornada)
        if actualizado is None:  # pragma: no cover
            raise RuntimeError("No se pudo recuperar el pick resuelto.")
        return actualizado


def resumen_mi_survivor(temporada: Optional[str] = None) -> Dict[str, Any]:
    """Vista oficial del estado de una temporada, con reglas Survivor aplicadas."""
    from src.survivor_reglas import evaluar_temporada

    temporada = normalizar_temporada(temporada or temporada_survivor_actual())
    picks = get_survivor_picks(temporada)
    usados = get_equipos_usados(temporada)
    estado_oficial = evaluar_temporada(picks)
    recomendacion = next((pick for pick in picks if pick["estado"] == "recomendado"), None)
    pick_actual = estado_oficial["picks_pendientes"][0] if estado_oficial["picks_pendientes"] else recomendacion
    return {
        "temporada": temporada,
        "usados": usados,
        "pick_actual": pick_actual,
        "picks": picks,
        **estado_oficial,
    }


# ---------------------------------------------------------------------------
# Historial de pronósticos (track-record del modelo: aciertos 1X2 y marcador).
# ---------------------------------------------------------------------------
def _clave_pronostico(local: str, visitante: str, fecha: str) -> str:
    return f"{_norm_equipo(local)}|{_norm_equipo(visitante)}|{str(fecha or '')[:10]}"


def registrar_pronostico(
    local: str,
    visitante: str,
    pick_1x2: str,
    prob_local: float,
    prob_empate: float,
    prob_visitante: float,
    marcador_predicho: str,
    fecha: str = "",
) -> bool:
    """Guarda un pronóstico si no existe (dedup por equipos+fecha). True si se insertó."""
    clave = _clave_pronostico(local, visitante, fecha)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT 1 FROM pronosticos_historial WHERE clave = {PH}", (clave,))
        if cur.fetchone():
            return False
        cur.execute(
            f"""INSERT INTO pronosticos_historial
                (clave, fecha, local, visitante, pick_1x2, prob_local, prob_empate,
                 prob_visitante, marcador_predicho)
                VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH})""",
            (
                clave,
                str(fecha or "")[:10],
                str(local),
                str(visitante),
                str(pick_1x2),
                float(prob_local or 0),
                float(prob_empate or 0),
                float(prob_visitante or 0),
                str(marcador_predicho or ""),
            ),
        )
        conn.commit()
        return True


def historial_pronosticos(limit: int = 50, offset: int = 0, solo_resueltos: bool = False) -> List[Dict[str, Any]]:
    """Historial de pronósticos (más recientes primero) como lista de dicts."""
    with get_db() as conn:
        cur = conn.cursor()
        filtro = "WHERE resuelto = 1" if solo_resueltos else ""
        cur.execute(
            f"SELECT * FROM pronosticos_historial {filtro} ORDER BY created_at DESC, clave LIMIT {PH} OFFSET {PH}",
            (limit, offset),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, fila)) for fila in cur.fetchall()]


def _clave_aprendizaje(local: str, visitante: str, fecha: str) -> str:
    return f"{_survivor_equipo_key(local)}|{_survivor_equipo_key(visitante)}|{str(fecha or '')[:10]}"


def guardar_aprendizaje_partido(aprendizaje: Dict[str, Any]) -> bool:
    """Persiste una lectura estructurada del resultado para que el plan la reutilice."""
    local = str(aprendizaje.get("local") or "").strip()
    visitante = str(aprendizaje.get("visitante") or "").strip()
    fecha = str(aprendizaje.get("fecha") or "")[:10]
    if not local or not visitante or not fecha:
        return False
    home_goals_raw = aprendizaje.get("home_goals")
    away_goals_raw = aprendizaje.get("away_goals")
    if home_goals_raw is None or away_goals_raw is None:
        return False
    try:
        home_goals = int(home_goals_raw)
        away_goals = int(away_goals_raw)
    except (TypeError, ValueError):
        return False
    clave = _clave_aprendizaje(local, visitante, fecha)
    valores = (
        clave,
        fecha,
        local,
        visitante,
        home_goals,
        away_goals,
        str(aprendizaje.get("tipo") or "RESULTADO_OBSERVADO"),
        str(aprendizaje.get("ganador") or "") or None,
        str(aprendizaje.get("perdedor") or "") or None,
        str(aprendizaje.get("favorito") or "") or None,
        aprendizaje.get("prob_ganador"),
        aprendizaje.get("prob_favorito"),
        str(aprendizaje.get("fuente_previa") or "") or None,
        json.dumps(aprendizaje.get("etiquetas") or [], ensure_ascii=False),
        str(aprendizaje.get("resumen_interno") or ""),
        json.dumps(aprendizaje.get("evidencia") or {}, ensure_ascii=False, sort_keys=True),
    )
    with get_db() as conn:
        cur = conn.cursor()
        columnas_comparables = (
            "fecha, local, visitante, home_goals, away_goals, tipo, ganador, perdedor, favorito, "
            "prob_ganador, prob_favorito, fuente_previa, etiquetas, resumen_interno, evidencia"
        )
        cur.execute(
            f"SELECT {columnas_comparables} FROM aprendizajes_partidos WHERE clave = {PH}",
            (clave,),
        )
        existente = cur.fetchone()
        if existente is not None and tuple(existente) == valores[1:]:
            return False
        marcadores = ", ".join([PH] * len(valores))
        cur.execute(
            f"""INSERT INTO aprendizajes_partidos
                (clave, fecha, local, visitante, home_goals, away_goals, tipo,
                 ganador, perdedor, favorito, prob_ganador, prob_favorito,
                 fuente_previa, etiquetas, resumen_interno, evidencia)
                VALUES ({marcadores})
                ON CONFLICT (clave) DO UPDATE SET
                fecha=excluded.fecha, local=excluded.local, visitante=excluded.visitante,
                home_goals=excluded.home_goals, away_goals=excluded.away_goals,
                tipo=excluded.tipo, ganador=excluded.ganador, perdedor=excluded.perdedor,
                favorito=excluded.favorito, prob_ganador=excluded.prob_ganador,
                prob_favorito=excluded.prob_favorito, fuente_previa=excluded.fuente_previa,
                etiquetas=excluded.etiquetas, resumen_interno=excluded.resumen_interno,
                evidencia=excluded.evidencia, updated_at=CURRENT_TIMESTAMP""",
            valores,
        )
        conn.commit()
    return True


def aprendizajes_partidos(limit: int = 200, desde: Optional[str] = None) -> List[Dict[str, Any]]:
    """Devuelve aprendizajes recientes, incluida la evidencia previa que los sustenta."""
    with get_db() as conn:
        cur = conn.cursor()
        if desde:
            cur.execute(
                f"SELECT * FROM aprendizajes_partidos WHERE fecha >= {PH} "
                f"ORDER BY fecha DESC, updated_at DESC LIMIT {PH}",
                (str(desde)[:10], max(1, int(limit))),
            )
        else:
            cur.execute(
                f"SELECT * FROM aprendizajes_partidos ORDER BY fecha DESC, updated_at DESC LIMIT {PH}",
                (max(1, int(limit)),),
            )
        cols = [d[0] for d in cur.description]
        salida = [dict(zip(cols, fila)) for fila in cur.fetchall()]
    for item in salida:
        try:
            etiquetas = json.loads(str(item.get("etiquetas") or "[]"))
        except (TypeError, ValueError):
            etiquetas = []
        item["etiquetas"] = etiquetas if isinstance(etiquetas, list) else []
        try:
            evidencia = json.loads(str(item.get("evidencia") or "{}"))
        except (TypeError, ValueError):
            evidencia = {}
        item["evidencia"] = evidencia if isinstance(evidencia, dict) else {}
    return salida


def settle_pronosticos(resultados: List[Dict[str, Any]]) -> int:
    """
    Resuelve pronósticos pendientes con resultados reales. `resultados`: lista de
    {home_team, away_team, home_goals, away_goals, fecha}. Rellena marcador real,
    resultado (1/X/2), y aciertos (1X2 y marcador exacto). Devuelve # resueltos.
    """
    por_clave: Dict[str, Any] = {}
    por_equipos: Dict[str, Any] = {}
    for r in resultados:
        try:
            hg, ag = int(r.get("home_goals", 0)), int(r.get("away_goals", 0))
        except (TypeError, ValueError):
            continue
        info: Optional[Dict[str, Any]] = {
            "hg": hg,
            "ag": ag,
            "home": r.get("home_team", ""),
            "away": r.get("away_team", ""),
        }
        por_clave[_clave_pronostico(r.get("home_team", ""), r.get("away_team", ""), r.get("fecha", ""))] = info
        por_equipos.setdefault(f"{_norm_equipo(r.get('home_team', ''))}|{_norm_equipo(r.get('away_team', ''))}", info)
    settled = 0
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT clave, local, visitante, pick_1x2, marcador_predicho FROM pronosticos_historial WHERE resuelto = 0"
        )
        pendientes = cur.fetchall()
        for clave, local, visitante, pick_1x2, marcador_pred in pendientes:
            info = por_clave.get(clave)
            if info is None:
                info = por_equipos.get(f"{_norm_equipo(local)}|{_norm_equipo(visitante)}")
            if info is None:
                continue
            hg, ag = info["hg"], info["ag"]
            res = "1" if hg > ag else ("2" if ag > hg else "X")
            pick_map = {"gana local": "1", "empate": "X", "gana visitante": "2"}
            pick_norm = pick_map.get(str(pick_1x2 or "").strip().lower())
            acierto_1x2 = 1 if pick_norm == res else 0
            marcador_real = f"{hg}-{ag}"
            acierto_marcador = 1 if str(marcador_pred or "").strip() == marcador_real else 0
            cur.execute(
                f"""UPDATE pronosticos_historial SET marcador_real={PH}, resultado_real={PH},
                    acierto_1x2={PH}, acierto_marcador={PH}, resuelto=1 WHERE clave={PH}""",
                (marcador_real, res, acierto_1x2, acierto_marcador, clave),
            )
            settled += 1
        conn.commit()
    return settled


def rentabilidad_pronosticos() -> Dict[str, Any]:
    """Track-record: aciertos 1X2 y de marcador exacto sobre los pronósticos resueltos."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(acierto_1x2),0), COALESCE(SUM(acierto_marcador),0)
            FROM pronosticos_historial WHERE resuelto = 1
        """)
        row = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM pronosticos_historial WHERE resuelto = 0")
        pend = cur.fetchone()[0] or 0
    n = row[0] or 0
    a1x2 = row[1] or 0
    amarc = row[2] or 0
    return {
        "resueltos": n,
        "pendientes": pend,
        "aciertos_1x2": a1x2,
        "acierto_1x2_pct": round(100.0 * a1x2 / n, 1) if n else None,
        "aciertos_marcador_exacto": amarc,
        "acierto_marcador_pct": round(100.0 * amarc / n, 1) if n else None,
    }


# ---------------------------------------------------------------------------
# Historial del PICK DE SURVIVOR (racha real: sobrevive / gana / cae por jornada)
# ---------------------------------------------------------------------------
def registrar_survivor_pick(
    jornada: str,
    equipo: str,
    rival: str,
    condicion: str,
    local: str,
    visitante: str,
    no_perder_pct: float,
    prob_victoria_pct: float,
    fecha: str = "",
) -> bool:
    """
    Registra (o actualiza si aún está pendiente) el pick de Survivor de una
    jornada. Una fila por jornada. Si ya está RESUELTO, no se sobreescribe.
    Devuelve True si insertó/actualizó.
    """
    if not jornada or not equipo:
        return False
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT resuelto FROM survivor_historial WHERE jornada = {PH}", (str(jornada),))
        row = cur.fetchone()
        if row is not None:
            if row[0]:  # ya resuelto: bloqueado
                return False
            cur.execute(
                f"""UPDATE survivor_historial SET fecha={PH}, equipo={PH}, rival={PH},
                    condicion={PH}, local={PH}, visitante={PH}, no_perder_pct={PH},
                    prob_victoria_pct={PH} WHERE jornada={PH}""",
                (
                    str(fecha or "")[:10],
                    str(equipo),
                    str(rival or ""),
                    str(condicion or ""),
                    str(local or ""),
                    str(visitante or ""),
                    float(no_perder_pct or 0),
                    float(prob_victoria_pct or 0),
                    str(jornada),
                ),
            )
        else:
            cur.execute(
                f"""INSERT INTO survivor_historial
                    (jornada, fecha, equipo, rival, condicion, local, visitante,
                     no_perder_pct, prob_victoria_pct)
                    VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH})""",
                (
                    str(jornada),
                    str(fecha or "")[:10],
                    str(equipo),
                    str(rival or ""),
                    str(condicion or ""),
                    str(local or ""),
                    str(visitante or ""),
                    float(no_perder_pct or 0),
                    float(prob_victoria_pct or 0),
                ),
            )
        conn.commit()
        return True


def settle_survivor(resultados: List[Dict[str, Any]]) -> int:
    """Resuelve automáticamente tanto el ciclo nuevo como el historial legado."""
    por_match_key: Dict[str, Any] = {}
    por_equipos: Dict[str, Any] = {}
    por_equipos_canon: Dict[str, Any] = {}
    por_clave_canon: Dict[str, Any] = {}
    for resultado in resultados:
        try:
            hg = int(resultado.get("home_goals", 0))
            ag = int(resultado.get("away_goals", 0))
        except (TypeError, ValueError):
            continue
        local = str(resultado.get("home_team", ""))
        visitante = str(resultado.get("away_team", ""))
        fecha = str(resultado.get("fecha", ""))[:10]
        info = {"hg": hg, "ag": ag}
        match_key_resultado = str(resultado.get("match_key") or "").strip()
        if not match_key_resultado and resultado.get("espn_event_id") not in (None, ""):
            match_key_resultado = f"espn:{str(resultado['espn_event_id']).strip()}"
        if match_key_resultado:
            por_match_key[match_key_resultado] = info
        clave_legacy = f"{_norm_equipo(local)}|{_norm_equipo(visitante)}"
        clave_canon = f"{_survivor_equipo_key(local)}|{_survivor_equipo_key(visitante)}"
        por_equipos.setdefault(clave_legacy, info)
        por_equipos_canon.setdefault(clave_canon, info)
        if fecha:
            por_clave_canon[f"{clave_canon}|{fecha}"] = info

    legacy_resueltos = 0
    nuevos_resueltos = 0
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT jornada, condicion, local, visitante FROM survivor_historial WHERE resuelto = 0")
        for jornada, condicion, local, visitante in cur.fetchall():
            info_legacy: Optional[Dict[str, Any]] = por_equipos.get(
                f"{_norm_equipo(str(local))}|{_norm_equipo(str(visitante))}"
            )
            if info_legacy is None:
                continue
            hg, ag = info_legacy["hg"], info_legacy["ag"]
            es_local = str(condicion or "").strip().lower() == "local"
            gf, gc = (hg, ag) if es_local else (ag, hg)
            estado = "gano" if gf > gc else ("empate" if gf == gc else "perdio")
            cur.execute(
                f"""UPDATE survivor_historial SET marcador_real={PH}, estado={PH}, resuelto=1
                    WHERE jornada={PH} AND resuelto=0""",
                (f"{hg}-{ag}", estado, jornada),
            )
            legacy_resueltos += int(cur.rowcount)

        cur.execute(
            """SELECT temporada, jornada, fecha, equipo, condicion, local, visitante, match_key
               FROM survivor_picks WHERE estado='bloqueado'"""
        )
        for temporada, jornada, fecha, equipo, condicion, local, visitante, match_key in cur.fetchall():
            if not local or not visitante:
                continue
            clave = f"{_survivor_equipo_key(str(local))}|{_survivor_equipo_key(str(visitante))}"
            fecha_pick = str(fecha or "")[:10]
            info_nuevo: Optional[Dict[str, Any]] = por_match_key.get(str(match_key or ""))
            if info_nuevo is None:
                info_nuevo = por_clave_canon.get(f"{clave}|{fecha_pick}") if fecha_pick else None
            info_nuevo = info_nuevo or por_equipos_canon.get(clave)
            if info_nuevo is None:
                continue
            hg, ag = info_nuevo["hg"], info_nuevo["ag"]
            es_local = str(condicion or "").strip().lower() == "local" or (
                _survivor_equipo_key(str(equipo)) == _survivor_equipo_key(str(local))
            )
            gf, gc = (hg, ag) if es_local else (ag, hg)
            resultado_pick = "gano" if gf > gc else ("empate" if gf == gc else "perdio")
            cur.execute(
                f"""UPDATE survivor_picks SET estado='resuelto', resultado={PH}, marcador_real={PH},
                    resuelto_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE temporada={PH} AND jornada={PH} AND estado='bloqueado'""",
                (resultado_pick, f"{hg}-{ag}", temporada, jornada),
            )
            nuevos_resueltos += int(cur.rowcount)
        conn.commit()
    return nuevos_resueltos + legacy_resueltos


def resumen_survivor() -> Dict[str, Any]:
    """
    Track-record del Survivor: jornadas jugadas, sobrevividas (gana+empata),
    victorias, empates, si sigue VIVO y en qué jornada cayó. Cronológico por fecha.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT jornada, fecha, equipo, rival, condicion, marcador_real, estado "
            "FROM survivor_historial WHERE resuelto = 1 ORDER BY fecha, jornada"
        )
        filas = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM survivor_historial WHERE resuelto = 0")
        pendientes = cur.fetchone()[0] or 0

    jugadas = len(filas)
    victorias = empates = sobrevividas = 0
    eliminado_en = None
    racha = 0
    detalle = []
    vivo = True
    for jornada, fecha, equipo, rival, condicion, marcador, estado in filas:
        detalle.append(
            {
                "jornada": jornada,
                "fecha": fecha,
                "equipo": equipo,
                "rival": rival,
                "condicion": condicion,
                "marcador": marcador,
                "estado": estado,
            }
        )
        if estado == "perdio":
            if eliminado_en is None:
                eliminado_en = jornada
                vivo = False
        else:
            if estado == "gano":
                victorias += 1
            elif estado == "empate":
                empates += 1
            sobrevividas += 1
        if vivo:
            racha += 1
    return {
        "jugadas": jugadas,
        "pendientes": pendientes,
        "sobrevividas": sobrevividas,
        "victorias": victorias,
        "empates": empates,
        "eliminado_en": eliminado_en,
        "sigue_vivo": vivo,
        "racha": racha,
        "detalle": detalle,
    }


def get_survivor_picks_recientes(limit: int = 10) -> List[Dict[str, Any]]:
    """
    Picks de Survivor recientes (pendientes o resueltos) para comparar
    con resultados reales. Devuelve lista de dicts con:
    {jornada, equipo, rival, condicion, local, visitante, ...}
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT jornada, equipo, rival, condicion, local, visitante, "
            f"no_perder_pct, prob_victoria_pct, marcador_real, estado, resuelto "
            f"FROM survivor_historial ORDER BY fecha DESC LIMIT {PH}",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, fila)) for fila in cur.fetchall()]


def save_pick(match_id: str, market: str, true_prob: float, momio: float, ev: float, kelly_pct: float) -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""INSERT INTO picks (match_id, market, true_prob, momio, ev, kelly_pct)
                VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH})""",
            (match_id, market, true_prob, momio, ev, kelly_pct),
        )
        conn.commit()


def get_metrics() -> Dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) as total_picks,
                SUM(CASE WHEN result = 1 THEN 1 ELSE 0 END) as wins,
                SUM(profit_loss) as total_profit,
                AVG(profit_loss) as avg_profit
            FROM picks
            WHERE status = 'settled'
        """)
        row = cur.fetchone()
        total = row[0] or 0
        wins = row[1] or 0
        return {
            "total_picks": total,
            "wins": wins,
            "win_rate": (wins / total * 100) if total > 0 else 0,
            "total_profit": row[2] or 0.0,
            "avg_profit": row[3] or 0.0,
        }


def get_history(limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    """Historial paginado de picks (más recientes primero) como lista de dicts."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT * FROM picks ORDER BY id DESC LIMIT {PH} OFFSET {PH}",
            (limit, offset),
        )
        cols = [d[0] for d in cur.description]
        filas = [dict(zip(cols, fila)) for fila in cur.fetchall()]
        return filas


def settle_pick(pick_id: int, result: float = 0.0, profit_loss: float = 0.0) -> int:
    """Marca un pick como 'settled' con su resultado y P/L."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE picks SET status='settled', result={PH}, profit_loss={PH} WHERE id={PH}",
            (result, profit_loss, pick_id),
        )
        conn.commit()
        return int(cur.rowcount)
