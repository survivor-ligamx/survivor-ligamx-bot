from unittest import mock

from src.telegram import plan_persistido as pp


def _plan():
    return {
        "plan": [
            {
                "jornada": 3,
                "equipo": "América",
                "rival": "Santos",
                "condicion": "Local",
                "prob_ganar_pct": 70.0,
                "no_perder_pct": 88.0,
                "nivel": "ALTA",
            }
        ],
        "historial_cerrado": [],
        "jornada_plan_desde": 3,
        "prob_supervivencia_total_pct": 88.0,
        "victorias_esperadas": 0.7,
        "calendario_incompleto": False,
    }


def test_mensaje_muestra_contexto_y_escapa_html_externo():
    plan = _plan()
    plan["contextos_web"] = [
        {
            "jornada": 3,
            "equipo": "América",
            "resumen": "Baja <confirmada> & rotación posible",
        }
    ]

    mensaje = pp.construir_mensaje_plan_persistido(plan)

    assert "🌐 Contexto web reciente" in mensaje
    assert "Baja &lt;confirmada&gt; &amp; rotación posible" in mensaje
    assert "Baja <confirmada>" not in mensaje


def test_enviar_plan_usa_contexto_cacheado_sin_hacer_busquedas():
    plan = _plan()
    contexto = [{"jornada": 3, "equipo": "América", "resumen": "Sin bajas"}]
    with (
        mock.patch.object(pp, "_plan_temporada", return_value=plan),
        mock.patch.object(pp, "_aplicar_tendencias", return_value=False),
        mock.patch(
            "src.contexto_web_partidos.contextos_para_plan",
            return_value=contexto,
        ) as cargar,
        mock.patch.object(pp.envio_mod, "enviar_mensaje", return_value=True),
    ):
        resultado = pp.enviar_plan([], usar_momios=False)

    cargar.assert_called_once()
    assert resultado["enviado"] is True
    assert resultado["contextos_web"] == 1
