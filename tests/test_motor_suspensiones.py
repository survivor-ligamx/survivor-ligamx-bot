#!/usr/bin/env python3
"""Cobertura del cableado de bajas en `motor_pronosticos.ajustar_por_alineaciones`.

Esta funcion no tenia ninguna prueba, y por eso el modulo `suspensiones`
pudo quedarse sin un solo call site durante varias jornadas: el ranking se
calculaba como si nadie estuviera sancionado y ningun test se quejaba.

Lo que se fija aqui es el CONTRATO, no los numeros del modelo:

1. El XI confirmado manda cuando existe.
2. Si no hay XI, se cae a los sancionados.
3. El mapa de sancionados se descarga UNA vez por jornada, no una por partido.
4. Nada de esto puede lanzar una excepcion hacia arriba.
"""

import os
import unittest
from typing import Any, Dict, List
from unittest import mock

from src import ligamx_api as lmx
from src import motor_pronosticos as motor

ENTORNO_CON_API = {"LIGAMX_API_URL": "http://api.local", "AJUSTE_XI": "1"}

SIN_XI: Dict[str, Any] = {"disponible": False, "equipos": {}}

IMPACTO_SUSPENSION: Dict[str, Any] = {
    "Pachuca": {"fuerza_xi_pct": 96.0, "ausentes_clave": ["Christian Rivera"], "motivo": "suspension"}
}

IMPACTO_XI: Dict[str, Any] = {
    "Pachuca": {"fuerza_xi_pct": 88.0, "ausentes_clave": ["Otro"], "motivo": "alineacion"}
}


def _pronostico(local: str = "Pachuca", visitante: str = "Puebla") -> Dict[str, Any]:
    return {
        "local": local,
        "visitante": visitante,
        "no_perder_local_pct": 85.5,
        "no_perder_visitante_pct": 40.0,
        "prob_local_pct": 58.8,
        "prob_empate_pct": 26.7,
        "prob_visitante_pct": 14.5,
    }


