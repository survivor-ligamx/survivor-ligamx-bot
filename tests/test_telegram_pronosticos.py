#!/usr/bin/env python3
"""Tests para src/telegram_pronosticos.py. Sin red: envío y motor mockeados."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

# Asegurar que 'src' esté en el path para importar telegram_pronosticos
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.telegram_pronosticos as tp  # noqa: E402


def _resultado():
    return {
        "generado_utc": "2026-07-16T10:00:00Z",
        "fuente_datos": "ESPN",
        "total_pronosticos": 1,
        "pronosticos": [
            {
                "local": "América",
                "visitante": "Toluca",
                "pick_1x2": "Gana Local",
                "prob_local_pct": 55.0,
                "prob_empate_pct": 25.0,
                "prob_visitante_pct": 20.0,
                "pick_ou": "Over",
                "prob_over_pct": 60.0,
                "pick_btts": "Sí",
                "prob_btts_si_pct": 55.0,
                "marcador_mas_probable": "2-1",
                "no_perder_local_pct": 80.0,
                "no_perder_visitante_pct": 45.0,
            }
        ],
        "decision": "INFORMATIVO / REVISIÓN HUMANA",
    }


class TestConstruirMensaje(unittest.TestCase):
    def test_incluye_partido_y_survivor(self):
        msg = tp.construir_mensaje(_resultado())
        self.assertIn("<b>América</b> 🏠 vs <b>Toluca</b>", msg)
        self.assertIn("SURVIVOR", msg)
        self.assertIn("🥇", msg)  # ranking top-3
        self.assertIn("🎯 Pick: <b>América</b>", msg)
        self.assertNotIn("Gana Local", msg)

    def test_incluye_disclaimer(self):
        msg = tp.construir_mensaje(_resultado())
        self.assertIn("No es consejo de apuesta", msg)

    def test_sin_pronosticos(self):
        msg = tp.construir_mensaje({"pronosticos": [], "fuente_datos": "cache"})
        self.assertIn("Sin pronósticos", msg)
        self.assertIn("No es consejo de apuesta", msg)

    def test_no_recomienda_apostar_ni_ev_falso(self):
        msg = tp.construir_mensaje(_resultado())
        self.assertNotIn("Actúa rápido", msg)
        self.assertNotIn("Kelly", msg)

    def test_incluye_contexto_api_si_se_pasa(self):
        ctx = {
            "home": "América",
            "away": "Toluca",
            "prediccion_api": {
                "prob_local_pct": 55.0,
                "prob_empate_pct": 25.0,
                "prob_visita_pct": 20.0,
                "goles_esp": "1.8-1.0",
            },
            "forma_local": "WWDLW",
            "forma_visita": "LDLWD",
            "en_riesgo_local": ["Jugador X"],
            "en_riesgo_visita": [],
            "h2h": None,
        }
        msg = tp.construir_mensaje(_resultado(), contexto_pick=ctx)
        self.assertIn("Contexto (Liga MX API)", msg)
        self.assertIn("2ª opinión API", msg)
        self.assertIn("En riesgo", msg)
        self.assertIn("Jugador X", msg)


class TestEnviar(unittest.TestCase):
    def test_sin_credenciales_no_envia(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
            self.assertFalse(tp.enviar_mensaje("hola"))

    def test_envia_con_credenciales(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}, clear=False):
            with mock.patch("src.telegram.envio.requests") as mreq:
                mreq.post.return_value = mock.Mock(status_code=200)
                self.assertTrue(tp.enviar_mensaje("hola"))

    def test_enviar_pronosticos_flujo(self):
        with (
            mock.patch("src.telegram.envio.motor.generar_pronosticos", return_value=_resultado()),
            mock.patch("src.telegram.envio.motor.motivacion_por_equipo", return_value={}),
            mock.patch("src.telegram.envio._plan_temporada", return_value={}),
            mock.patch("src.ligamx_api.disponible", return_value=True),
            mock.patch("src.ligamx_api.goleadores_por_equipo", return_value={}),
            mock.patch("src.ligamx_api.porteros_por_equipo", return_value={}),
            mock.patch("src.telegram.envio._contexto_top_pick", return_value=None),
            mock.patch("src.telegram.envio._partidos_jugados_torneo", return_value=100),
            mock.patch("src.telegram.envio._registrar_survivor_historial"),
            mock.patch("src.telegram.envio.enviar_mensaje", return_value=True) as menv,
        ):
            r = tp.enviar_pronosticos()
        self.assertTrue(r["enviado"])
        self.assertEqual(r["total_pronosticos"], 1)
        menv.assert_called_once()

    def test_enviar_pronosticos_pick_viene_del_plan(self):
        res = _resultado()
        plan = {
            "plan": [
                {
                    "jornada": "1",
                    "equipo": "Toluca",
                    "rival": "América",
                    "condicion": "Visitante",
                    "no_perder_pct": 70.0,
                    "prob_ganar_pct": 45.0,
                }
            ]
        }
        with (
            mock.patch("src.telegram.envio.motor.generar_pronosticos", return_value=res),
            mock.patch("src.telegram.envio.motor.motivacion_por_equipo", return_value={}),
            mock.patch("src.telegram.envio._plan_temporada", return_value=plan),
            mock.patch("src.telegram.envio._jornada_actual_num", return_value=1),
            mock.patch("src.ligamx_api.disponible", return_value=False),
            mock.patch("src.telegram.envio._partidos_jugados_torneo", return_value=100),
            mock.patch("src.telegram.envio._registrar_survivor_historial"),
            mock.patch("src.telegram.envio.enviar_mensaje", return_value=True) as menv,
        ):
            tp.enviar_pronosticos()
        msg = menv.call_args[0][0]
        self.assertIn("PICK: Toluca", msg)
        self.assertIn("Gana: <b>45%", msg)


class TestHelpersPlanReal(unittest.TestCase):
    def test_jornada_actual_num_usa_calendario_real(self):
        self.assertEqual(tp._jornada_actual_num(date(2026, 7, 22)), 2)

    def test_historial_usa_clave_unica_por_torneo(self):
        from src import database
        from src.telegram import envio

        pick = {
            "jornada": 2,
            "equipo": "América",
            "rival": "Toluca",
            "condicion": "Local",
            "no_perder_pct": 75.0,
            "prob_victoria_pct": 55.0,
        }
        pronostico = {
            "local": "América",
            "visitante": "Toluca",
            "fecha": "2026-07-22",
        }
        with mock.patch.object(database, "registrar_survivor_pick", return_value=True) as registrar:
            envio._registrar_survivor_historial([pick], [pronostico])

        self.assertEqual(registrar.call_args.kwargs["jornada"], "Apertura-2026-J2")

    def test_plan_temporada_usa_api_real_del_planificador(self):
        from src import fuentes_datos
        from src import planificador_survivor as plan_mod
        from src import poisson_model as pm

        calendario = [
            {"jornada": 1, "partidos": []},
            {"jornada": 2, "partidos": []},
        ]
        calendario_vigente = [calendario[1]]
        esperado = {"plan": [{"jornada": 2, "equipo": "América"}], "calendario_incompleto": False}
        with (
            mock.patch.object(plan_mod, "cargar_calendario", return_value=calendario),
            mock.patch.object(fuentes_datos, "leer_cache", return_value=[]),
            mock.patch.object(fuentes_datos, "obtener_resultados", return_value={"resultados": [{"real": True}]}),
            mock.patch.object(pm, "calcular_fuerzas", return_value={"equipos": {}}),
            mock.patch.object(plan_mod, "construir_odds_por_partido", return_value={}),
            mock.patch.object(
                plan_mod,
                "preparar_calibracion_segura",
                return_value={"parametros_planificador": {"aplicar": False, "alpha": 0.0, "base": (1 / 3, 1 / 3, 1 / 3)}},
            ),
            mock.patch.object(plan_mod, "planificar", return_value=esperado) as planificar,
        ):
            resultado = tp._plan_temporada(["Toluca"], jornada_desde=2)

        self.assertEqual(resultado, esperado)
        planificar.assert_called_once_with(
            calendario_vigente,
            {"equipos": {}},
            equipos_usados=["Toluca"],
            peso_victoria=0.5,
            odds_por_partido={},
            vida_empate_consumida=False,
            calibracion={"aplicar": False, "alpha": 0.0, "base": (1 / 3, 1 / 3, 1 / 3)},
        )

    def test_plan_no_repite_descarga_si_el_motor_dejo_cache_vacio(self):
        from src import fuentes_datos
        from src import planificador_survivor as plan_mod

        with (
            mock.patch.object(plan_mod, "cargar_calendario", return_value=[{"jornada": 2, "partidos": []}]),
            mock.patch.object(fuentes_datos, "leer_cache", return_value=[]),
            mock.patch.object(fuentes_datos, "obtener_resultados") as descargar,
        ):
            resultado = tp._plan_temporada([], jornada_desde=2, permitir_descarga=False)

        self.assertEqual(resultado["plan"], [])
        self.assertIn("caché", resultado["error"])
        descargar.assert_not_called()

    def test_plan_no_reabre_jornadas_al_finalizar_temporada(self):
        from src import planificador_survivor as plan_mod
        from src.telegram import envio

        with (
            mock.patch.object(plan_mod, "cargar_calendario", return_value=[{"jornada": 17, "partidos": []}]),
            mock.patch.object(envio, "_jornada_actual_num", return_value=None),
        ):
            resultado = envio._plan_temporada([])

        self.assertEqual(resultado["plan"], [])
        self.assertTrue(resultado["temporada_finalizada"])
        self.assertIn("temporada ya finalizó", tp.construir_mensaje_plan(resultado))


class TestNivelRiesgoYPlan(unittest.TestCase):
    def test_enviar_plan_sin_calendario(self):
        import src.planificador_survivor as ps

        with mock.patch.object(ps, "cargar_calendario", return_value=[]):
            with mock.patch("src.telegram.envio.enviar_mensaje", return_value=True) as menv:
                r = tp.enviar_plan()
        self.assertTrue(r["calendario_incompleto"])
        menv.assert_called_once()


class TestFormatoMovil(unittest.TestCase):
    def test_pct_sin_decimales(self):
        self.assertEqual(tp._pct(55.0), "55")
        self.assertEqual(tp._pct(57.6), "58")

    def test_fecha_mx_convierte_y_fallback(self):
        self.assertIn("04:00", tp._fecha_mx("2026-07-16T10:00:00Z"))


class TestDividirMensaje(unittest.TestCase):
    def test_enviar_mensaje_largo_manda_varios(self):
        largo = "\n".join(f"linea {i} " + "y" * 60 for i in range(400))
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}, clear=False):
            with mock.patch("src.telegram.envio.requests") as mreq:
                mreq.post.return_value = mock.Mock(status_code=200)
                ok = tp.enviar_mensaje(largo)
        self.assertTrue(ok)
        self.assertGreater(mreq.post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
