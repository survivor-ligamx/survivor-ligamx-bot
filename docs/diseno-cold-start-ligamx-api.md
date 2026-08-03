# Diseño: cold start, salud y consultas de Liga MX API

## Estado

Propuesta de diseño. No cambia código ni configuración de producción.

## Objetivos

- Mantener `LIGAMX_API_TIMEOUT=5` como límite predeterminado de las consultas normales.
- Mantener la puerta rápida de salud antes de usar Liga MX API como fuente.
- Evitar que un cold start de Render bloquee el cálculo completo del pick.
- Reutilizar datos recientes de 365Scores sin confundir caché vieja con datos actuales.
- Degradar de forma explícita y observable hacia las fuentes existentes.

## No objetivos

- No aumentar el timeout general a 30 segundos.
- No convertir `/health` en una consulta a base de datos o a proveedores externos.
- No cambiar probabilidades, reglas de Survivor ni prioridades deportivas.
- No añadir reintentos ilimitados dentro de una solicitud del usuario.

## Separación de responsabilidades

### 1. Salud rápida

`/health` debe comprobar únicamente que el proceso HTTP responde.

- Sin consultas a PostgreSQL, Redis, ESPN o 365Scores.
- Presupuesto recomendado en el bot: 1 segundo.
- Una respuesta satisfactoria habilita una consulta normal, pero no garantiza frescura de datos.
- Un timeout o error marca la API como no disponible para esa ejecución y activa la degradación.

La puerta rápida actual se conserva. La optimización propuesta es permitir que use un presupuesto menor que las consultas normales sin alterar el timeout predeterminado de 5 segundos.

### 2. Consultas normales

Endpoints como `/matches`, `/calendar`, `/standings` y análisis conservan el límite de 5 segundos.

- Una sola llamada por necesidad de datos.
- Sin bucles de reintentos dentro de la misma solicitud.
- Paginación con identidad canónica y corte cuando no aparecen elementos nuevos.
- Respuestas con metadatos de fuente y frescura cuando aplique.

### 3. Precalentamiento de Render

El precalentamiento debe ser externo al flujo del pick.

- Un monitor programado llama solo a `/health` cada pocos minutos.
- Antes de la ventana operativa de una jornada puede hacer una llamada controlada a un endpoint liviano de lectura.
- El bot no espera a que termine un cold start durante `/pick`.
- El monitor no ejecuta sincronizaciones ni proveedores externos.

### 4. Caché de 365Scores

La API puede conservar la última respuesta válida con sus metadatos:

- `captured_at` en UTC.
- Identificador de temporada y alcance de la consulta.
- TTL específico por tipo de dato.
- Último error de refresco separado del último valor válido.

Política sugerida:

| Dato | TTL fresco | Uso degradado |
| --- | ---: | --- |
| Calendario/partidos próximos | 15 min | hasta 6 h, marcado como viejo |
| Resultados en jornada | 5 min | hasta 1 h |
| Tabla | 15 min | hasta 6 h |
| Alineaciones | 2 min | no usar vieja después del inicio |
| Noticias | 30 min | hasta 24 h |

Una fecha ausente, inválida o futura fuera de tolerancia invalida la entrada. Nunca se debe etiquetar una respuesta vieja como fresca.

## Flujo propuesto del bot

1. Consultar la puerta rápida de salud con presupuesto corto.
2. Si responde, ejecutar la consulta normal con el timeout predeterminado de 5 segundos.
3. Si la consulta responde, validar esquema, identidad y frescura.
4. Si la API está fría, falla o entrega datos inválidos, continuar con las fuentes existentes.
5. Registrar el motivo de degradación sin interrumpir el pick.
6. No reintentar más de una vez dentro de la misma ejecución.

## Estados observables

La integración debe distinguir:

- `healthy`: proceso y consulta normal disponibles.
- `warming`: salud responde, pero la primera consulta todavía no.
- `degraded`: se usó una fuente alternativa o una caché permitida.
- `stale`: existe dato anterior, pero excede TTL fresco.
- `unavailable`: no hay respuesta válida ni caché permitida.

Los estados son informativos y no modifican por sí mismos las probabilidades.

## Métricas mínimas

- Latencia de `/health` y de cada consulta.
- Conteo de cold starts inferidos.
- Porcentaje de ejecuciones degradadas.
- Edad de la caché servida.
- Fuente final usada por tipo de dato.
- Errores y timeouts por endpoint.

No registrar llaves, tokens, encabezados de autenticación ni payloads sensibles.

## Plan de implementación

### Fase A: observabilidad

- Medir latencia y motivo de degradación.
- Añadir pruebas de estados sin cambiar decisiones.

### Fase B: presupuestos separados

- Presupuesto corto solo para `/health`.
- Mantener 5 segundos en consultas normales.
- Probar cold start, timeout y recuperación.

### Fase C: caché con frescura

- Añadir `captured_at`, alcance y TTL por tipo de dato.
- Implementar stale-while-revalidate solo donde la tabla anterior lo permite.
- Rechazar fechas ausentes o corruptas.

### Fase D: precalentamiento

- Configurar monitor externo para `/health`.
- Evaluar con métricas antes de añadir una lectura liviana previa a jornada.

## Criterios de aceptación

- Un cold start no retrasa `/pick` más allá de los presupuestos definidos.
- El timeout normal sigue siendo 5 segundos.
- La puerta rápida de salud sigue activa.
- La caída de Liga MX API no impide producir una respuesta con fuentes alternativas.
- Toda caché usada expone su edad y fuente.
- Alineaciones viejas no se reutilizan después del inicio del partido.
- Las pruebas cubren salud rápida, API fría, consulta recuperada, caché fresca, caché vieja y timestamp inválido.
