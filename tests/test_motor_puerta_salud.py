#!/usr/bin/env python3
"""Puerta de salud de la Liga MX API antes de ajustar por XI. Sin red."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import motor_pronosticos as mp  # noqa: E402


def _pronosticos():
    return [
        {"local": "América", "visitante": "Toluca", "no_perder_local_pct": 80.0, "no_perder_visitante_pct": 40.0},
        {"local": "Atlas", "visitante": "Pumas UNAM", "no_perder_local_pct": 55.0, "no_perder_visitante_pct": 60.0},
    ]


class TestPuertaDeSalud(unittest.TestCase):
    def setUp(self):
        parche = mock.patch.dict("os.environ", {"LIGAMX_API_URL": "https://ejemplo.test", "AJUSTE_XI": "1"})
        parche.start()
        self.addCleanup(parche.stop)

    def test_api_caida_devuelve_la_base_sin_tocarla(self):
        # Si la API no contesta, la jornada se rankea igual (sin ajuste) en vez
        # de encadenar una llamada por partido y morir en un 504.
        with mock.patch("src.ligamx_api.disponible", return_value=False):
            with mock.patch("src.ligamx_api.lineup_impact_partido") as impacto:
                salida = mp.ajustar_por_alineaciones(_pronosticos())
        self.assertEqual(salida, _pronosticos())
        impacto.assert_not_called()

    def test_la_puerta_usa_el_timeout_corto(self):
        with mock.patch("src.ligamx_api.disponible", return_value=False) as salud:
            mp.ajustar_por_alineaciones(_pronosticos())
        salud.assert_called_once_with(timeout=mp.SALUD_TIMEOUT_S)

    def test_api_viva_consulta_cada_partido_con_el_mapa(self):
        mapa = {"america": {"fuerza_xi_pct": 96.0}}
        with mock.patch("src.ligamx_api.disponible", return_value=True):
            with mock.patch("src.suspensiones.suspendidos_por_equipo", return_value=mapa):
                with mock.patch("src.ligamx_api.lineup_impact_partido", return_value={}) as impacto:
                    mp.ajustar_por_alineaciones(_pronosticos())
        self.assertEqual(impacto.call_count, 2)
        for llamada in impacto.call_args_list:
            self.assertEqual(llamada.args[2], mapa)

    def test_sin_api_configurada_ni_siquiera_pregunta(self):
        with mock.patch.dict("os.environ", {"LIGAMX_API_URL": ""}):
            with mock.patch("src.ligamx_api.disponible") as salud:
                salida = mp.ajustar_por_alineaciones(_pronosticos())
        self.assertEqual(salida, _pronosticos())
        salud.assert_not_called()

    def test_mapa_suspendidos_tolera_fallos(self):
        with mock.patch("src.suspensiones.suspendidos_por_equipo", side_effect=RuntimeError("boom")):
            self.assertEqual(mp._mapa_suspendidos(), {})

    def test_mapa_suspendidos_devuelve_copia(self):
        original = {"atlas": {"fuerza_xi_pct": 92.0}}
        with mock.patch("src.suspensiones.suspendidos_por_equipo", return_value=original):
            copia = mp._mapa_suspendidos()
        self.assertEqual(copia, original)
        self.assertIsNot(copia, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
