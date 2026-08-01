# Comandos — Survivor Liga MX Bot

Todos los comandos se ejecutan desde la raíz del proyecto. Las funciones que usan
APIs externas requieren un archivo `.env` con las llaves necesarias (nunca se
versiona). El bot es **informativo**: nunca cierra ni envía picks automáticos.

## Bot completo (pipeline real: ESPN + Poisson)

```bash
./run_bot.sh
```

Baja resultados reales de ESPN (con caché/respaldo si falla) y genera
pronósticos 1X2/Over-Under/BTTS + pick de Survivor + top-3. Para enviar a
Telegram, pásale `--telegram` (se reenvía a `main.py`):

```bash
./run_bot.sh --telegram
./run_bot.sh --excluir America,Toluca   # excluir equipos ya usados en Survivor
```

## Generar pronósticos sin bajar datos

```bash
python3 main.py                       # solo reporte local
python3 main.py --telegram            # además envía a Telegram
python3 main.py --excluir America,Toluca
```

## Recalibrar / validar el modelo

```bash
python3 src/validacion_modelo.py      # accuracy / Brier contra resultados reales
```

## Backtest del juego Survivor

```bash
python3 src/simulador_survivor.py     # ¿cuántas jornadas sobrevives?
```

## Análisis de riesgo: ¿cuándo falla el favorito?

```bash
python3 src/analisis_riesgo.py        # tasas reales de fallo del favorito
```

Recorre el histórico real (walk-forward) y mide cuándo NO gana el favorito del
modelo, desglosado por local/visitante, nivel de confianza y partidos cerrados
('under'). Útil para no quemar el Survivor con un favorito engañoso. También en
la web: `GET /analisis/riesgo`.

## Planificador de temporada (estrategia Survivor)

```bash
python3 src/planificador_survivor.py   # qué equipo usar en cada jornada
```

Resuelve TODA la temporada con programación dinámica y estado de vida de empate
(disponible/consumida): 1 equipo por jornada, sin repetir, con transición oficial
de supervivencia (empate solo salva una vez). También en la web:
`GET /plan-survivor?excluir=America,Toluca&peso_victoria=0.5&vida_empate_consumida=false`.

Requiere `data/calendario.json` con el calendario completo (se publica cerca del
arranque, ~17-jul). Esquema:

```json
[
  {"jornada": 1, "partidos": [
    {"home_team": "América", "away_team": "Atlético de San Luis"},
    {"home_team": "Cruz Azul", "away_team": "Querétaro"}
  ]},
  {"jornada": 2, "partidos": [ ... ]}
]
```

> Importante: los nombres de los equipos deben coincidir con los de ESPN (con
> acentos): `América`, `Atlético de San Luis`, `Mazatlán FC`, `Querétaro`,
> `Tigres UANL`, `Pumas UNAM`, `Guadalajara`, `León`, `FC Juárez`, etc.

Para generar `data/calendario.json` automáticamente desde ESPN (cuando ya
publicaron el calendario del torneo):

```bash
python3 scripts/import_calendario.py            # baja de ESPN y escribe
python3 scripts/import_calendario.py --dry-run  # muestra sin escribir
```

Agrupa los fixtures programados por fin de semana (jornada) y usa los nombres de
ESPN, así que ya quedan listos para el planificador. El agrupado detecta el
cambio de jornada cuando un equipo se repite o cambia el fin de semana (maneja
partidos entre semana).

Con momios reales: si `ODDS_API_IO_KEY` está activa, `/plan-survivor` mezcla los
momios de odds-api.io para las jornadas con cobertura (`usar_momios=true` por
defecto; `?usar_momios=false` para solo-modelo).

## Telegram

Telegram es **opcional e informativo**: el bot **nunca** envía picks automáticos.
Todo mensaje pasa por el safety gate; si el reporte no conserva una etiqueta
segura o contiene señales prohibidas (`CERRAR`, `ENVIAR PICK`, `APOSTAR`…), el
envío se **bloquea**.

