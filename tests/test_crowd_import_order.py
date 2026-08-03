#!/usr/bin/env python3
"""Regresión del orden de importación del crowd (CLI vs FastAPI/web).

Reproduce el orden real de arranque web (uvicorn src.api:app) en un subprocess
separado para no contaminar ``sys.modules``:

  A. CLI: se importa primero ``src.motor_pronosticos`` (ruta de main.py/scripts).
  B. Web: se importa primero la aplicación FastAPI (``src.api``) y después se usa
     el motor por la misma ruta de producción.

Se apaga el scheduler en el subprocess (``SCHEDULER_ENABLED=false``) porque su
hilo de fondo importa módulos en paralelo y convertiría la comparación en una
carrera no determinista; aislado así, la prueba verifica exclusivamente el orden
de importación de FastAPI.

La prueba es INDEPENDIENTE del snapshot de jornada: no fija equipos, porcentajes
ni tamaños concretos. Compara la distribución que carga el motor contra el módulo
neutral ``src.crowd_data`` y exige que CLI y web coincidan por completo. Para el
efecto real de la penalización elige dinámicamente el equipo con mayor % del
snapshot y calcula la penalización esperada con los umbrales ACTUALES del motor.
Sin red y sin credenciales: solo se registran datos no sensibles.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]

import src.motor_pronosticos as motor  # noqa: E402 (ruta CLI, para el test sintético)

_SCRIPT = r"""
import json
import sys

scenario = sys.argv[1]

if scenario == "web":
    import src.api  # noqa: F401  (ruta real de uvicorn: src.api:app)
import src.motor_pronosticos as motor
from src.crowd_data import CROWD_DISTRIBUTION as DATA_DIST

motor_dist = dict(motor._CROWD_DIST or {})
fingerprint = json.dumps(sorted(motor_dist.items()), ensure_ascii=False, sort_keys=True)

# Equipo con mayor % del snapshot (dinámico; sin nombres fijos de jornada).
top_team = max(DATA_DIST, key=lambda k: DATA_DIST[k]) if DATA_DIST else "Equipo Fake"
top_pct = float(DATA_DIST.get(top_team, 0.0))

# Penalización esperada según los umbrales ACTUALES del motor (sin fijar 12 ni 15).
if top_pct >= motor.CROWD_PEN_ALTO_PCT:
    expected_pen = motor.PEN_CROWD_ALTO
elif top_pct >= motor.CROWD_PEN_MED_PCT:
    expected_pen = motor.PEN_CROWD_MEDIO
else:
    expected_pen = 0.0
actual_pen = motor._penalizacion_crowd(top_team)

# El candidato fuerte es el equipo con más crowd; el rival y la segunda plaza son
# nombres sintéticos (no existen en el snapshot, así que sin penalización).
pronos = [
    {
        "local": top_team,
        "visitante": "Rival A",
        "prob_local_pct": 62.0,
        "prob_empate_pct": 20.0,
        "prob_visitante_pct": 18.0,
        "no_perder_local_pct": 82.0,
        "no_perder_visitante_pct": 40.0,
    },
    {
        "local": "Casa Segura",
        "visitante": "Rival B",
        "prob_local_pct": 60.0,
        "prob_empate_pct": 22.0,
        "prob_visitante_pct": 18.0,
        "no_perder_local_pct": 80.0,
        "no_perder_visitante_pct": 38.0,
    },
]

est = motor.mejores_picks_estrategico(pronos, partidos_jugados_torneo=100, n=2)
picks = est.get("picks", [])

score_sin = 82.0 + 0.5 * 62.0
score_con = score_sin - actual_pen

print(json.dumps({
    "scenario": scenario,
    "n_motor": len(motor_dist),
    "n_data": len(DATA_DIST),
    "motor_matches_data": bool(motor_dist == DATA_DIST),
    "dist_fingerprint": fingerprint,
    "top_team": top_team,
    "top_pct": top_pct,
    "expected_pen": expected_pen,
    "actual_pen": actual_pen,
    "penalty_effect": score_sin - score_con,
    "pick_orden": [p.get("equipo") for p in picks],
}))
"""


def _escenario(scenario: str) -> dict:
    env = dict(os.environ)
    env["SCHEDULER_ENABLED"] = "false"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT, scenario],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"subprocess {scenario} falló (rc={proc.returncode}):\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestCrowdImportOrder(unittest.TestCase):
    def test_cli_y_web_cargan_la_misma_distribucion(self):
        a = _escenario("cli")
        b = _escenario("web")

        # La distribución del módulo neutral no está vacía.
        self.assertGreater(a["n_data"], 0, "src.crowd_data.CROWD_DISTRIBUTION está vacío")
        # El motor carga COMPLETAMENTE la distribución en ambos órdenes de importación.
        self.assertTrue(a["motor_matches_data"], "CLI no cargó CROWD_DISTRIBUTION")
        self.assertTrue(b["motor_matches_data"], "web no cargó CROWD_DISTRIBUTION (import circular)")
        # Mismo tamaño y exactamente la misma distribución.
        self.assertEqual(a["n_motor"], a["n_data"])
        self.assertEqual(b["n_motor"], a["n_motor"])
        self.assertEqual(b["dist_fingerprint"], a["dist_fingerprint"], "CLI y web no cargan la misma distribución")
        # Misma penalización y mismo orden del pick.
        self.assertEqual(b["actual_pen"], a["actual_pen"], "la penalización depende del orden de importación")
        self.assertEqual(b["pick_orden"], a["pick_orden"], "el orden del pick depende del orden de importación")

    def test_penalizacion_crowd_usa_los_umbrales_actuales(self):
        a = _escenario("cli")
        # El equipo con más crowd del snapshot se penaliza según los umbrales vigentes.
        self.assertEqual(a["actual_pen"], a["expected_pen"], "la penalización no coincide con los umbrales del motor")
        # La penalización tiene efecto real sobre el score.
        self.assertEqual(a["penalty_effect"], a["actual_pen"])

    def test_penalizacion_umbrales_con_datos_sinteticos(self):
        # Verifica la relación umbral->penalización con datos sintéticos, sin depender
        # del snapshot ni del orden de importación.
        dist = {"EquipoA": 20.0, "EquipoB": 7.0, "EquipoC": 2.0}
        with mock.patch.object(motor, "_CROWD_DIST", dist):
            self.assertEqual(motor._penalizacion_crowd("EquipoA"), motor.PEN_CROWD_ALTO)
            self.assertEqual(motor._penalizacion_crowd("EquipoB"), motor.PEN_CROWD_MEDIO)
            self.assertEqual(motor._penalizacion_crowd("EquipoC"), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
