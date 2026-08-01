# Survivor Liga MX Bot

Asistente **informativo** para decisiones de **Survivor Liga MX** y pronósticos
de partidos (1X2, Over/Under, BTTS, marcador). **No apuesta ni envía picks
automáticos**: toda salida es informativa y para revisión humana.

![Python](https://img.shields.io/badge/Python-3.12-blue)
![CI](https://github.com/CAILLOU77/survivor-ligamx-bot/workflows/CI%2FCD/badge.svg)
![Coverage](https://img.shields.io/badge/coverage-view_logs-green)

## Arquitectura (ESPN + Poisson)

Tras descartar las APIs de momios como fuente primaria (no cubrían bien Liga MX
o eran de pago caro), el sistema usa **datos públicos gratuitos + un modelo
estadístico**. Las predicciones salen de **resultados reales**, no de momios.

```
ESPN API (gratis, sin key) ─┐
TheSportsDB (gratis, respaldo) ─┤→ fuentes_datos (redundancia + caché)
                                 │
                                 ▼
                         poisson_model (Dixon-Coles)
                         fuerza de equipos: recencia + shrinkage
                                 │
ESPN fixtures ───────────────────┼──────────────┐
                                 ▼              ▼
                          motor_pronosticos   tabla_posiciones (motivación)
                                 │
              ┌──────────────────┼───────────────────────┐
              ▼                  ▼                        ▼
        /predicciones        /survivor                Telegram
        1X2·O/U·BTTS         pick "no perder"          (informativo)
                                 ▲
                  (opcional) comparador_mercado ── momios reales odds-api.io
```

- **Sin scraping, sin bypass.** Solo APIs públicas/gratuitas.
- **Redundancia:** ESPN → TheSportsDB → caché local.
- **Momios opcionales:** solo para *comparar* el modelo vs el mercado (capa
  apagada por defecto; ver más abajo).

## Endpoints de la web (FastAPI)

| Endpoint | Qué hace |
|---|---|
| `GET /predicciones` | 1X2 / Over-Under / BTTS / marcador por partido próximo |
| `GET /survivor?excluir=America,Toluca` | mejor equipo "no perder" (excluye usados) |
| `GET /jornada?excluir=` | todo-en-uno: predicciones + pick + top-3 + motivación + momios |
| `GET /plan-survivor?excluir=&peso_victoria=0.5&vida_empate_consumida=false` | **estrategia de temporada**: qué equipo usar en cada jornada con regla oficial de una sola vida de empate y probabilidad total no inflada (requiere `data/calendario.json`) |
| `GET /analisis/riesgo` | ¿cuándo falla el favorito? (análisis de upsets, datos reales) |
| `GET /analisis-partido?home=America&away=Toluca` | dossier de un partido (Liga MX API): predicción + forma + tarjetas + rachas + h2h |
| `GET /alineacion?home=&away=` | alineación confirmada del partido (365Scores, ~1h antes): detecta suplentes |
| `GET /noticias?limit=10` | noticias Liga MX (365Scores + Google): fichajes/lesiones/bajas |
| `GET /jugadores-riesgo` | jugadores en riesgo de suspensión por tarjetas |
| `GET /historial/pronosticos` · `GET /historial/rentabilidad` | track-record: marcador predicho vs real y % de aciertos (1X2 y marcador exacto) |
| `GET /survivor/usados` · `POST /survivor/usados?equipo=` · `POST /survivor/usados/reset` | equipos ya usados (se excluyen del pick; persisten en BD) |
| `POST /telegram/webhook` | comandos por Telegram (`/pick`, `/usado`, `/usados`, `/reset`, `/ayuda`) |
| `GET /tabla` | tabla de ESPN + **motivación** por equipo (zona, vivo/eliminado) |
| `GET /valor` | predicciones + comparación vs **mercado** (si hay momios) |
| `GET /valor/diagnostico` | diagnóstico de la conexión de momios (sin exponer la key) |
| `GET /health/fuentes` | salud de las fuentes (ESPN / TheSportsDB / odds) |
| `POST /alerts/pronosticos` | envía el resumen por Telegram (real) |
| `POST /alerts/plan` | envía el plan de temporada por Telegram |
| `POST /cron/backtest` | **validación real** del modelo vs ESPN (cron diario) |
| `GET /stats`, `GET /history`, `GET /dashboard`, `GET /health`, `GET /docs` | métricas, historial, dashboard, salud, OpenAPI |

## Instalación

Requiere **Python 3.12** (ver `runtime.txt`).

```bash
git clone https://github.com/CAILLOU77/survivor-ligamx-bot.git
cd survivor-ligamx-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # edita y rellena lo que uses (opcional)
python3 -m pytest tests/        # verificar (todos los tests deben pasar)
```

## Cómo correr (local)

```bash
# Pipeline real: baja resultados de ESPN y genera pronósticos + pick Survivor
bash run_bot.sh

# Solo el orquestador (con Telegram opcional y equipos ya usados):
python3 main.py
python3 main.py --telegram --excluir "America,Toluca"

# Pasos individuales:
python3 src/espn_data.py            # resultados reales -> data/resultados_historicos.json
python3 src/motor_pronosticos.py    # pronósticos + pick Survivor
python3 src/validacion_modelo.py    # ¿qué tan bueno es el modelo? (backtest honesto)
```

## Validación honesta del modelo

`src/validacion_modelo.py` mide el modelo contra resultados **reales** de ESPN
(entrena con lo antiguo, predice lo reciente):

- **accuracy** del pick 1X2 vs el baseline "siempre gana local".
- **Brier** (calibración de probabilidades).

El cron diario `POST /cron/backtest` corre exactamente esa validación (ya **no**
hay métricas inventadas). Referencia reciente: accuracy ~49% vs baseline ~45%.

## Telegram (opcional, informativo)

El resumen de pronósticos se envía con `src/telegram_pronosticos.py` (incluye el
pick de Survivor, 1X2/O-U/BTTS por partido y, si están disponibles, momios/valor
y la motivación del rival). Requiere `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.

```bash
python3 main.py --telegram          # genera y envía
# o vía la web (lo usa el workflow auto-alerts cada 6h):
#   POST /alerts/pronosticos
```

Sin credenciales, no envía nada (no-op). Nunca dice "apuesta ya".

## Capa de momios (opcional) — odds-api.io

Apagada por defecto. Solo sirve para **comparar el modelo contra el mercado**
(favorito, Over/Under explosivo/cauteloso, hándicap y dónde el modelo ve valor).
El modelo sigue siendo la fuente de verdad.

Para activarla, define en el entorno (p. ej. en Render):

| Variable | Default | Descripción |
|---|---|---|
| `ODDS_API_IO_KEY` | — | key gratis de odds-api.io (requerida para activar) |
| `ODDS_API_IO_LIGA` | `mexico-liga-mx-apertura` | slug de la liga |
| `ODDS_API_IO_MAX_CASAS` | `2` | máx. casas por consulta (límite del tier gratis) |
| `ODDS_API_IO_BOOKMAKERS` | (auto) | forzar casas concretas (coma) en vez de auto-selección |

El bot **auto-selecciona** las casas que sí tienen momios de Liga MX. Verifica el
estado en `GET /valor/diagnostico`.

> En pretemporada puede no haber momios publicados aún; aparecen solos conforme
> se acerca la jornada.

## Tests y CI

```bash
python3 -m pytest tests/
```

El workflow `.github/workflows/ci.yml` instala dependencias y **corre toda la
suite** en cada push y pull request.

## Calendario de la temporada — Liga MX API (para el planificador)

El planificador de Survivor (`/plan-survivor`) necesita el calendario completo
de las 17 jornadas en `data/calendario.json`. Se genera con:

```bash
python3 scripts/import_calendario.py                 # Liga MX API -> fallback ESPN
python3 scripts/import_calendario.py --fuente espn   # forzar ESPN
python3 scripts/import_calendario.py --dry-run       # ver sin escribir
```

La fuente primaria es la **Liga MX API** (proyecto hermano,
`https://ligamx-api.onrender.com`, sin key), que expone los fixtures del torneo
vía `/calendar`. El cliente vive en `src/ligamx_api.py` y es **opcional y
tolerante a fallos**: si la API está caída o dormida, el script cae a ESPN. Las
jornadas se re-derivan de las **fechas reales** (regla round-robin), corrigiendo
cualquier agrupado imperfecto del upstream → **17 jornadas × 9** limpias. Se
configura con `LIGAMX_API_URL` / `LIGAMX_API_TIMEOUT` (ver `.env.example`) y su
estado se ve en `GET /health/fuentes`.

Cuando la temporada tenga partidos jugados, esta misma API puede alimentar el
modelo Poisson con resultados reales: activa `LIGAMX_API_AS_SOURCE=1` y
`fuentes_datos` la usará como fuente primaria (en pretemporada devuelve vacío y
cae a ESPN, así que activarla no rompe nada).

### Señales de enriquecimiento (Liga MX API)

`src/ligamx_api.py` también expone señales por equipo y por partido que se
juntan en un **dossier** vía `GET /analisis-partido?home=America&away=Toluca`:
predictor de la API (2ª opinión), forma reciente, disciplina/tarjetas
(jugadores en riesgo de suspensión), rachas, head-to-head, más utilidades de
liga (proyección de tabla, power-ranking, goleadores, noticias/lesiones). Todo
es **tolerante a fallos**: cada señal que la API aún no tenga (pretemporada)
llega en `null`, sin romper el resto. El modelo local (ESPN + Poisson) sigue
siendo la fuente de verdad del pick; esto es contexto informativo.

## Herramientas locales (opcionales)

Utilidades locales de apoyo manual (no deciden picks, no envían nada):
`scripts/import_fbref_schedule.py` (importar/auditar calendario de FBref a mano),
`scripts/assisted_caliente_odds.py` (importar momios pegados manualmente, sin
scraping), `scripts/rss_lesiones_ligamx.py` (lesiones vía RSS) y
`scripts/final_security_gate.py` (gate de seguridad).

## Notas

- El bot **nunca** cierra ni envía picks automáticos. Salidas:
  `INFORMATIVO / REVISIÓN HUMANA`.
- No se commitea `.env`, `data/`, `reports/` ni logs. No se imprimen secretos.
- Más comandos en [`COMMANDS.md`](COMMANDS.md); historial en
  [`CHANGELOG.md`](CHANGELOG.md).
