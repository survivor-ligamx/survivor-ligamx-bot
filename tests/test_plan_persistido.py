from unittest import mock

from src.telegram import plan_persistido as pp


def _historial():
    return [
        {
            "jornada": 1,
            "equipo": "Monterrey",
            "estado": "resuelto",
            "resultado": "gano",
        },
        {
            "jornada": 2,
            "equipo": "Cruz Azul",
            "estado": "resuelto",
            "resultado": "gano",
        },
    ]


def _resultado_plan():
    return {
        "plan": [
            {
                "jornada": 3,
                "equipo": "América",
                "rival": "Santos",
                "condicion": "Local",
                "prob_ganar_pct": 72.7,
                "no_perder_pct": 89.7,
                "nivel": "ALTA",
            }
        ],
        "prob_supervivencia_total_pct": 89.7,
        "victorias_esperadas": 0.73,
        "jornadas_riesgosas": [],
        "calendario_incompleto": False,
    }


def _tendencias():
    return {
        "undes_fc": {
            "senal": 0.02,
            "razones": ["ataque en forma"],
        }
    }


def _resultados_torneo():
    return [
        {"fecha": "2026-07-10", "home_team": "Unders FC", "away_team": "Grande", "home_goals": 2, "away_goals": 1},
        {"fecha": "2026-07-11", "home_team": "Debil", "away_team": "Solido", "home_goals": 0, "away_goals": 1},
    ]


def test_plan_excluye_jornadas_cerradas_y_muestra_historial():
    calendario = [
        {"jornada": 1, "partidos": []},
        {"jornada": 2, "partidos": []},
        {"jornada": 3, "partidos": []},
        {"jornada": 4, "partidos": []},
    ]
    with (
        mock.patch(
            "src.database.temporada_survivor_actual",
            return_value="Apertura-2026",
        ),
        mock.patch("src.database.get_survivor_picks", return_value=_historial()),
        mock.patch(
            "src.planificador_survivor.cargar_calendario",
            return_value=calendario,
        ),
        mock.patch(
            "src.fuentes_datos.leer_cache",
            return_value=[{"home_team": "A", "away_team": "B"}],
        ),
        mock.patch(
            "src.poisson_model.calcular_fuerzas",
            return_value={"equipos": {}},
        ),
        mock.patch(
            "src.planificador_survivor.planificar",
            return_value=_resultado_plan(),
        ) as planificar,
    ):
        resultado = pp._plan_temporada(
            ["Monterrey", "Cruz Azul"],
            usar_momios=False,
            jornada_desde=2,
            permitir_descarga=False,
        )

    calendario_recibido = planificar.call_args.args[0]
    assert [bloque["jornada"] for bloque in calendario_recibido] == [3, 4]
    assert [item["jornada"] for item in resultado["historial_cerrado"]] == [1, 2]
    assert [item["jornada"] for item in resultado["plan"]] == [3]

    mensaje = pp.construir_mensaje_plan_persistido(resultado)
    assert "J1 · Monterrey</b> 🔒 ✅ Ganó" in mensaje
    assert "J2 · Cruz Azul</b> 🔒 ✅ Ganó" in mensaje
    assert "Plan restante desde J3" in mensaje
    assert "J1 · América" not in mensaje
    assert "J2 · América" not in mensaje


def test_fallback_sin_bd_conserva_horizonte_actual():
    calendario = [
        {"jornada": 2, "partidos": []},
        {"jornada": 3, "partidos": []},
    ]
    plan = _resultado_plan()
    with (
        mock.patch(
            "src.database.get_survivor_picks",
            side_effect=RuntimeError("sin BD"),
        ),
        mock.patch(
            "src.planificador_survivor.cargar_calendario",
            return_value=calendario,
        ),
        mock.patch(
            "src.fuentes_datos.leer_cache",
            return_value=[{"home_team": "A", "away_team": "B"}],
        ),
        mock.patch(
            "src.poisson_model.calcular_fuerzas",
            return_value={"equipos": {}},
        ),
        mock.patch(
            "src.planificador_survivor.planificar",
            return_value=plan,
        ) as planificar,
    ):
        resultado = pp._plan_temporada(
            [],
            usar_momios=False,
            jornada_desde=2,
            permitir_descarga=False,
        )

    calendario_recibido = planificar.call_args.args[0]
    assert [bloque["jornada"] for bloque in calendario_recibido] == [2, 3]
    assert resultado["historial_cerrado"] == []


