from src.survivor_reglas import evaluar_temporada, metricas_candidato


def _pick(jornada, resultado=None, estado="resuelto"):
    return {
        "jornada": jornada,
        "estado": estado,
        "resultado": resultado,
        "equipo": f"Equipo {jornada}",
        "fecha": f"2026-08-{jornada:02d}",
    }


def test_primer_empate_sobrevive_y_consume_la_vida():
    estado = evaluar_temporada([_pick(1, "empate")])

    assert estado["sigue_vivo"] is True
    assert estado["vida_empate_consumida"] is True
    assert estado["vida_empate_jornada"] == 1
    assert estado["empates"] == 1
    assert estado["racha"] == 1


def test_segundo_empate_elimina():
    estado = evaluar_temporada([_pick(1, "empate"), _pick(2, "gano"), _pick(3, "empate")])

    assert estado["sigue_vivo"] is False
    assert estado["eliminado_en"] == 3
    assert estado["victorias"] == 1
    assert estado["empates"] == 2


def test_derrota_elimina_y_guarda_jornada():
    estado = evaluar_temporada([_pick(1, "gano"), _pick(2, "perdio")])

    assert estado["sigue_vivo"] is False
    assert estado["eliminado_en"] == 2
    assert estado["racha"] == 1
    assert estado["aviso_finalista"]


def test_aplazado_permanece_pendiente_mientras_hay_otra_jornada():
    estado = evaluar_temporada([_pick(2, None, "bloqueado"), _pick(3, None, "confirmado")])

    assert estado["sigue_vivo"] is True
    assert estado["pendientes"] == 2
    assert [pick["jornada"] for pick in estado["picks_pendientes"]] == [2, 3]


def test_con_vida_consumida_empate_ya_no_cuenta_como_supervivencia():
    candidato = {"prob_victoria_pct": 54.0, "prob_empate_pct": 26.0}

    con_vida = metricas_candidato(candidato, False)
    sin_vida = metricas_candidato(candidato, True)
    assert con_vida["supervivencia_pct"] == 80.0
    assert sin_vida["supervivencia_pct"] == 54.0
    assert sin_vida["score_oficial"] == 54.0
