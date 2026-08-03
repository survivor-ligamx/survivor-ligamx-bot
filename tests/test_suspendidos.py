#!/usr/bin/env python3
"""
Tests de `suspensiones`: las bajas por sanción como señal de ajuste.

Sin red: se parchea el cliente HTTP. Lo que se protege aquí es que el motor
deje de ir ciego durante la semana previa a la jornada, que es cuando hay que
elegir el pick del Survivor.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ajuste_pronostico as aj  # noqa: E402
import ligamx_api as lmx  # noqa: E402
import suspensiones as sus  # noqa: E402


_DISCIPLINE = {
    "season": "Apertura 2026",
    "count": 3,
    "players": [
        {"player": "Duk", "team": "Atlas", "team_id": 216, "suspended_next_match": True, "suspension_reason": "roja"},
        {
            "player": "Milton Valenzuela",
            "team": "Atlas",
            "team_id": 216,
            "suspended_next_match": True,
            "suspension_reason": "roja",
        },
        # En riesgo pero HABILITADO: no debe contar como baja.
        {
            "player": "Cuarta Amarilla",
            "team": "Tigres UANL",
            "team_id": 232,
            "suspended_next_match": False,
            "suspension_risk": True,
        },
    ],
}


def _pron_atlas_tigres():
    return {
        "local": "Atlas",
        "visitante": "Tigres UANL",
        "prob_local_pct": 35.72,
        "prob_empate_pct": 30.0,
        "prob_visitante_pct": 34.28,
        "goles_esperados_local": 1.302,
        "goles_esperados_visitante": 1.294,
        "pick_1x2": "Gana Local",
        "prob_pick_pct": 35.72,
        "nivel_confianza": "BAJA",
        "no_perder_local_pct": 65.72,
        "no_perder_visitante_pct": 64.28,
    }


class TestLecturaDeSancionados(unittest.TestCase):
    def test_solo_los_inhabilitados_entran(self):
        with mock.patch.object(lmx, "_get", return_value=_DISCIPLINE):
            filas = sus.suspendidos_liga()
        nombres = [f["player"] for f in filas]
        self.assertEqual(nombres, ["Duk", "Milton Valenzuela"])
        self.assertNotIn("Cuarta Amarilla", nombres)  # en riesgo != inhabilitado

    def test_pide_unavailable_y_limite_amplio(self):
        # El default del endpoint es limit=20 y trunca `players`: hay que pedir mas.
        with mock.patch.object(lmx, "_get", return_value=_DISCIPLINE) as g:
            sus.suspendidos_liga()
        _, kwargs = g.call_args
        params = kwargs.get("params") or g.call_args[0][1]
        self.assertEqual(params["unavailable"], "true")
        self.assertEqual(params["limit"], 50)

    def test_agrupa_por_equipo(self):
        with mock.patch.object(lmx, "_get", return_value=_DISCIPLINE):
            mapa = sus.suspendidos_por_equipo()
        self.assertEqual(len(mapa["Atlas"]), 2)
        self.assertNotIn("Tigres UANL", mapa)

    def test_api_caida_no_rompe(self):
        with mock.patch.object(lmx, "_get", side_effect=RuntimeError("dormida")):
            self.assertEqual(sus.suspendidos_por_equipo(), {})


class TestDeficit(unittest.TestCase):
    def test_escala_por_baja(self):
        self.assertEqual(sus.deficit_por_bajas(0), 0.0)
        self.assertEqual(sus.deficit_por_bajas(1), 4.0)
        self.assertEqual(sus.deficit_por_bajas(2), 8.0)

    def test_tiene_tope(self):
        # Sin tope, media plantilla sancionada anularía al equipo.
        self.assertEqual(sus.deficit_por_bajas(99), sus.MAX_DEFICIT_PCT)


class TestImpactoPorSuspensiones(unittest.TestCase):
    def test_contrato_igual_al_de_lineup_impact(self):
        with mock.patch.object(lmx, "_get", return_value=_DISCIPLINE):
            imp = sus.impacto_por_suspensiones("Atlas", "Tigres UANL")
        self.assertTrue(imp["disponible"])
        self.assertEqual(imp["fuente"], "suspensiones")
        atlas = imp["equipos"]["Atlas"]
        self.assertEqual(atlas["fuerza_xi_pct"], 92.0)  # 100 - 2*4
        self.assertIn("Duk", atlas["ausentes_clave"])
        self.assertNotIn("Tigres UANL", imp["equipos"])  # sin bajas, sin castigo

    def test_sin_sancionados_no_esta_disponible(self):
        with mock.patch.object(lmx, "_get", return_value={"players": []}):
            imp = sus.impacto_por_suspensiones("Pumas UNAM", "Querétaro")
        self.assertFalse(imp["disponible"])
        self.assertEqual(imp["equipos"], {})

    def test_reusa_el_mapa_sin_volver_a_pegarle_a_la_api(self):
        mapa = {"Atlas": [{"nombre": "Duk", "motivo": "roja"}]}
        with mock.patch.object(lmx, "_get", side_effect=AssertionError("no debía llamar")):
            imp = sus.impacto_por_suspensiones("Atlas", "Tigres UANL", mapa)
        self.assertEqual(imp["equipos"]["Atlas"]["fuerza_xi_pct"], 96.0)


class TestFallbackDeLineupImpact(unittest.TestCase):
    def test_con_xi_publicado_manda_el_xi(self):
        payload = {"disponible": True, "equipos": {"Atlas": {"fuerza_xi_pct": 70.0}}}
        with (
            mock.patch.object(lmx, "evento_365_id", return_value=99),
            mock.patch.object(lmx, "lineup_impact", return_value=payload),
            mock.patch.object(sus, "impacto_por_suspensiones", side_effect=AssertionError("no debía caer")),
        ):
            r = lmx.lineup_impact_partido("Atlas", "Tigres UANL")
        self.assertEqual(r["equipos"]["Atlas"]["fuerza_xi_pct"], 70.0)

    def test_sin_xi_cae_a_las_sanciones(self):
        # Doce días antes del partido no hay XI, pero las rojas ya son firmes.
        with (
            mock.patch.object(lmx, "evento_365_id", return_value=None),
            mock.patch.object(lmx, "_get", return_value=_DISCIPLINE),
        ):
            r = lmx.lineup_impact_partido("Atlas", "Tigres UANL")
        self.assertTrue(r["disponible"])
        self.assertEqual(r["fuente"], "suspensiones")
        self.assertEqual(r["equipos"]["Atlas"]["fuerza_xi_pct"], 92.0)

    def test_sin_xi_y_sin_sanciones_devuelve_vacio(self):
        with (
            mock.patch.object(lmx, "evento_365_id", return_value=None),
            mock.patch.object(lmx, "_get", return_value={"players": []}),
        ):
            self.assertEqual(lmx.lineup_impact_partido("Pumas UNAM", "Querétaro"), {})


class TestEfectoEnElPronostico(unittest.TestCase):
    def test_atlas_con_dos_rojas_baja_su_probabilidad(self):
        base = _pron_atlas_tigres()
        with mock.patch.object(lmx, "_get", return_value=_DISCIPLINE):
            imp = sus.impacto_por_suspensiones("Atlas", "Tigres UANL")
        r = aj.ajustar_pronostico(base, impacto_equipos=imp["equipos"])
        self.assertTrue(r["ajuste"]["aplicado"])
        self.assertLess(r["prob_local_pct"], base["prob_local_pct"])
        self.assertLess(r["goles_esperados_local"], base["goles_esperados_local"])

    def test_la_nota_nombra_a_los_ausentes(self):
        with mock.patch.object(lmx, "_get", return_value=_DISCIPLINE):
            imp = sus.impacto_por_suspensiones("Atlas", "Tigres UANL")
        r = aj.ajustar_pronostico(_pron_atlas_tigres(), impacto_equipos=imp["equipos"])
        nota = " ".join(r["ajuste"]["notas"])
        self.assertIn("Duk", nota)
        self.assertIn("sancionados", nota)

    def test_sigue_siendo_un_empujon_no_un_vuelco(self):
        # Aun con el tope de bajas, el ajuste no puede pasar del 15% del CAP.
        base = _pron_atlas_tigres()
        imp = {"Atlas": {"fuerza_xi_pct": 100.0 - sus.MAX_DEFICIT_PCT, "motivo": "suspension"}}
        r = aj.ajustar_pronostico(base, impacto_equipos=imp)
        recorte = 1.0 - (r["goles_esperados_local"] / base["goles_esperados_local"])
        self.assertLessEqual(recorte, aj.CAP_LINEUP + 1e-9)


if __name__ == "__main__":
    unittest.main()
