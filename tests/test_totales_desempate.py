from src.telegram.totales import calcular_totales_jornada


def _partido(xg_local=1.2, xg_visitante=1.3):
    return {
        "goles_esperados_local": xg_local,
        "goles_esperados_visitante": xg_visitante,
        "pick_ou": "Over",
        "pick_btts": "Sí",
    }


def test_cobertura_completa_publica_moda_para_desempate_exacto():
    totales = calcular_totales_jornada([_partido() for _ in range(9)])

    assert totales["partidos"] == 9
    assert totales["partidos_con_xg"] == 9
    assert totales["cobertura_completa"] is True
    assert totales["goles_esperados_total"] == 22.5
    assert totales["goles_desempate"] == 22
    assert totales["metodo_desempate"] == "moda_poisson_agregada"


def test_cobertura_parcial_no_publica_desempate_definitivo():
    totales = calcular_totales_jornada([_partido() for _ in range(8)])

    assert totales["partidos"] == 8
    assert totales["partidos_con_xg"] == 8
    assert totales["partidos_sin_xg"] == 1
    assert totales["cobertura_completa"] is False
    assert totales["goles_desempate"] is None
    assert totales["metodo_desempate"] is None


def test_partido_solo_momios_no_se_convierte_en_cero():
    pronosticos = [_partido(1.5, 1.0), {"mercado": {"1x2": {"momios": {}}}}]
    totales = calcular_totales_jornada(pronosticos, partidos_esperados=2)

    assert totales["partidos"] == 2
    assert totales["partidos_con_xg"] == 1
    assert totales["partidos_sin_xg"] == 1
    assert totales["goles_esperados_total"] == 2.5
    assert totales["promedio_goles_partido"] == 2.5
    assert totales["goles_desempate"] is None
