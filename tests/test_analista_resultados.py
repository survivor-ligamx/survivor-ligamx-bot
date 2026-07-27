from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

from src import analista_resultados as ar


def _evento_espn(tipo: str, minuto: str = "10", jugadores: list[dict] | None = None) -> dict:
    return {
        "type": {"text": tipo},
        "clock": {"displayValue": minuto},
        "team": {"displayName": "América"},
        "athletesInvolved": jugadores or [{"displayName": "Jugador Uno"}],
        "text": f"Detalle {tipo}",
    }


def test_parseo_fecha_y_partido_jugado():
    assert ar._parse_dt("2026-07-22T12:30:00Z") == datetime(2026, 7, 22, 12, 30)
    assert ar._parse_dt("fecha inválida") is None
    assert ar._ya_jugado("", "STATUS_FULL_TIME") is True
    assert ar._ya_jugado("fecha inválida", "STATUS_SCHEDULED") is False
    pasado = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    assert ar._ya_jugado(pasado, "STATUS_SCHEDULED", horas_post=2) is True


def test_extraer_y_formatear_eventos_espn():
    eventos = [
        _evento_espn("Goal", "12"),
        _evento_espn("Yellow Card", "20"),
        _evento_espn("Red Card", "30"),
        _evento_espn(
            "Substitution",
            "40",
            [{"displayName": "Jugador Sale"}, {"displayName": "Jugador Entra"}],
        ),
        _evento_espn("Penalty", "50"),
        _evento_espn("Unknown", "60"),
    ]
    extraidos = ar._extraer_eventos_espn({"competitions": [{"events": eventos}]})

    assert [evento["type"] for evento in extraidos] == [
        "goal",
        "yellow_card",
        "red_card",
        "substitution",
        "penalty",
    ]
    lineas = ar._formatear_eventos(extraidos)
    assert lineas[0].startswith("⚽ 12")
    assert any("Jugador Entra" in linea for linea in lineas)
    tarjetas = ar._formatear_tarjetas(extraidos)
    assert len(tarjetas) == 2
    assert ar._goles_desde_marcador("América", "Toluca", 2, 1) == [
        "⚽ América — Gol 1",
        "⚽ América — Gol 2",
        "⚽ Toluca — Gol 1",
    ]
    assert ar._goles_desde_marcador("América", "Toluca", None, 1) == []


def test_obtener_partidos_espn_deduplica_y_normaliza():
    evento = {
        "id": "evt-1",
        "date": "2026-07-22T02:00:00Z",
        "status": {"type": {"name": "STATUS_FULL_TIME"}},
        "competitions": [
            {
                "competitors": [
                    {"homeAway": "home", "score": "2", "team": {"displayName": "América"}},
                    {"homeAway": "away", "score": "1", "team": {"displayName": "Toluca"}},
                ],
                "events": [_evento_espn("Goal")],
            }
        ],
    }
    respuesta = mock.Mock(status_code=200)
    respuesta.json.return_value = {"events": [evento]}
    with mock.patch.object(ar.requests, "get", return_value=respuesta) as get:
        partidos = ar._obtener_partidos_espn("20260722")

    assert get.call_count == 5
    assert len(partidos) == 1
    assert partidos[0]["home_goals"] == 2
    assert partidos[0]["eventos_espn"][0]["type"] == "goal"


def test_obtener_partidos_ligamx_y_combinar_fuentes():
    crudos = [
        {
            "espn_event_id": "1",
            "kickoff_utc": "2026-07-22T02:00:00Z",
            "fecha": "2026-07-22",
            "home_team": "América",
            "away_team": "Toluca",
            "home_goals": 2,
            "away_goals": 1,
        },
        {"home_team": "", "away_team": "", "home_goals": None, "away_goals": None},
    ]
    with mock.patch.object(ar.lmx, "resultados_historicos", return_value=crudos):
        liga = ar._obtener_partidos_ligamx()
    assert len(liga) == 1
    assert liga[0]["estado"] == "STATUS_FULL_TIME"

    espn = [dict(liga[0])]
    with (
        mock.patch.object(ar, "_obtener_partidos_espn", return_value=espn),
        mock.patch.object(ar, "_obtener_partidos_ligamx", return_value=liga),
    ):
        combinados = ar.obtener_partidos_jornada()
    assert combinados == espn