```bash
# Envío real (requiere TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en .env)
python3 src/telegram_notifier.py --report reports/reporte_survivor_ultimo.txt

# Previsualizar sin enviar (respeta el safety gate; funciona sin credenciales)
python3 src/telegram_notifier.py --report reports/reporte_survivor_ultimo.txt --dry-run
```

En la web (requieren `API_KEY`): `POST /alerts/pronosticos` (pronósticos + top-3
de Survivor con nivel de riesgo ALTA/MEDIA/RIESGOSA) y `POST /alerts/plan` (plan
de temporada completo, requiere `data/calendario.json`).

## Momios reales (odds-api.io, opcional)

Si `ODDS_API_IO_KEY` está configurada, la web expone los momios en `/valor`,
`/valor/diagnostico` y `/jornada`. Sin key, esa parte queda apagada y el resto
del bot funciona igual.

## Herramientas locales de apoyo (opcionales, manuales)

No deciden ni envían picks; no hacen scraping a sitios con login/anti-bot.

```bash
# Importar momios pegados a mano (sin scraping)
python3 scripts/assisted_caliente_odds.py --help

# Importar/auditar calendario de FBref guardado manualmente como HTML
python3 scripts/import_fbref_schedule.py \
  --html data/fbref/raw/fbref_ligamx_schedule.html \
  --jornada 1 --jornadas-json data/jornadas.json \
  --out-dir data/fbref --reports-dir reports

# Lesiones vía RSS
python3 scripts/rss_lesiones_ligamx.py --help

# Gate de seguridad (revisa que no haya secretos/señales prohibidas)
python3 scripts/final_security_gate.py
```

## API pública unificada de Liga MX (`/api/v1`)

API REST gratis, sin key, read-only, que consolida en un solo lugar todo lo que
el bot recolecta de fuentes públicas (ESPN + TheSportsDB + calendario oficial +
modelo Poisson). Cacheada. Cada respuesta lleva `INFORMATIVO / REVISIÓN HUMANA`.

| Endpoint | Qué devuelve |
|---|---|
| `GET /api/v1` | Índice/catálogo de la API |
| `GET /api/v1/equipos` | Equipos del torneo (+ `tiene_modelo`) |
| `GET /api/v1/equipos/{equipo}` | Ficha: fuerzas del modelo + calendario del equipo |
| `GET /api/v1/equipos/{equipo}/calendario` | Todos los partidos del equipo en la temporada |
| `GET /api/v1/calendario` | Calendario completo (17 jornadas) |
| `GET /api/v1/calendario/{jornada}?predicciones=true` | Una jornada (con predicción opcional) |
| `GET /api/v1/jornada-actual?fecha=&predicciones=` | Jornada en curso/próxima según la fecha (auto) |
| `GET /api/v1/resultados?meses=2` | Resultados reales recientes (ESPN) |
| `GET /api/v1/tabla` | Tabla general + motivación |
| `GET /api/v1/predicciones` | Predicciones de la jornada próxima |
| `GET /api/v1/h2h?local=&visitante=` | Head-to-head histórico real + predicción del modelo |

La búsqueda de equipos **ignora acentos** y entiende alias comunes
(`chivas`, `pumas`, `tigres`, `xolos`, `rayados`, `san luis`, etc.).

## Endpoints web (FastAPI en Render)

`/predicciones` · `/survivor?excluir=` · `/jornada` (todo-en-uno) · `/tabla` ·
`/valor` · `/valor/diagnostico` · `/health/fuentes` · `/analisis/riesgo` ·
`/plan-survivor` · `/stats` · `/history` · `/dashboard` · `/health` ·
`/cron/backtest` (validación real diaria) · `/docs`.

## Tests y lint

```bash
python3 -m pytest tests/      # toda la suite (también corre en CI)
ruff check .                  # linter (gate en CI)
```
