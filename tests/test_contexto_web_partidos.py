from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest import mock

from src import contexto_web_partidos as cwp


def _respuesta(data, status_code=200):
    respuesta = mock.Mock(status_code=status_code)
    respuesta.json.return_value = data
    respuesta.raise_for_status.side_effect = None if status_code == 200 else RuntimeError("http")
    return respuesta


def test_tavily_es_primario_y_no_gasta_respaldos_si_hay_resultados():
    resultados = [
        {
            "title": f"América Noticia {n}",
            "url": "https://ejemplo.mx/" + str(n),
            "content": "Baja confirmada en América",
        }
        for n in range(4)
    ]
    with (
        mock.patch.dict(
            "os.environ",
            {"TAVILY_API_KEY": "t", "GNEWS_API_KEY": "g", "SERPER_API_KEY": "s"},
            clear=False,
        ),
        mock.patch.object(cwp, "_leer_cache", return_value=None),
        mock.patch.object(cwp, "_guardar_cache"),
        mock.patch.object(cwp.requests, "post", return_value=_respuesta({"results": resultados})) as post,
        mock.patch.object(cwp.requests, "get") as get,
    ):
        contexto = cwp.investigar_partido("América", "Santos", jornada=3)

    assert contexto["disponible"] is True
    assert contexto["proveedores"] == ["tavily"]
    assert post.call_count == 1
    get.assert_not_called()


def test_gnews_respalda_si_tavily_falla():
    articulos = [
        {"title": "Puebla alineación probable", "url": "https://noticias.mx/a", "description": "Última hora Puebla"}
    ]
    with (
        mock.patch.dict(
            "os.environ",
            {"TAVILY_API_KEY": "t", "GNEWS_API_KEY": "g", "SERPER_API_KEY": ""},
            clear=False,
        ),
        mock.patch.object(cwp, "_leer_cache", return_value=None),
        mock.patch.object(cwp, "_guardar_cache"),
        mock.patch.object(cwp.requests, "post", side_effect=RuntimeError("sin Tavily")),
        mock.patch.object(cwp.requests, "get", return_value=_respuesta({"articles": articulos})),
    ):
        contexto = cwp.investigar_partido("Puebla", "Guadalajara", jornada=3)

    assert contexto["disponible"] is True
    assert contexto["proveedores"] == ["gnews"]


def test_clave_separa_el_mismo_partido_por_jornada():
    assert cwp._clave("América", "Santos", "post", 3) != cwp._clave("América", "Santos", "post", 17)
    assert cwp._clave("América", "Santos", "post", 3).startswith("v2|")


def test_cache_evitar_consultar_proveedores():
    cache = {"disponible": True, "resumen": "Baja confirmada", "cache": True}
    with (
        mock.patch.dict("os.environ", {"TAVILY_API_KEY": "t"}, clear=False),
        mock.patch.object(cwp, "_leer_cache", return_value=cache),
        mock.patch.object(cwp.requests, "post") as post,
    ):
        resultado = cwp.investigar_partido("América", "Santos", jornada=3)

    assert resultado is cache
    post.assert_not_called()


def test_contextos_para_plan_solo_muestra_alertas_de_previa():
    plan = {"plan": [{"jornada": 3, "equipo": "América", "rival": "Santos", "condicion": "Local"}]}
    previa = {"disponible": True, "resumen": "Baja confirmada en América", "fuentes": [], "cache": True}
    with (
        mock.patch.object(cwp, "_leer_cache", return_value=previa) as leer,
        mock.patch.object(cwp, "_historial_equipo") as historial,
    ):
        resultado = cwp.contextos_para_plan(plan)

    assert resultado[0]["resumen"] == "Baja confirmada en América"
    assert "Forma reciente" not in resultado[0]["resumen"]
    leer.assert_called_once_with("América", "Santos", "previa", 3)
    historial.assert_not_called()