def test_obtener_detalle_partido_usa_365_y_cache():
    ar._CACHE_EVENTOS_365.clear()
    ar._CACHE_DETALLES.clear()
    eventos = [{"type": "goal", "minute": "8", "team": "América", "player": "Jugador"}]
    with (
        mock.patch.object(ar.lmx, "evento_365_id", return_value=101) as evento_id,
        mock.patch.object(ar.lmx, "eventos_365_partido", return_value=eventos) as eventos_365,
        mock.patch.object(ar.lmx, "noticias_de_equipos", return_value=[{"titulo": "Previa"}]),
    ):
        primero = ar.obtener_detalle_partido("América", "Toluca", fecha="2026-07-22")
        segundo = ar.obtener_detalle_partido("América", "Toluca", fecha="2026-07-22")

    assert primero["eventos"] == eventos
    assert primero["noticias"] == [{"titulo": "Previa"}]
    assert segundo is primero
    evento_id.assert_called_once()
    eventos_365.assert_called_once_with(101)


def test_senales_con_visitante_y_roja():
    eventos = [{"type": "red_card", "team": "Toluca"}]
    senales, bien, mal = ar._senales_partido("América", "Toluca", 1, 2, eventos)
    assert "Victoria de Toluca por margen mínimo (2-1); el marcador no demuestra dominio" in senales
    assert "Toluca ganó CON 1 roja(s)" in senales
    assert bien == {"Toluca"}
    assert mal == {"América"}

    empate, bien_empate, mal_empate = ar._senales_partido("Pumas", "Atlas", 0, 0, [])
    assert empate == ["Empate Pumas 0-0 Atlas"]
    assert not bien_empate
    assert not mal_empate


def test_chivas_uno_cero_al_90_mas_6_se_marca_sufrido_no_dominante():
    eventos = [{"type": "goal", "minute": "90'+6'", "team": "Guadalajara", "player": "Jugador"}]

    senales, bien, mal = ar._senales_partido("Guadalajara", "FC Juarez", 1, 0, eventos)
    conclusion = ar._conclusion_factual("Guadalajara", "FC Juarez", 1, 0, eventos)

    assert senales == ["Victoria sufrida de Guadalajara: 1-0 con gol al 90+6; el resultado no demuestra dominio"]
    assert bien == {"Guadalajara"}
    assert mal == {"FC Juarez"}
    assert "victoria sufrida" in conclusion["conclusion"]
    assert "gol al 90+6" in conclusion["conclusion"]
    assert "no se puede calificar" in conclusion["conclusion"]
    assert "sólida presentación" not in conclusion["conclusion"]
    normalizados = ar._normalizar_eventos(["⚽ 90+6' Guadalajara — Yael Padilla"])
    assert normalizados == [{"type": "goal", "minute": "90+6", "team": "Guadalajara", "detail": "Yael Padilla"}]
    conclusion_fallback = ar._conclusion_factual("Guadalajara", "FC Juarez", 1, 0, normalizados)
    assert "gol al 90+6" in conclusion_fallback["conclusion"]

    dos_uno = ar._conclusion_factual("Guadalajara", "FC Juarez", 2, 1, normalizados)
    assert "margen mínimo" in dos_uno["conclusion"]
    assert "gol tardío al 90+6" in dos_uno["conclusion"]


def test_conclusion_sin_estadisticas_es_factual_y_no_llama_ia():
    detalle = {
        "eventos": [{"type": "goal", "minute": "10", "team": "América", "player": "Jugador"}],
        "alineacion": {
            "disponible": True,
            "equipos": [{"equipo": "América", "titulares": ["A", "B", "C", "D"]}],
        },
        "impacto_xi": {
            "disponible": True,
            "equipos": {
                "América": {
                    "fuerza_xi_pct": 90,
                    "ausentes_clave": [{"jugador": "Ausente"}],
                }
            },
        },
    }
    with (
        mock.patch.object(ar.ia, "habilitado", return_value=True),
        mock.patch.object(ar.requests, "post") as post,
    ):
        conclusion = ar._conclusion_ia("América", "Toluca", detalle, hg=2, ag=1)

    assert conclusion["disponible"] is True
    assert "América ganó 2-1" in conclusion["conclusion"]
    assert "margen mínimo" in conclusion["conclusion"]
    assert "no se puede calificar" in conclusion["conclusion"]
    post.assert_not_called()


