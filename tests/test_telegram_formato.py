#!/usr/bin/env python3
"""Tests para los helpers de formato de src/telegram_pronosticos.py.

Caracterizan el comportamiento de las funciones puras de formato (la base para
un futuro refactor que las extraiga a su propio módulo). Sin red.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import telegram_pronosticos as tp  # noqa: E402


class TestPct(unittest.TestCase):
    def test_redondeo_sin_decimales(self):
        self.assertEqual(tp._pct(55.0), "55")
        self.assertEqual(tp._pct(55.6), "56")
        self.assertEqual(tp._pct(55.4), "55")

    def test_no_numerico_devuelve_str(self):
        self.assertEqual(tp._pct("abc"), "abc")
        self.assertEqual(tp._pct(None), "None")


class TestNormSimple(unittest.TestCase):
    def test_normaliza_espacios_y_case(self):
        self.assertEqual(tp._norm_simple("  América  FC "), "américa fc")

    def test_none_vacio(self):
        self.assertEqual(tp._norm_simple(None), "")


class TestMarcadorAFavor(unittest.TestCase):
    def test_local_mantiene_orden(self):
        self.assertEqual(tp._marcador_a_favor("2-1", True), "2-1")

    def test_visitante_invierte(self):
        self.assertEqual(tp._marcador_a_favor("2-1", False), "1-2")

    def test_invalido_devuelve_original(self):
        self.assertEqual(tp._marcador_a_favor("x", True), "x")
        self.assertEqual(tp._marcador_a_favor(None, True), "")


class TestDividirMensaje(unittest.TestCase):
    def test_corto_sin_cortar(self):
        self.assertEqual(tp._dividir_mensaje("hola", limite=100), ["hola"])

    def test_largo_corta_en_saltos_de_linea(self):
        texto = "a" * 50 + "\n" + "b" * 50
        partes = tp._dividir_mensaje(texto, limite=60)
        self.assertGreater(len(partes), 1)
        for p in partes:
            self.assertLessEqual(len(p), 60)
        # Reensamblado conserva el texto original.
        self.assertEqual("\n".join(partes), texto)

    def test_linea_larga_corte_duro(self):
        texto = "x" * 150
        partes = tp._dividir_mensaje(texto, limite=60)
        self.assertEqual(len(partes), 3)
        for p in partes:
            self.assertLessEqual(len(p), 60)


class TestTotalesJornada(unittest.TestCase):
    def test_vacio_ceros(self):
        t = tp._totales_jornada([])
        self.assertEqual(t["partidos"], 0)
        self.assertEqual(t["goles_esperados_total"], 0.0)
        self.assertEqual(t["over_25_count"], 0)

    def test_con_datos_agrega(self):
        pronosticos = [
            {"goles_esperados_local": 1.5, "goles_esperados_visitante": 1.0, "pick_ou": "Over", "pick_btts": "Sí"},
            {"goles_esperados_local": 0.5, "goles_esperados_visitante": 0.3, "pick_ou": "Under", "pick_btts": "No"},
        ]
        t = tp._totales_jornada(pronosticos)
        self.assertEqual(t["partidos"], 2)
        self.assertEqual(t["over_25_count"], 1)
        self.assertEqual(t["under_25_count"], 1)
        self.assertEqual(t["btts_si_count"], 1)
        self.assertEqual(t["btts_no_count"], 1)
        self.assertAlmostEqual(t["goles_esperados_total"], 3.3, places=1)
        self.assertAlmostEqual(t["promedio_goles_partido"], 1.65, places=2)

    def test_desempate_solo_con_cobertura_completa(self):
        pronosticos = [
            {
                "goles_esperados_local": 1.4,
                "goles_esperados_visitante": 1.1,
                "pick_ou": "Over",
                "pick_btts": "Sí",
            },
            {
                "goles_esperados_local": 0.8,
                "goles_esperados_visitante": 0.7,
                "pick_ou": "Under",
                "pick_btts": "No",
            },
        ]
        t = tp._totales_jornada(pronosticos, partidos_esperados=2)
        self.assertTrue(t["cobertura_completa"])
        self.assertEqual(t["partidos_con_xg"], 2)
        self.assertEqual(t["goles_desempate"], 4)

    def test_partido_solo_momios_no_reduce_el_promedio_xg(self):
        pronosticos = [
            {"goles_esperados_local": 1.5, "goles_esperados_visitante": 1.0},
            {"mercado": {"1x2": {}}},
        ]
        t = tp._totales_jornada(pronosticos, partidos_esperados=2)
        self.assertEqual(t["partidos"], 2)
        self.assertEqual(t["partidos_con_xg"], 1)
        self.assertFalse(t["cobertura_completa"])
        self.assertIsNone(t["goles_desempate"])
        self.assertEqual(t["promedio_goles_partido"], 2.5)


class TestFechaMx(unittest.TestCase):
    def test_iso_valida_no_vacia(self):
        out = tp._fecha_mx("2026-07-16T20:00:00Z")
        self.assertIsInstance(out, str)
        self.assertTrue(out)
        # zoneinfo disponible -> horario CDMX; si no, fallback UTC.
        self.assertTrue("h (CDMX)" in out or "UTC" in out)

    def test_invalida_fallback(self):
        self.assertEqual(tp._fecha_mx("no-es-fecha"), "no-es-fecha")


class TestCercaDeJornada(unittest.TestCase):
    def test_vacio_false(self):
        self.assertFalse(tp._cerca_de_jornada([]))

    def test_muy_lejos_false(self):
        self.assertFalse(tp._cerca_de_jornada([{"fecha": "2099-01-01"}], dias=2))

    def test_fecha_malformada_se_ignora(self):
        self.assertFalse(tp._cerca_de_jornada([{"fecha": "basura"}], dias=2))


class TestLineaGoles(unittest.TestCase):
    def test_sin_datos_vacio(self):
        self.assertEqual(tp._linea_goles({}), "")

    def test_over_con_marcador(self):
        p = {"pick_ou": "Over", "prob_over_pct": 60.0, "pick_btts": "Sí", "marcador_mas_probable": "2-1"}
        out = tp._linea_goles(p)
        self.assertIn("Over 2.5", out)
        self.assertIn("60%", out)
        self.assertIn("BTTS Sí", out)
        self.assertIn("2-1", out)

    def test_under_usa_complemento(self):
        p = {"pick_ou": "Under", "prob_over_pct": 60.0, "marcador_mas_probable": "1-0"}
        out = tp._linea_goles(p)
        self.assertIn("Under 2.5", out)
        self.assertIn("40%", out)  # complemento de 60%


if __name__ == "__main__":
    unittest.main(verbosity=2)
