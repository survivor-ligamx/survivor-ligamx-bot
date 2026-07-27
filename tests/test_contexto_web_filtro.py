from src.contexto_web_partidos import _es_relevante


def test_resultado_post_relevante_con_ambos_equipos():
    assert (
        _es_relevante(
            "Puebla gana en casa",
            "Victoria del Puebla ante Santos",
            "Puebla",
            "Santos",
            "post",
        )
        is True
    )


def test_liga_mx_generica_no_es_contexto_del_partido():
    assert (
        _es_relevante(
            "Noticias Liga MX hoy",
            "Resumen de la jornada",
            "Puebla",
            "Santos",
        )
        is False
    )


def test_torneo_generico_no_es_contexto_del_partido():
    assert (
        _es_relevante(
            "Clausura 2026 resultados",
            "Partidos del fin de semana",
            "Toluca",
            "América",
        )
        is False
    )


def test_resultado_irrelevante_sin_equipos_ni_liga():
    assert (
        _es_relevante(
            "Washington Nationals vs Athletics",
            "MLB game results",
            "Puebla",
            "Santos",
        )
        is False
    )


def test_resultado_irrelevante_gavi_barcelona():
    assert (
        _es_relevante(
            "Gavi: Argentina players shouldn't be punished",
            "Barcelona midfielder speaks out",
            "Puebla",
            "Santos",
        )
        is False
    )


def test_nombre_corto_detecta_equipo():
    assert (
        _es_relevante(
            "América vs Santos",
            "Resumen América gana",
            "América",
            "Santos",
        )
        is True
    )
