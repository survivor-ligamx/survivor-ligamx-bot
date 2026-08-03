"""`metricas_rendimiento` debe medir aciertos del 1X2, no rendimiento de apuestas.

El bug: leia la tabla `picks` (apuestas de valor con momio/EV/Kelly) y contaba
las filas con `result > 0`. Eso mezclaba dos cosas distintas y, como `result`
arranca en 0.0, los picks todavia sin liquidar entraban al denominador como
fallos.
"""

import unittest
from unittest import mock

from src import validacion_modelo


def _track(resueltos, aciertos_1x2, aciertos_marcador=0, pendientes=0):
    return {
        "resueltos": resueltos,
        "pendientes": pendientes,
        "aciertos_1x2": aciertos_1x2,
        "acierto_1x2_pct": round(100.0 * aciertos_1x2 / resueltos, 1) if resueltos else None,
        "aciertos_marcador_exacto": aciertos_marcador,
        "acierto_marcador_pct": round(100.0 * aciertos_marcador / resueltos, 1) if resueltos else None,
    }


class TestMetricasRendimiento(unittest.TestCase):
    def _correr(self, track):
        with mock.patch("src.database.rentabilidad_pronosticos", return_value=track):
            return validacion_modelo.metricas_rendimiento()

    def test_accuracy_sale_de_los_aciertos_1x2(self):
        m = self._correr(_track(resueltos=27, aciertos_1x2=13))
        self.assertAlmostEqual(m["accuracy_1x2"], round(13 / 27, 4))
        self.assertEqual(m["total_predicciones"], 27)

    def test_los_pendientes_no_entran_al_denominador(self):
        # 9 pronosticos aun sin resultado no deben contar como fallos.
        m = self._correr(_track(resueltos=10, aciertos_1x2=5, pendientes=9))
        self.assertEqual(m["accuracy_1x2"], 0.5)
        self.assertEqual(m["total_predicciones"], 10)
        self.assertEqual(m["pendientes"], 9)

    def test_tambien_reporta_el_marcador_exacto(self):
        m = self._correr(_track(resueltos=20, aciertos_1x2=10, aciertos_marcador=3))
        self.assertEqual(m["accuracy_marcador"], 0.15)

    def test_sin_pronosticos_resueltos_no_inventa_un_cero(self):
        m = self._correr(_track(resueltos=0, aciertos_1x2=0))
        self.assertIsNone(m["accuracy_1x2"])
        self.assertIsNone(m["accuracy_marcador"])
        self.assertEqual(m["total_predicciones"], 0)

    def test_si_la_base_falla_devuelve_la_forma_esperada(self):
        with mock.patch("src.database.rentabilidad_pronosticos", side_effect=RuntimeError("sin base")):
            m = validacion_modelo.metricas_rendimiento()
        self.assertIsNone(m["accuracy_1x2"])
        self.assertEqual(m["total_predicciones"], 0)
        self.assertIn("accuracy_por_jornada", m)


if __name__ == "__main__":
    unittest.main()
