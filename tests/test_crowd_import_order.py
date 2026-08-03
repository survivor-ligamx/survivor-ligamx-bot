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
de importación de FastAPI. Sin red y sin credenciales: solo se registran datos no
sensibles (conteos, porcentajes de crowd, scores y orden del pick).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SCRIPT = r"""
import json
import sys

scenario = sys.argv[1]

if scenario == "web":
    import src.api  # noqa: F401  (ruta real de uvicorn: src.api:app)
import src.motor_pronosticos as motor

pronos = [
    {
        "local": "Pumas UNAM",
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

pen = motor._penalizacion_crowd("Pumas UNAM")
score_oficial = 82.0 + 0.5 * 62.0
score_con_pen = score_oficial - pen

print(json.dumps({
    "scenario": scenario,
    "n_equipos_crowd": len(motor._CROWD_DIST),
    "necaxa_pct": motor._crowd_pct("Necaxa"),
    "pumas_pct": motor._crowd_pct("Pumas UNAM"),
    "pen_crowd_pumas": pen,
    "penalizacion_aplicada": pen > 0,
    "score_pumas_sin_crowd": score_oficial,
    "score_pumas_con_crowd": score_con_pen,
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
    def test_cli_y_web_aplican_el_mismo_crowd(self):
        a = _escenario("cli")
        b = _escenario("web")

        self.assertEqual(b["n_equipos_crowd"], a["n_equipos_crowd"], "web no cargó el crowd")
        self.assertEqual(a["n_equipos_crowd"], 18, "el snapshot de crowd debería tener 18 equipos")
        self.assertAlmostEqual(a["necaxa_pct"], 2.46, places=2)
        self.assertAlmostEqual(b["necaxa_pct"], a["necaxa_pct"], places=2, msg="web perdió el crowd de Necaxa")
        self.assertTrue(a["penalizacion_aplicada"], "CLI debería penalizar a Pumas UNAM (>15% crowd)")
        self.assertTrue(b["penalizacion_aplicada"], "web debería penalizar a Pumas UNAM igual que CLI")
        self.assertEqual(b["pen_crowd_pumas"], a["pen_crowd_pumas"])
        self.assertEqual(b["pick_orden"], a["pick_orden"], "el orden del pick depende del orden de importación")

    def test_penalizacion_crowd_tiene_efecto_real(self):
        a = _escenario("cli")
        self.assertGreater(
            a["score_pumas_sin_crowd"] - a["score_pumas_con_crowd"], 0, "la penalización no cambió el score"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
