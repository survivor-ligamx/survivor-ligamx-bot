#!/usr/bin/env python3
"""Regresiones de integridad para marcadores ESPN y combinación de fuentes."""

from __future__ import annotations

from unittest import mock

import pytest

from src import espn_data as ed
from src import fuentes_datos as fd


def _evento(home: str, away: str, hg: object, ag: object, fecha: str = "2026-02-07T01:00Z") -> dict:
    return {
        "date": fecha,
        "status": {"type": {"name": ed.ESTADO_FINAL}},
        "competitions": [
            {
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": home}, "score": hg},
                    {"homeAway": "away", "team": {"displayName": away}, "score": ag},
                ]
            }
        ],
    }


@pytest.mark.parametrize("valor", ["2", "2.0", 2, 2.0, " 2.0 "])
def test_espn_acepta_marcadores_enteros_equivalentes(valor: object) -> None:
    partido = ed.parsear_eventos({"events": [_evento("Necaxa", "Puebla", valor, "1.0")]})[0]

    assert partido["jugado"] is True
    assert partido["home_goals"] == 2
    assert partido["away_goals"] == 1


@pytest.mark.parametrize("valor", [None, "", "abc", "2.5", -1, "-1.0"])
def test_espn_rechaza_marcadores_invalidos_o_fraccionarios(valor: object) -> None:
    partido = ed.parsear_eventos({"events": [_evento("Necaxa", "Puebla", valor, "1")]})[0]

    assert partido["jugado"] is False
    assert "home_goals" not in partido
    assert "away_goals" not in partido


def test_resultado_decimal_de_espn_no_se_descarta() -> None:
    data = {"events": [_evento("Necaxa", "Puebla", "2.0", "1.0")]}
    with mock.patch.object(ed, "_fetch_scoreboard", return_value=data):
        resultados = ed.obtener_resultados(meses=1)

    assert len(resultados) == 1
    assert resultados[0]["home_goals"] == 2
    assert resultados[0]["away_goals"] == 1


def _r(home: str, away: str, fecha: str, hg: int = 1, ag: int = 0) -> dict:
    return {
        "home_team": home,
        "away_team": away,
        "home_goals": hg,
        "away_goals": ag,
        "fecha": fecha,
    }


def test_combinar_deduplica_aliases_y_conserva_prioridad_de_primera_fuente() -> None:
    primario = _r("Pumas UNAM", "Club América", "2026-02-01", 2, 1)
    duplicado = _r("Pumas", "America", "2026-02-01T23:00:00Z", 0, 0)

    combinado = fd._combinar([primario], [duplicado])

    assert combinado == [primario]


def test_combinar_deduplica_aliases_adicionales() -> None:
    primario = _r("Tigres UANL", "Club Tijuana", "2026-02-02")
    duplicado = _r("Tigres", "Tijuana", "2026-02-02")

    assert fd._combinar([primario], [duplicado]) == [primario]


def test_combinar_conserva_mismos_equipos_en_fechas_distintas() -> None:
    jornada_a = _r("Pumas UNAM", "América", "2026-02-01")
    jornada_b = _r("Pumas", "America", "2026-05-10")

    assert fd._combinar([jornada_a], [jornada_b]) == [jornada_a, jornada_b]


def test_combinar_no_intercambia_local_y_visitante() -> None:
    ida = _r("Pumas UNAM", "América", "2026-02-01")
    localia_invertida = _r("America", "Pumas", "2026-02-01")

    assert fd._combinar([ida], [localia_invertida]) == [ida, localia_invertida]
