#!/usr/bin/env python3
"""Regresiones de robustez del cliente de Liga MX API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

from src import ligamx_api as api


def _match(i: int) -> dict:
    return {
        "id": i,
        "espn_event_id": str(900000 + i),
        "match_date": "2026-07-17T01:00:00Z",
        "home_team": {"name": f"Local {i}"},
        "away_team": {"name": f"Visita {i}"},
        "home_score": 1,
        "away_score": 0,
    }


def test_resultados_historicos_detiene_pagina_repetida_y_deduplica() -> None:
    pagina = [_match(i) for i in range(100)]
    with mock.patch.object(api, "obtener_partidos", side_effect=[pagina, pagina]) as cargar:
        resultados = api.resultados_historicos(max_partidos=5000)

    assert len(resultados) == 100
    assert len({r["match_key"] for r in resultados}) == 100
    assert cargar.call_count == 2


def test_calendario_tolera_jornada_none() -> None:
    payload = {
        "jornadas": [
            {
                "jornada": None,
                "matches": [
                    {
                        "date": "2026-07-17T01:00:00Z",
                        "home_team": {"name": "Necaxa"},
                        "away_team": {"name": "Puebla"},
                    }
                ],
            }
        ]
    }
    with mock.patch.object(api, "obtener_calendario", return_value=payload):
        calendario = api.calendario_para_planificador()

    assert calendario[0]["jornada"] == 0
    assert calendario[0]["partidos"][0]["home_team"] == "Necaxa"


def test_noticias_filtra_timestamp_naive_antiguo() -> None:
    antiguo = (datetime.now(timezone.utc) - timedelta(days=40)).replace(tzinfo=None).isoformat()
    reciente = (datetime.now(timezone.utc) - timedelta(days=2)).replace(tzinfo=None).isoformat()
    noticias = [
        {"title": "Necaxa noticia antigua", "description": "", "published_at": antiguo},
        {"title": "Necaxa noticia reciente", "description": "", "published_at": reciente},
    ]
    with mock.patch.object(api, "noticias", return_value=noticias):
        resultado = api.noticias_de_equipos(["Necaxa"], dias=30)

    assert [n["titulo"] for n in resultado] == ["Necaxa noticia reciente"]
