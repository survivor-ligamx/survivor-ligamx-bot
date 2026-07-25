from src.motor_pronosticos import mejores_picks_estrategico


def _pronostico(home, away, local_pct=55.0, empate_pct=25.0, visita_pct=20.0):
    return {
        "local": home,
        "visitante": away,
        "prob_local_pct": local_pct,
        "prob_empate_pct": empate_pct,
        "prob_visitante_pct": visita_pct,
        "no_perder_local_pct": round(local_pct + empate_pct, 2),
        "no_perder_visitante_pct": round(visita_pct + empate_pct, 2),
    }


def test_ranking_con_vida_disponible_usa_no_perder_completo():
    result = mejores_picks_estrategico(
        [_pronostico("América", "Toluca", 55, 25, 20)],
        vida_empate_consumida=False,
        n=1,
    )
    picks = result["picks"]
    assert len(picks) == 1
    assert picks[0]["equipo"] == "América"
    assert picks[0]["supervivencia_pct"] == 80.0
    assert "YA CONSUMIDA" not in picks[0]["razon"]
    assert result["vida_empate_consumida"] is False


def test_ranking_con_vida_consumida_supervivencia_es_solo_ganar():
    result = mejores_picks_estrategico(
        [_pronostico("América", "Toluca", 55, 25, 20)],
        vida_empate_consumida=True,
        n=1,
    )
    picks = result["picks"]
    assert len(picks) == 1
    assert picks[0]["supervivencia_pct"] == 55.0
    assert "YA CONSUMIDA" in picks[0]["razon"]
    assert result["vida_empate_consumida"] is True


def test_ranking_con_vida_consumida_contiene_advertencia():
    result = mejores_picks_estrategico(
        [_pronostico("América", "Toluca", 55, 25, 20)],
        vida_empate_consumida=True,
        n=1,
    )
    assert "solo ganar sobrevive" in (result.get("advertencia") or "")
