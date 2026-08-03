#!/usr/bin/env python3
"""
crowd_data.py — Datos del crowd (distribución de picks de la comunidad).

Módulo NEUTRO: solo contiene datos, sin imports de routers, FastAPI ni
motor_pronosticos. Vivía en routers/predicciones.py y motor_pronosticos lo
importaba, creando un import circular que, según el orden de importación de
FastAPI, dejaba la distribución vacía en el motor (crowd mostrado en la
respuesta pero sin penalización real en el score).

Se movió aquí para que ambos consumidores importen desde el mismo sitio y el
orden de importación deje de importar.
"""

from __future__ import annotations

from typing import Dict

# Distribución de picks de la comunidad (Playdoit Survivor Fecha 4).
# Sirve para identificar "picks populares" que eliminan a muchos si fallan.
# Fuente: Pick Distribution pública de Playdoit (survivorplaydoit.mx).
# ⚠️ Actualizar cada jornada: estos % cambian. Fecha de captura: 2026-08-03 (J4).
# Queretaro, Puebla y FC Juarez van en 0.0: no aparecen en el top-15 visible y
# la suma de los 15 listados ya llega al 100%.
# Las claves pueden ir sin acento: la lectura normaliza con canonical_team_key.
CROWD_DISTRIBUTION: Dict[str, float] = {
    "Pumas UNAM": 28.88,
    "Pachuca": 26.51,
    "Monterrey": 20.37,
    "Toluca": 9.39,
    "Guadalajara": 6.15,
    "America": 2.99,
    "Necaxa": 2.46,
    "Tigres UANL": 0.97,
    "Atlas": 0.70,
    "Leon": 0.44,
    "Cruz Azul": 0.35,
    "Tijuana": 0.26,
    "Santos": 0.18,
    "Atletico de San Luis": 0.18,
    "Atlante": 0.18,
    "Queretaro": 0.0,
    "Puebla": 0.0,
    "FC Juarez": 0.0,
}

# Fecha de captura del snapshot de la distribución de la comunidad.
CROWD_CAPTURED_AT = "2026-08-03"  # J4 Apertura 2026
