#!/usr/bin/env python3
"""Tests para src/planificador_survivor.py (estrategia de temporada). Sin red."""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import poisson_model as pm  # noqa: E402
import planificador_survivor as ps  # noqa: E402


def _historico():
    """Resultados sintéticos: A fuerte, D débil; suficientes para estimar fuerzas."""
    out = []
    d0 = date(2025, 8, 4)
    pares = [
        ("A", "D", 3, 0),
        ("A", "C", 2, 0),
        ("B", "D", 2, 1),
        ("C", "D", 1, 1),
        ("A", "B", 2, 1),
        ("B", "C", 1, 0),
        ("D", "C", 0, 1),
        ("C", "A", 0, 2),
        ("D", "A", 0, 3),
        ("B", "A", 1, 2),
        ("C", "B", 0, 1),
        ("D", "B", 1, 1),
    ]
    for w, (h, a, hg, ag) in enumerate(pares):
        out.append(
            {
                "home_team": h,
                "away_team": a,
                "home_goals": hg,
                "away_goals": ag,
                "fecha": (d0 + timedelta(days=7 * w)).isoformat(),
            }
        )
    return out


def _calendario():
    return [
        {"jornada": 1, "partidos": [{"home_team": "A", "away_team": "D"}, {"home_team": "B", "away_team": "C"}]},
        {"jornada": 2, "partidos": [{"home_team": "C", "away_team": "A"}, {"home_team": "D", "away_team": "B"}]},
    ]


class TestMomios(unittest.TestCase):
    def test_prob_implicita_americana(self):
        self.assertAlmostEqual(ps.prob_implicita_americana(-125), 0.5556, places=3)
        self.assertAlmostEqual(ps.prob_implicita_americana(110), 0.4762, places=3)
        with self.assertRaises(ValueError):
            ps.prob_implicita_americana(0)

    def test_devig_suma_uno(self):
        pl, pe, pv = ps.devig_americano(-125, 280, 350)
        self.assertAlmostEqual(pl + pe + pv, 1.0, places=6)
        self.assertTrue(0 < pl < 1 and 0 < pe < 1 and 0 < pv < 1)


class TestPlanificador(unittest.TestCase):
    def setUp(self):
        self.fuerzas = pm.calcular_fuerzas(_historico())

    def test_plan_sin_repetir(self):
        r = ps.planificar(_calendario(), self.fuerzas)
        equipos = [p["equipo"] for p in r["plan"]]
        self.assertEqual(len(equipos), len(set(equipos)))  # no repite
        self.assertEqual(len(r["plan"]), 2)  # una por jornada

    def test_estructura_y_metricas(self):
        r = ps.planificar(_calendario(), self.fuerzas)
        for k in (
            "plan",
            "prob_supervivencia_total_pct",
            "victorias_esperadas",
            "jornadas_riesgosas",
            "equipos_no_usados",
            "decision",
        ):
            self.assertIn(k, r)
        self.assertTrue(0 < r["prob_supervivencia_total_pct"] <= 100)
        for p in r["plan"]:
            self.assertIn(p["nivel"], {"ALTA", "MEDIA", "RIESGOSA"})
            self.assertAlmostEqual(
                p["prob_ganar_pct"] + p["prob_empate_pct"] + p["prob_perder_pct"],
                100.0,
                delta=0.2,
            )
            self.assertIn("supervivencia_inmediata_con_vida_pct", p)
            self.assertIn("supervivencia_inmediata_sin_vida_pct", p)
            self.assertIn("riesgo_consumir_vida_pct", p)
            self.assertIn("vida_empate_disponible_asumida", p)

    def test_excluye_equipos_usados(self):
        r = ps.planificar(_calendario(), self.fuerzas, equipos_usados=["A"])
        equipos_norm = {pm._norm(p["equipo"]) for p in r["plan"]}
        self.assertNotIn("a", equipos_norm)

    def test_peso_victoria_cero_solo_sobrevive(self):
        # No debe romper con peso_victoria=0 (solo maximiza no-perder).
        r = ps.planificar(_calendario(), self.fuerzas, peso_victoria=0.0)
        self.assertEqual(len(r["plan"]), 2)

    def test_planificador_cambia_pick_si_vida_ya_fue_consumida(self):
        calendario = [
            {
                "jornada": 1,
                "partidos": [
                    {"home_team": "A", "away_team": "C"},
                    {"home_team": "B", "away_team": "D"},
                ],
            },
            {
                "jornada": 2,
                "partidos": [
                    {"home_team": "A", "away_team": "D"},
                    {"home_team": "B", "away_team": "C"},
                ],
            },
        ]
        probs = {
            ("A", "C"): (0.35, 0.55, 0.10),
            ("B", "D"): (0.60, 0.05, 0.35),
            ("A", "D"): (0.80, 0.00, 0.20),
            ("B", "C"): (0.55, 0.35, 0.10),
        }

        def _fake_probs(home, away, *_args, **_kwargs):
            return probs[(home, away)]

        with mock.patch("planificador_survivor._probs_partido", side_effect=_fake_probs):
            con_vida = ps.planificar(calendario, {"equipos": {}}, vida_empate_consumida=False, descuento_visitante=0.0)
            sin_vida = ps.planificar(calendario, {"equipos": {}}, vida_empate_consumida=True, descuento_visitante=0.0)

        self.assertEqual(con_vida["plan"][0]["equipo"], "A")
        self.assertEqual(sin_vida["plan"][0]["equipo"], "B")
        self.assertFalse(con_vida["vida_empate_inicial_consumida"])
        self.assertTrue(sin_vida["vida_empate_inicial_consumida"])

    def test_probabilidad_total_no_infla_con_dos_empates_seguidos(self):
        calendario = [
            {"jornada": 1, "partidos": [{"home_team": "A", "away_team": "C"}]},
            {"jornada": 2, "partidos": [{"home_team": "B", "away_team": "D"}]},
        ]

        with mock.patch("planificador_survivor._probs_partido", return_value=(0.0, 1.0, 0.0)):
            r = ps.planificar(calendario, {"equipos": {}}, descuento_visitante=0.0)

        self.assertEqual(len(r["plan"]), 2)
        self.assertEqual(r["prob_supervivencia_total_pct"], 0.0)
        self.assertEqual(r["plan"][0]["riesgo_consumir_vida_pct"], 100.0)
        self.assertEqual(r["plan"][1]["vida_empate_disponible_asumida"], False)

    def test_probabilidad_total_es_cero_si_no_hay_forma_de_ganar(self):
        calendario = [
            {"jornada": 1, "partidos": [{"home_team": "A", "away_team": "C"}]},
        ]
        with mock.patch("planificador_survivor._probs_partido", return_value=(0.0, 1.0, 0.0)):
            r = ps.planificar(calendario, {"equipos": {}}, vida_empate_consumida=True, descuento_visitante=0.0)

        self.assertEqual(r["prob_supervivencia_total_pct"], 0.0)


