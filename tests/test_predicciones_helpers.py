#!/usr/bin/env python3
"""Tests para los helpers puros de src/routers/predicciones.py. Sin red: lmx/BD mockeados."""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

pred = importlib.import_module("routers.predicciones")


class TestTotalesJornada(unittest.TestCase):
    def test_vacio_devuelve_ceros(self):
        t = pred._totales_jornada([])
        self.assertEqual(t["partidos"], 0)
        self.assertEqual(t["goles_esperados_total"], 0.0)
        self.assertEqual(t["over_25_count"], 0)

    def test_totales_con_datos(self):
        pronosticos = [
            {"goles_esperados_local": 1.5, "goles_esperados_visitante": 1.0, "pick_ou": "Over", "pick_btts": "Sí"},
            {"goles_esperados_local": 0.5, "goles_esperados_visitante": 0.3, "pick_ou": "Under", "pick_btts": "No"},
        ]
        t = pred._totales_jornada(pronosticos)
        self.assertEqual(t["partidos"], 2)
        self.assertEqual(t["over_25_count"], 1)
        self.assertEqual(t["under_25_count"], 1)
        self.assertEqual(t["btts_si_count"], 1)
        self.assertEqual(t["btts_no_count"], 1)
        self.assertAlmostEqual(t["goles_esperados_total"], 3.3, places=1)
        self.assertAlmostEqual(t["promedio_goles_partido"], 1.65, places=2)


class TestEnriquecerCrowd(unittest.TestCase):
    def test_none_passthrough(self):
        self.assertIsNone(pred._enriquecer_con_crowd(None))

    def test_umbrales_de_riesgo(self):
        dist = {"América": 20.0, "Toluca": 10.0, "Puebla": 2.0}
        with mock.patch.object(pred, "CROWD_DISTRIBUTION", dist):
            self.assertEqual(pred._enriquecer_con_crowd({"equipo": "América"})["crowd_risk"], "ALTO")
            self.assertEqual(pred._enriquecer_con_crowd({"equipo": "Toluca"})["crowd_risk"], "MEDIO")
            self.assertEqual(pred._enriquecer_con_crowd({"equipo": "Puebla"})["crowd_risk"], "BAJO")

    def test_equipo_desconocido_cero(self):
        with mock.patch.object(pred, "CROWD_DISTRIBUTION", {}):
            out = pred._enriquecer_con_crowd({"equipo": "Nadie"})
        self.assertEqual(out["crowd_pct"], 0.0)
        self.assertEqual(out["crowd_risk"], "BAJO")

    def test_lista(self):
        with mock.patch.object(pred, "CROWD_DISTRIBUTION", {}):
            out = pred._enriquecer_lista_con_crowd([{"equipo": "A"}, {"equipo": "B"}])
        self.assertEqual(len(out), 2)
        self.assertIn("crowd_pct", out[0])

    def test_acentos_casan_con_claves_sin_acento(self):
        """El motor entrega 'América'/'León'; el snapshot puede venir sin acento."""
        dist = {
            "America": 20.0,
            "Leon": 6.0,
            "Queretaro": 0.17,
            "Atletico de San Luis": 0.17,
        }
        with mock.patch.object(pred, "CROWD_DISTRIBUTION", dist):
            america = pred._enriquecer_con_crowd({"equipo": "América"})
            leon = pred._enriquecer_con_crowd({"equipo": "León"})
            queretaro = pred._enriquecer_con_crowd({"equipo": "Querétaro"})
            san_luis = pred._enriquecer_con_crowd({"equipo": "Atlético de San Luis"})
        self.assertEqual(america["crowd_pct"], 20.0)
        self.assertEqual(america["crowd_risk"], "ALTO")
        self.assertEqual(leon["crowd_pct"], 6.0)
        self.assertEqual(leon["crowd_risk"], "MEDIO")
        self.assertEqual(queretaro["crowd_pct"], 0.17)
        self.assertEqual(san_luis["crowd_pct"], 0.17)

    def test_alias_conocidos_casan(self):
        dist = {"Guadalajara": 18.0, "FC Juarez": 19.68, "Tigres UANL": 1.55}
        with mock.patch.object(pred, "CROWD_DISTRIBUTION", dist):
            self.assertEqual(pred._enriquecer_con_crowd({"equipo": "Chivas"})["crowd_pct"], 18.0)
            self.assertEqual(pred._enriquecer_con_crowd({"equipo": "FC Juárez"})["crowd_pct"], 19.68)
            self.assertEqual(pred._enriquecer_con_crowd({"equipo": "Tigres"})["crowd_pct"], 1.55)

    def test_equipo_vacio_no_rompe(self):
        with mock.patch.object(pred, "CROWD_DISTRIBUTION", {"America": 20.0}):
            out = pred._enriquecer_con_crowd({"rival": "Toluca"})
        self.assertEqual(out["crowd_pct"], 0.0)
        self.assertEqual(out["crowd_risk"], "BAJO")

    def test_coincidencia_exacta_tiene_prioridad(self):
        """Si la clave existe tal cual, no se normaliza (evita colisiones raras)."""
        dist = {"Santos": 0.02, "Santos Laguna": 5.0}
        with mock.patch.object(pred, "CROWD_DISTRIBUTION", dist):
            self.assertEqual(pred._enriquecer_con_crowd({"equipo": "Santos"})["crowd_pct"], 0.02)


class TestUsadosCombinados(unittest.TestCase):
    def test_combina_y_dedup(self):
        with mock.patch("src.database.get_equipos_usados", return_value=["América", "Cruz Azul"]):
            out = pred._usados_combinados("Toluca, América")
        # Persistidos primero; América (manual) duplicado se descarta (case-insensitive).
        self.assertEqual(out, ["América", "Cruz Azul", "Toluca"])

    def test_bd_falla_usa_solo_parametro(self):
        with mock.patch("src.database.get_equipos_usados", side_effect=RuntimeError("sin BD")):
            out = pred._usados_combinados("Toluca, Puebla")
        self.assertEqual(out, ["Toluca", "Puebla"])

    def test_vacio(self):
        with mock.patch("src.database.get_equipos_usados", return_value=[]):
            self.assertEqual(pred._usados_combinados(""), [])


class TestPartidosJugadosTorneo(unittest.TestCase):
    def test_devuelve_int(self):
        with mock.patch.object(pred.lmx, "estado_temporada", return_value={"finished_matches": 42}):
            self.assertEqual(pred._partidos_jugados_torneo(), 42)

    def test_sin_dato_devuelve_none(self):
        with mock.patch.object(pred.lmx, "estado_temporada", return_value={}):
            self.assertIsNone(pred._partidos_jugados_torneo())

    def test_api_falla_devuelve_none(self):
        with mock.patch.object(pred.lmx, "estado_temporada", side_effect=RuntimeError("down")):
            self.assertIsNone(pred._partidos_jugados_torneo())


class TestContextoPick(unittest.TestCase):
    def test_local_deriva_home_equipo(self):
        with mock.patch.object(pred.lmx, "resumen_partido", return_value={"home": "A"}) as m:
            out = pred._contexto_pick({"equipo": "América", "rival": "Toluca", "condicion": "Local"})
        m.assert_called_once_with("América", "Toluca")
        self.assertEqual(out["home"], "A")

    def test_visitante_deriva_home_rival(self):
        with mock.patch.object(pred.lmx, "resumen_partido", return_value={"x": 1}) as m:
            pred._contexto_pick({"equipo": "América", "rival": "Toluca", "condicion": "Visitante"})
        m.assert_called_once_with("Toluca", "América")


if __name__ == "__main__":
    unittest.main(verbosity=2)
