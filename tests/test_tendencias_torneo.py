from unittest import mock

import pytest

from src import tendencias_torneo as tt
from src.team_normalizer import canonical_team_key


def _resultados():
    return [
        {"fecha": "2026-07-10", "home_team": "Unders FC", "away_team": "Grande", "home_goals": 2, "away_goals": 1},
        {"fecha": "2026-07-11", "home_team": "Débil", "away_team": "Sólido", "home_goals": 0, "away_goals": 1},
        {"fecha": "2026-07-17", "home_team": "Rival", "away_team": "Unders FC", "home_goals": 1, "away_goals": 2},
        {"fecha": "2026-07-18", "home_team": "Grande", "away_team": "Débil", "home_goals": 0, "away_goals": 1},
        {"fecha": "2026-07-19", "home_team": "Sólido", "away_team": "Rival", "home_goals": 2, "away_goals": 0},
    ]


def test_metricas_ventanas_y_condicion():
    tendencias = tt.calcular_tendencias(_resultados())
    under = tendencias[canonical_team_key("Unders FC")]
    assert under["ventanas"]["3"]["pj"] == 2
    assert under["ventanas"]["5"]["pg"] == 2
    assert under["ventanas"]["5"]["gf_pp"] == 2.0
    assert under["ventanas"]["5"]["recibe_pct"] == 100.0
    assert under["local"]["pj"] == 1
    assert under["visitante"]["pj"] == 1
    assert under["ventanas"]["5"]["racha_invicto"] == 2


def test_detecta_sorpresa_ataque_y_favorito_en_baja():
    fortalezas = {
        canonical_team_key("Unders FC"): 0.9,
        canonical_team_key("Grande"): 1.2,
    }
    tendencias = tt.calcular_tendencias(_resultados(), fortalezas)
    assert "EQUIPO_SORPRESA" in tendencias[canonical_team_key("Unders FC")]["etiquetas"]
    assert "ATAQUE_EN_FORMA" in tendencias[canonical_team_key("Unders FC")]["etiquetas"]
    assert "FAVORITO_EN_BAJA" in tendencias[canonical_team_key("Grande")]["etiquetas"]


def test_arranque_regulariza_y_limita_la_senal():
    tendencias = tt.calcular_tendencias(_resultados())
    under = tendencias[canonical_team_key("Unders FC")]
    assert under["muestra_preliminar"] is True
    assert under["peso_actual"] == pytest.approx(0.2)
    assert abs(under["senal"]) < 0.02
    assert abs(under["senal"]) <= tt.MAX_AJUSTE


def test_ajuste_1x2_renormaliza_y_es_pequeno():
    buena = {"senal": 0.03, "razones": ["ataque en forma"]}
    mala = {"senal": -0.02, "razones": ["defensa vulnerable"]}
    ajuste = tt.ajustar_probabilidades([0.5, 0.25, 0.25], buena, mala)
    assert sum(ajuste["ajustadas"]) == pytest.approx(1.0)
    assert ajuste["ajustadas"][0] > ajuste["base"][0]
    assert abs(ajuste["cambio_local_pp"]) < 3.0
    assert ajuste["razones"]


def test_ajustar_fuerzas_no_muta_el_original():
    fuerzas = {
        "avg_home": 1.4,
        "avg_away": 1.1,
        "equipos": {
            canonical_team_key("Unders FC"): {
                "ataque_local": 1.0,
                "ataque_visita": 1.0,
                "defensa_local": 1.0,
                "defensa_visita": 1.0,
            }
        },
    }
    tendencias = {canonical_team_key("Unders FC"): {"senal": 0.02}}
    salida = tt.ajustar_fuerzas(fuerzas, tendencias)
    assert fuerzas["equipos"][canonical_team_key("Unders FC")]["ataque_local"] == 1.0
    assert salida["equipos"][canonical_team_key("Unders FC")]["ataque_local"] == pytest.approx(1.02)
    assert salida["equipos"][canonical_team_key("Unders FC")]["defensa_local"] == pytest.approx(0.98)


def test_fuente_ligamx_api_usa_temporada_actual():
    with (
        mock.patch.object(tt, "cargar_aprendizajes_internos", return_value=[]),
        mock.patch("src.ligamx_api.estado_temporada", return_value={"tournament_now": "Apertura-2026"}),
        mock.patch("src.ligamx_api.resultados_historicos", return_value=_resultados()) as historico,
    ):
        salida = tt.cargar_resultados_torneo_actual()
    historico.assert_called_once_with(season="Apertura-2026")
    assert salida["fuente"] == "LigaMX-API"
    assert len(salida["resultados"]) == len(_resultados())


def test_fallback_filtra_resultados_anteriores_al_torneo():
    datos = {
        "fuente": "ESPN",
        "resultados": [
            {"fecha": "2026-06-01", "home_team": "A", "away_team": "B", "home_goals": 1, "away_goals": 0},
            {"fecha": "2026-07-18", "home_team": "A", "away_team": "B", "home_goals": 2, "away_goals": 0},
        ],
    }
    with (
        mock.patch.object(tt, "cargar_aprendizajes_internos", return_value=[]),
        mock.patch("src.ligamx_api.estado_temporada", side_effect=RuntimeError("sin red")),
        mock.patch("src.fuentes_datos.obtener_resultados", return_value=datos),
    ):
        salida = tt.cargar_resultados_torneo_actual("2026-07-16")
    assert salida["fuente"] == "ESPN"
    assert [r["fecha"] for r in salida["resultados"]] == ["2026-07-18"]


def test_sin_red_devuelve_lista_vacia():
    with (
        mock.patch.object(tt, "cargar_aprendizajes_internos", return_value=[]),
        mock.patch("src.ligamx_api.estado_temporada", side_effect=RuntimeError("sin red")),
        mock.patch("src.fuentes_datos.obtener_resultados", side_effect=RuntimeError("sin red")),
    ):
        salida = tt.cargar_resultados_torneo_actual()
    assert salida["fuente"] == "no_disponible"
    assert salida["resultados"] == []