class TestCalibracionPlanificador(unittest.TestCase):
    def test_aplica_solo_si_mejora_walkforward(self):
        resultados = [{"home_goals": 1, "away_goals": 0}]
        reporte = {
            "alpha_sugerido": 0.2,
            "calibracion_ayuda": True,
            "n_muestras": 30,
        }
        with mock.patch("src.calibracion.evaluar_calibracion", return_value=reporte):
            meta = ps.preparar_calibracion_segura(resultados)
        self.assertTrue(meta["aplicada"])
        self.assertEqual(meta["parametros_planificador"]["aplicar"], True)

    def test_fallback_si_no_hay_muestras(self):
        resultados: list[dict[str, int]] = []
        reporte = {"n_muestras": 0, "mensaje": "Muestras insuficientes para calibrar.", "decision": "INFORMATIVO"}
        with mock.patch("src.calibracion.evaluar_calibracion", return_value=reporte):
            meta = ps.preparar_calibracion_segura(resultados)
        self.assertFalse(meta["aplicada"])
        self.assertEqual(meta["parametros_planificador"]["aplicar"], False)


class TestCargarCalendario(unittest.TestCase):
    def test_archivo_inexistente_usa_fallback_inline(self):
        cal = ps.cargar_calendario(Path("/no/existe/x.json"))
        self.assertEqual(len(cal), 17)  # fallback inline: 17 jornadas
        self.assertTrue(all("jornada" in j and j["partidos"] for j in cal))


class TestOddsPorPartido(unittest.TestCase):
    def test_construir_desde_momios_inyectados(self):
        import comparador_mercado as cm

        cal = [{"jornada": 1, "partidos": [{"home_team": "América", "away_team": "Toluca"}]}]
        clave = cm._clave_partido("América", "Toluca")
        # Local muy favorito (momio bajo) => prob_local mayor.
        momios = {clave: {"ml": {"local": 1.5, "empate": 4.0, "visita": 6.0}}}
        odds = ps.construir_odds_por_partido(cal, momios_crudos=momios)
        key = (ps._norm("América"), ps._norm("Toluca"))
        self.assertIn(key, odds)
        pl, pe, pv = odds[key]
        self.assertAlmostEqual(pl + pe + pv, 1.0, places=6)
        self.assertGreater(pl, pv)

    def test_sin_momios_devuelve_vacio(self):
        cal = [{"jornada": 1, "partidos": [{"home_team": "A", "away_team": "B"}]}]
        self.assertEqual(ps.construir_odds_por_partido(cal, momios_crudos={}), {})

    def test_planificar_con_odds_no_rompe(self):
        import comparador_mercado as cm

        fuerzas = pm.calcular_fuerzas(_historico())
        cal = _calendario()
        clave = cm._clave_partido("A", "D")
        momios = {clave: {"ml": {"local": 1.4, "empate": 4.5, "visita": 7.0}}}
        odds = ps.construir_odds_por_partido(cal, momios_crudos=momios)
        r = ps.planificar(cal, fuerzas, odds_por_partido=odds)
        self.assertEqual(len(r["plan"]), 2)


if __name__ == "__main__":
    unittest.main()


def test_plan_expone_politica_adaptativa_y_complejidad_acotada():
    fuerzas = ps.pm.calcular_fuerzas(_historico())
    r = ps.planificar(_calendario(), fuerzas)
    assert r["tipo_plan"] == "politica_adaptativa_por_estado_de_vida"
    assert "política adaptativa" in r["nota_plan"]
    assert 0 < r["estados_dp_evaluados"] <= 2 * (2 ** r["equipos_disponibles"])