def test_tendencias_no_disponibles_devuelve_false():
    """Si no hay resultados del torneo, _aplicar_tendencias retorna False."""
    plan = _resultado_plan()
    plan["historial_cerrado"] = []
    plan["jornada_plan_desde"] = 3
    calendario = [
        {"jornada": 3, "partidos": [{"fecha": "2026-07-20"}]},
    ]
    with (
        mock.patch(
            "src.planificador_survivor.cargar_calendario",
            return_value=calendario,
        ),
        mock.patch(
            "src.tendencias_torneo.cargar_resultados_torneo_actual",
            return_value={"fuente": "no_disponible", "resultados": []},
        ),
        mock.patch(
            "src.fuentes_datos.leer_cache",
            return_value=[{"home_team": "A", "away_team": "B"}],
        ),
    ):
        aplicado = pp._aplicar_tendencias(plan, [], 0.5, False, False)

    assert aplicado is False


def test_tendencias_aplica_ajuste_y_marca_true():
    """Con datos del torneo, _aplicar_tendencias ajusta el plan y retorna True."""
    plan = _resultado_plan()
    plan["historial_cerrado"] = []
    plan["jornada_plan_desde"] = 3
    calendario = [
        {"jornada": 3, "partidos": [{"fecha": "2026-07-20"}]},
    ]
    with (
        mock.patch(
            "src.planificador_survivor.cargar_calendario",
            return_value=calendario,
        ),
        mock.patch(
            "src.tendencias_torneo.cargar_resultados_torneo_actual",
            return_value={
                "fuente": "mock",
                "resultados": _resultados_torneo(),
            },
        ),
        mock.patch(
            "src.tendencias_torneo.calcular_tendencias",
            return_value=_tendencias(),
        ),
        mock.patch(
            "src.tendencias_torneo.ajustar_fuerzas",
            return_value={"equipos": {"undes fc": {"ataque_local": 1.02, "defensa_local": 0.98}}},
        ),
        mock.patch(
            "src.fuentes_datos.leer_cache",
            return_value=[{"home_team": "A", "away_team": "B"}],
        ),
        mock.patch(
            "src.poisson_model.calcular_fuerzas",
            return_value={"equipos": {"undes fc": {"ataque_local": 1.0, "defensa_local": 1.0}}},
        ),
        mock.patch(
            "src.planificador_survivor.planificar",
            return_value=_resultado_plan(),
        ),
    ):
        aplicado = pp._aplicar_tendencias(plan, [], 0.5, False, False)

    assert aplicado is True


def test_tendencias_no_rompe_si_falla():
    """Si falla la capa de tendencias, retorna False sin lanzar excepción."""
    plan = _resultado_plan()
    plan["historial_cerrado"] = []
    plan["jornada_plan_desde"] = 3
    calendario = [{"jornada": 3, "partidos": []}]
    with (
        mock.patch(
            "src.planificador_survivor.cargar_calendario",
            return_value=calendario,
        ),
        mock.patch(
            "src.tendencias_torneo.cargar_resultados_torneo_actual",
            side_effect=RuntimeError("sin red"),
        ),
        mock.patch(
            "src.fuentes_datos.leer_cache",
            return_value=[{"home_team": "A", "away_team": "B"}],
        ),
    ):
        aplicado = pp._aplicar_tendencias(plan, [], 0.5, False, False)

    assert aplicado is False