class TestCableadoSuspensiones(unittest.TestCase):
    """El motor debe consultar las sanciones cuando todavia no hay alineacion."""

    def test_cae_a_suspensiones_cuando_no_hay_xi(self):
        marcados: List[Dict[str, Any]] = []

        def _fake_ajustar(p, impacto_equipos=None):
            marcados.append(impacto_equipos or {})
            salida = dict(p)
            salida["ajuste"] = {"aplicado": True, "notas": ["sin Christian Rivera"]}
            return salida

        with mock.patch.dict(os.environ, ENTORNO_CON_API, clear=False):
            with mock.patch.object(lmx, "lineup_impact_partido", return_value=SIN_XI):
                with mock.patch.object(motor, "_mapa_suspendidos", return_value={"Pachuca": [{"nombre": "Rivera"}]}):
                    with mock.patch.object(motor, "_impacto_suspensiones", return_value=IMPACTO_SUSPENSION):
                        with mock.patch("src.ajuste_pronostico.ajustar_pronostico", side_effect=_fake_ajustar):
                            salida = motor.ajustar_por_alineaciones([_pronostico()])

        self.assertEqual(marcados, [IMPACTO_SUSPENSION])
        self.assertTrue(salida[0]["ajuste"]["aplicado"])

    def test_el_xi_confirmado_tiene_prioridad(self):
        """Con alineacion real no se consultan las sanciones: el XI ya las incluye."""
        recibidos: List[Dict[str, Any]] = []

        def _fake_ajustar(p, impacto_equipos=None):
            recibidos.append(impacto_equipos or {})
            return dict(p)

        con_xi = {"disponible": True, "equipos": IMPACTO_XI}
        with mock.patch.dict(os.environ, ENTORNO_CON_API, clear=False):
            with mock.patch.object(lmx, "lineup_impact_partido", return_value=con_xi):
                with mock.patch.object(motor, "_mapa_suspendidos", return_value={"Pachuca": [{}]}):
                    with mock.patch.object(motor, "_impacto_suspensiones") as sus_mock:
                        with mock.patch("src.ajuste_pronostico.ajustar_pronostico", side_effect=_fake_ajustar):
                            motor.ajustar_por_alineaciones([_pronostico()])

        sus_mock.assert_not_called()
        self.assertEqual(recibidos, [IMPACTO_XI])

    def test_el_mapa_se_descarga_una_sola_vez_por_jornada(self):
        """Nueve partidos, una peticion. Antes habria sido una por partido."""
        partidos = [_pronostico(f"Local{i}", f"Visita{i}") for i in range(9)]

        with mock.patch.dict(os.environ, ENTORNO_CON_API, clear=False):
            with mock.patch.object(lmx, "lineup_impact_partido", return_value=SIN_XI):
                with mock.patch.object(motor, "_mapa_suspendidos", return_value={}) as mapa_mock:
                    salida = motor.ajustar_por_alineaciones(partidos)

        mapa_mock.assert_called_once()
        self.assertEqual(len(salida), 9)

    def test_sin_sanciones_ni_xi_devuelve_los_pronosticos_intactos(self):
        with mock.patch.dict(os.environ, ENTORNO_CON_API, clear=False):
            with mock.patch.object(lmx, "lineup_impact_partido", return_value=SIN_XI):
                with mock.patch.object(motor, "_mapa_suspendidos", return_value={}):
                    salida = motor.ajustar_por_alineaciones([_pronostico()])

        self.assertEqual(salida[0]["no_perder_local_pct"], 85.5)
        self.assertNotIn("ajuste", salida[0])

    def test_si_la_api_de_xi_revienta_el_pronostico_sobrevive(self):
        """Una excepcion en un partido no puede tumbar la jornada entera."""
        with mock.patch.dict(os.environ, ENTORNO_CON_API, clear=False):
            with mock.patch.object(lmx, "lineup_impact_partido", side_effect=RuntimeError("API dormida")):
                with mock.patch.object(motor, "_mapa_suspendidos", return_value={}):
                    salida = motor.ajustar_por_alineaciones([_pronostico(), _pronostico("Necaxa", "Leon")])

        self.assertEqual(len(salida), 2)
        self.assertEqual(salida[1]["local"], "Necaxa")


class TestHelpersSuspensiones(unittest.TestCase):
    """Los dos helpers nuevos degradan a vacio en vez de propagar el fallo."""

    def test_mapa_vacio_si_la_api_falla(self):
        with mock.patch("src.suspensiones.suspendidos_por_equipo", side_effect=RuntimeError("dormida")):
            self.assertEqual(motor._mapa_suspendidos(), {})

    def test_impacto_vacio_si_el_mapa_esta_vacio(self):
        """Sin sancionados no se gasta ni una llamada."""
        with mock.patch("src.suspensiones.impacto_por_suspensiones") as imp_mock:
            self.assertEqual(motor._impacto_suspensiones("Pachuca", "Puebla", {}), {})
        imp_mock.assert_not_called()

    def test_impacto_vacio_si_no_esta_disponible(self):
        no_disp = {"disponible": False, "equipos": {}}
        with mock.patch("src.suspensiones.impacto_por_suspensiones", return_value=no_disp):
            self.assertEqual(motor._impacto_suspensiones("Pachuca", "Puebla", {"Pachuca": [{}]}), {})

    def test_impacto_devuelve_los_equipos_cuando_hay_sancionados(self):
        disp = {"disponible": True, "equipos": IMPACTO_SUSPENSION}
        with mock.patch("src.suspensiones.impacto_por_suspensiones", return_value=disp):
            salida = motor._impacto_suspensiones("Pachuca", "Puebla", {"Pachuca": [{}]})
        self.assertEqual(salida, IMPACTO_SUSPENSION)


if __name__ == "__main__":
    unittest.main()