def test_procesar_partido_con_fallback_fuerte():
    partido = {
        "home_team": "América",
        "away_team": "Toluca",
        "home_goals": 2,
        "away_goals": 1,
        "fecha": "2026-07-22",
    }
    detalle_fuerte = {
        "eventos": [{"type": "goal", "minute": "15", "team": "América", "player": "Jugador"}],
        "conclusion": "América controló el partido.",
    }
    with (
        mock.patch.object(
            ar,
            "obtener_detalle_partido",
            return_value={"eventos": [], "alineacion": None, "impacto_xi": None},
        ),
        mock.patch.object(ar, "_obtener_detalles_fuera", return_value=detalle_fuerte),
    ):
        analisis = ar._procesar_partido(
            partido,
            [{"equipo": "América", "rival": "Toluca", "condicion": "Local"}],
        )

    assert analisis["resultado"] == "🏆 América 2-1 Toluca"
    assert "América ganó 2-1" in analisis["conclusion_ia"]["conclusion"]
    assert "margen mínimo" in analisis["conclusion_ia"]["conclusion"]
    assert "controló el partido" not in analisis["conclusion_ia"]["conclusion"]
    assert analisis["picks_lineas"] == ["🤖 El bot había recomendado América (Local) en este partido."]


def test_analizar_jornada_construye_resumen_y_tabla():
    partidos = [
        {"home_team": "América", "away_team": "Toluca", "home_goals": 2, "away_goals": 1},
        {"home_team": "Pumas", "away_team": "Atlas", "home_goals": 0, "away_goals": 0},
    ]

    def procesar(p: dict, _picks: list[dict]) -> dict:
        home = p["home_team"]
        away = p["away_team"]
        hg = p["home_goals"]
        ag = p["away_goals"]
        senales, bien, mal = ar._senales_partido(home, away, hg, ag, [])
        return {
            "home": home,
            "away": away,
            "home_goals": hg,
            "away_goals": ag,
            "eventos": [],
            "eventos_lineas": [],
            "tarjetas": [],
            "alineacion": None,
            "impacto_xi": None,
            "picks_lineas": [],
            "senales": senales,
            "bien": bien,
            "mal": mal,
            "conclusion_ia": {"disponible": True, "conclusion": "Conclusión basada en datos."},
        }

    tabla = {
        "América": {"pj": 1, "g": 1, "e": 0, "p": 0, "gf": 2, "gc": 1, "puntos": 3},
        "Toluca": {"pj": 1, "g": 0, "e": 0, "p": 1, "gf": 1, "gc": 2, "puntos": 0},
    }
    with (
        mock.patch.object(ar, "obtener_partidos_jornada", return_value=partidos),
        mock.patch.object(ar, "_procesar_partido", side_effect=procesar),
        mock.patch.object(ar, "_guardar_resultados_jornada") as guardar,
        mock.patch.object(ar, "cargar_historial_resultados", return_value={"por_fecha": {"2026-07-22": {}}}),
        mock.patch.object(ar, "_tabla_acumulada", return_value=tabla),
    ):
        resultado = ar.analizar_jornada(fecha="2026-07-22")

    assert len(resultado["partidos"]) == 2
    assert "ANÁLISIS DE LA JORNADA" in resultado["resumen"]
    assert "TABLA GENERAL (1 j.)" in resultado["mensaje_tabla"]
    assert "Empezaron BIEN" in resultado["mensaje_tabla"]
    assert len(resultado["mensajes_individuales"]) == 2
    guardar.assert_called_once()


def test_analizar_jornada_sin_partidos():
    with mock.patch.object(ar, "obtener_partidos_jornada", return_value=[]):
        resultado = ar.analizar_jornada()
    assert resultado["partidos"] == []
    assert "No hay partidos jugados" in resultado["resumen"]