def test_relevancia_exige_partido_o_alerta_accionable():
    assert cwp._es_relevante(
        "América vs Santos: previa de la Jornada 3",
        "América recibe a Santos en Liga MX",
        "América",
        "Santos",
        "previa",
    )
    assert cwp._es_relevante(
        "América confirma una baja por lesión",
        "El titular no estará disponible",
        "América",
        "Santos",
        "previa",
    )
    assert not cwp._es_relevante(
        "América ganó y aprovechó la localía",
        "Noticias generales de Liga MX",
        "América",
        "Santos",
        "previa",
    )
    assert not cwp._es_relevante(
        "América confirma una baja",
        "La crónica habla de otro torneo",
        "América",
        "Santos",
        "post",
    )
    assert not cwp._es_relevante(
        "América Femenil vs Santos Femenil",
        "Alineación confirmada",
        "América",
        "Santos",
        "previa",
    )
    assert cwp._es_relevante(
        "América llama a un juvenil por lesión ante Santos",
        "Será baja un titular del primer equipo",
        "América",
        "Santos",
        "previa",
    )


def test_cache_expirado_no_se_reutiliza():
    fila = (
        1,
        "2026-07-18",
        "resumen",
        "[]",
        "[]",
        "[]",
        datetime.now(timezone.utc).isoformat(),
        (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    )
    cursor = mock.Mock()
    cursor.fetchone.return_value = fila
    conexion = mock.MagicMock()
    conexion.cursor.return_value = cursor
    contexto = mock.MagicMock()
    contexto.__enter__.return_value = conexion
    contexto.__exit__.return_value = False
    with (
        mock.patch.object(cwp, "_asegurar_tabla"),
        mock.patch("src.database.get_db", return_value=contexto),
    ):
        assert cwp._leer_cache("A", "B", "post", 1) is None


def test_backfill_inicia_en_jornada_uno_y_revisa_cada_partido():
    calendario = [
        {
            "jornada": 1,
            "fecha_inicio": "2026-07-16",
            "fecha_fin": "2026-07-18",
            "partidos": [
                {"home_team": "Necaxa", "away_team": "Atlante"},
                {"home_team": "Monterrey", "away_team": "Santos"},
            ],
        },
        {
            "jornada": 3,
            "fecha_inicio": "2026-07-31",
            "fecha_fin": "2026-08-02",
            "partidos": [{"home_team": "América", "away_team": "Santos"}],
        },
    ]
    with (
        mock.patch.dict("os.environ", {"TAVILY_API_KEY": "t", "WEB_CONTEXT_MATCH_LIMIT": "9"}, clear=False),
        mock.patch("src.planificador_survivor.cargar_calendario", return_value=calendario),
        mock.patch.object(
            cwp,
            "investigar_partido",
            return_value={"disponible": True, "cache": False},
        ) as investigar,
    ):
        resultado = cwp.actualizar_contexto_automatico(hoy=date(2026, 7, 24))

    assert resultado["jornadas"] == [1]
    assert resultado["partidos_revisados"] == 2
    assert investigar.call_count == 2
    investigar.assert_any_call(
        "Monterrey",
        "Santos",
        fase="post",
        jornada=1,
        fecha_partido="2026-07-18",
    )


def test_previa_inmediata_tiene_prioridad_sobre_backfill():
    calendario = [
        {
            "jornada": 1,
            "fecha_inicio": "2026-07-16",
            "fecha_fin": "2026-07-18",
            "partidos": [{"home_team": "Monterrey", "away_team": "Santos"}],
        },
        {
            "jornada": 3,
            "fecha_inicio": "2026-07-31",
            "fecha_fin": "2026-08-02",
            "partidos": [{"home_team": "América", "away_team": "Santos"}],
        },
    ]
    with (
        mock.patch.dict("os.environ", {"TAVILY_API_KEY": "t", "WEB_CONTEXT_MATCH_LIMIT": "1"}, clear=False),
        mock.patch("src.planificador_survivor.cargar_calendario", return_value=calendario),
        mock.patch.object(
            cwp,
            "investigar_partido",
            return_value={"disponible": True, "cache": False},
        ) as investigar,
    ):
        resultado = cwp.actualizar_contexto_automatico(hoy=date(2026, 7, 30))

    assert resultado["jornadas"] == [3]
    investigar.assert_called_once_with(
        "América",
        "Santos",
        fase="previa",
        jornada=3,
        fecha_partido="2026-07-31",
    )
