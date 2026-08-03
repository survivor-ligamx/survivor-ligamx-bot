#!/usr/bin/env python3
"""Tests para src/comparador_mercado.py (comparación modelo vs mercado). Sin red."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import comparador_mercado as cm  # noqa: E402


def _odds_response():
    """Respuesta /odds con el formato real de odds-api.io (2 casas)."""
    return {
        "id": 1,
        "home": "América",
        "away": "Toluca",
        "status": "pending",
        "bookmakers": {
            "Bet365": [
                {"name": "ML", "odds": [{"home": "2.00", "draw": "3.40", "away": "4.00"}]},
                {"name": "Over/Under", "odds": [{"max": 2.5, "over": "1.80", "under": "2.05"}]},
                {"name": "Asian Handicap", "odds": [{"hdp": -0.5, "home": "1.90", "away": "1.95"}]},
            ],
            "Pinnacle": [
                {"name": "ML", "odds": [{"home": "2.10", "draw": "3.50", "away": "3.80"}]},
                {"name": "Over/Under", "odds": [{"max": 2.5, "over": "1.85", "under": "2.00"}]},
            ],
        },
    }


class TestQuitarVig(unittest.TestCase):
    def test_1x2_suma_uno_y_vig(self):
        r = cm.quitar_vig(2.0, 3.5, 4.0)
        self.assertAlmostEqual(r["prob_local"] + r["prob_empate"] + r["prob_visita"], 1.0, places=9)
        self.assertGreater(r["vig"], 0.0)

    def test_2vias_suma_uno(self):
        r = cm.quitar_vig_2(1.90, 1.90)
        self.assertAlmostEqual(r["prob_a"] + r["prob_b"], 1.0, places=9)
        self.assertAlmostEqual(r["prob_a"], 0.5, places=6)

    def test_momio_invalido_lanza(self):
        with self.assertRaises(ValueError):
            cm.quitar_vig(1.0, 3.0, 4.0)
        with self.assertRaises(ValueError):
            cm.quitar_vig_2(1.0, 2.0)


class TestComparar1x2(unittest.TestCase):
    def test_detecta_valor_y_favorito(self):
        r = cm.comparar_1x2([70.0, 20.0, 10.0], 2.0, 3.5, 4.0)
        self.assertEqual(r["favorito_mercado"], "local")
        self.assertTrue(r["hay_valor"])
        self.assertEqual(r["valor_en"], "local")
        self.assertFalse(r["empate_accionable"])

    def test_sin_valor_si_igual_al_mercado(self):
        mkt = cm.quitar_vig(2.0, 3.5, 4.0)
        prob = [mkt["prob_local"], mkt["prob_empate"], mkt["prob_visita"]]
        r = cm.comparar_1x2(prob, 2.0, 3.5, 4.0)
        self.assertFalse(r["hay_valor"])


class TestCompararTotales(unittest.TestCase):
    def test_mercado_explosivo_y_valor_over(self):
        # Over barato (1.6) => mercado explosivo; modelo aún más alto => valor Over.
        r = cm.comparar_totales(75.0, 1.6, 2.4)
        self.assertEqual(r["mercado_ve"], "explosivo")
        self.assertEqual(r["valor_en"], "Over")

    def test_mercado_cauteloso(self):
        # Under barato => cauteloso.
        r = cm.comparar_totales(45.0, 2.4, 1.6)
        self.assertEqual(r["mercado_ve"], "cauteloso")


class TestHandicap(unittest.TestCase):
    def test_local_muy_favorito(self):
        r = cm.resumen_handicap(-1.5, 1.9, 1.9)
        self.assertEqual(r["favorito"], "local")
        self.assertEqual(r["fuerza"], "muy favorito")

    def test_visitante_ligero(self):
        r = cm.resumen_handicap(0.25, 1.9, 1.9)
        self.assertEqual(r["favorito"], "visitante")


class TestParsearMercado(unittest.TestCase):
    def test_extrae_ml_totales_handicap_promediando(self):
        m = cm.parsear_mercado(_odds_response())
        self.assertIn("ml", m)
        self.assertAlmostEqual(m["ml"]["local"], 2.05, places=2)  # (2.00+2.10)/2
        self.assertIn("totals", m)
        self.assertEqual(m["totals"]["linea"], 2.5)
        self.assertAlmostEqual(m["totals"]["over"], 1.825, places=3)  # (1.80+1.85)/2
        self.assertIn("handicap", m)
        self.assertEqual(m["handicap"]["linea"], -0.5)

    def test_prefiere_linea_2_5(self):
        resp = {
            "bookmakers": {
                "X": [
                    {"name": "Over/Under", "odds": [{"max": 3.0, "over": "2.5", "under": "1.5"}]},
                    {"name": "Over/Under", "odds": [{"max": 2.5, "over": "1.9", "under": "1.9"}]},
                ]
            }
        }
        m = cm.parsear_mercado(resp)
        self.assertEqual(m["totals"]["linea"], 2.5)

    def test_vacio_o_invalido(self):
        self.assertEqual(cm.parsear_mercado({}), {})
        self.assertEqual(cm.parsear_mercado({"bookmakers": {}}), {})
        self.assertEqual(cm.parsear_mercado(None), {})


class TestAnotar(unittest.TestCase):
    def _pron(self):
        return {
            "local": "América",
            "visitante": "Toluca",
            "prob_local_pct": 70.0,
            "prob_empate_pct": 20.0,
            "prob_visitante_pct": 10.0,
            "prob_over_pct": 60.0,
            "prob_under_pct": 40.0,
        }

    def test_sin_mercado_none(self):
        self.assertIsNone(cm.anotar_pronostico(self._pron(), None)["mercado"])

    def test_con_mercado_anota_bloques(self):
        mercado = cm.parsear_mercado(_odds_response())
        r = cm.anotar_pronostico(self._pron(), mercado)
        self.assertIsNotNone(r["mercado"])
        self.assertIn("1x2", r["mercado"])
        self.assertIn("over_under", r["mercado"])
        self.assertIn("handicap", r["mercado"])

    def test_lista_empareja_por_clave(self):
        mercado = cm.parsear_mercado(_odds_response())
        clave = cm._clave_partido("América", "Toluca")
        out = cm.anotar_pronosticos([self._pron()], {clave: mercado})
        self.assertIsNotNone(out[0]["mercado"])

    def test_empareja_nombres_flexibles(self):
        # Modelo (ESPN) "Tigres UANL" vs momios (odds-api) "Tigres"; "Club Tijuana".
        mercado = cm.parsear_mercado(_odds_response())
        clave = cm._clave_partido("Club Tijuana", "Tigres")
        pron = {
            "local": "Tijuana",
            "visitante": "Tigres UANL",
            "prob_local_pct": 50.0,
            "prob_empate_pct": 28.0,
            "prob_visitante_pct": 22.0,
            "prob_over_pct": 55.0,
        }
        out = cm.anotar_pronosticos([pron], {clave: mercado})
        self.assertIsNotNone(out[0]["mercado"])

    def test_no_empareja_equipos_distintos(self):
        self.assertFalse(cm._equipos_coinciden("America", "Toluca"))
        self.assertTrue(cm._equipos_coinciden("Tigres UANL", "Tigres"))
        self.assertTrue(cm._equipos_coinciden("Club Tijuana", "Tijuana"))


class TestPinnacleKeySoloEnv(unittest.TestCase):
    def test_sin_env_no_usa_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            import importlib

            importlib.reload(cm)
            self.assertEqual(cm.PINNACLE_KEY, "")
        importlib.reload(cm)  # restaurar estado del módulo

    def test_con_env_usa_valor_inyectado(self):
        inyectada = "test-pinnacle-key-abc123"
        with mock.patch.dict(os.environ, {"PINNACLE_API_KEY": inyectada}, clear=True):
            import importlib

            importlib.reload(cm)
            self.assertEqual(cm.PINNACLE_KEY, inyectada)
        importlib.reload(cm)  # restaurar estado del módulo

    def test_get_pinnacle_falla_sin_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            import importlib

            importlib.reload(cm)
            with mock.patch.object(cm, "requests") as mock_requests:
                with self.assertRaises(RuntimeError):
                    cm._get_pinnacle("/sports/29/leagues?brandId=0")
                mock_requests.get.assert_not_called()
        importlib.reload(cm)  # restaurar estado del módulo


class TestGating(unittest.TestCase):
    def test_deshabilitado_sin_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(cm.mercado_habilitado())
            self.assertEqual(cm.obtener_momios_liga_mx(), {})

    def test_habilitado_con_key(self):
        with mock.patch.dict(os.environ, {cm.ENV_KEY: "abc123"}, clear=True):
            self.assertTrue(cm.mercado_habilitado())

    def test_comparar_pronosticos_passthrough_sin_key(self):
        pron = [{"local": "A", "visitante": "B", "prob_local_pct": 50, "prob_empate_pct": 30, "prob_visitante_pct": 20}]
        with mock.patch.dict(os.environ, {}, clear=True):
            r = cm.comparar_pronosticos(pron)
        self.assertFalse(r["mercado_habilitado"])
        self.assertEqual(r["partidos_con_momios"], 0)
        self.assertIsNone(r["pronosticos"][0]["mercado"])

    def test_diagnostico_sin_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            d = cm.diagnostico_mercado()
        self.assertFalse(d["habilitado"])
        self.assertIn("nota", d)

    def test_auto_seleccion_elige_casas_con_mas_odds(self):
        activas = [{"name": n, "active": True} for n in ("Bet365", "Betano", "Caliente", "Codere")]

        def fake(url, p):
            if url.endswith("/bookmakers"):
                return activas
            if url.endswith("/events"):
                b = p.get("bookmaker")
                if b == "Betano":
                    return [{"id": 1}] * 9
                if b == "Caliente":
                    return [{"id": 2}] * 5
                return []
            return []

        cm._ACTIVAS_CACHE = None
        cm._CASAS_AUTO_CACHE = {"casas": None, "ts": None}
        with mock.patch.object(cm, "_get", side_effect=fake):
            casas = cm.casas_con_odds_liga("k")
        self.assertEqual(casas, ["Betano", "Caliente"])  # ordenadas por nº de partidos
        cm._ACTIVAS_CACHE = None
        cm._CASAS_AUTO_CACHE = {"casas": None, "ts": None}

    def test_bookmakers_override_por_env(self):
        with mock.patch.dict(os.environ, {"ODDS_API_IO_BOOKMAKERS": "Bet365,Pinnacle"}, clear=True):
            import importlib

            importlib.reload(cm)
            self.assertEqual(cm._bookmakers_consulta(), "Bet365,Pinnacle")
        importlib.reload(cm)  # restaurar estado del módulo


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPronosticoDesdeMomios(unittest.TestCase):
    def test_construye_desde_ml(self):
        merc = {"ml": {"local": 1.45, "empate": 4.2, "visita": 7.0}}
        pr = cm.pronostico_desde_momios("Necaxa", "Atlante", merc, "2026-07-17")
        self.assertEqual(pr["local"], "Necaxa")
        self.assertEqual(pr["pick_1x2"], "Gana Local")  # local muy favorito
        self.assertEqual(pr["fuente_pick"], "mercado")
        self.assertEqual(pr["nivel_confianza"], "SOLO MERCADO")
        # no-perder local = ganar + empatar; probabilidades suman ~100.
        self.assertAlmostEqual(pr["prob_local_pct"] + pr["prob_empate_pct"] + pr["prob_visitante_pct"], 100.0, places=0)
        self.assertGreater(pr["no_perder_local_pct"], pr["prob_local_pct"])

    def test_incluye_over_under_si_hay_totals(self):
        merc = {"ml": {"local": 2.0, "empate": 3.3, "visita": 3.5}, "totals": {"over": 1.9, "under": 1.9, "linea": 2.5}}
        pr = cm.pronostico_desde_momios("A", "B", merc)
        self.assertIn("pick_ou", pr)
        self.assertIn("prob_over_pct", pr)

    def test_sin_ml_devuelve_none(self):
        self.assertIsNone(cm.pronostico_desde_momios("A", "B", None))
        self.assertIsNone(cm.pronostico_desde_momios("A", "B", {"totals": {"over": 1.9, "under": 1.9}}))
