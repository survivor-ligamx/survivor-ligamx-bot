from datetime import date
from unittest import mock

from src import contexto_web_partidos as cwp


def test_ventana_previa_default_es_cinco_dias():
    with mock.patch.dict("os.environ", {}, clear=True):
        assert cwp._dias_previa() == 5


def test_ventana_previa_se_acota():
    with mock.patch.dict("os.environ", {"WEB_CONTEXT_PREMATCH_DAYS": "99"}, clear=False):
        assert cwp._dias_previa() == 10
    with mock.patch.dict("os.environ", {"WEB_CONTEXT_PREMATCH_DAYS": "0"}, clear=False):
        assert cwp._dias_previa() == 1


def test_perea_disponible_no_es_titular_confirmado():
    evento = cwp.clasificar_evento(
        {
            "titulo": "Óscar Perea ya tiene visa de trabajo y podría debutar con América",
            "texto": "El colombiano está disponible para ser convocado ante Santos",
            "url": "https://americamonumental.bolavip.com/noticias/perea",
            "proveedor": "tavily",
            "fecha": "2026-07-29",
        },
        "América",
        "Santos Laguna",
    )
    assert evento["tipo"] == "DEBUT_POSIBLE"
    assert evento["confirmacion"] == "NO_CONFIRMADA"
    assert evento["titularidad"] == "NO_CONFIRMADA"


def test_dos_dominios_independientes_producen_probable():
    eventos = [
        {"equipo": "América", "tipo": "LESION", "confirmacion": "NO_CONFIRMADA", "dominio": "medio-a.mx"},
        {"equipo": "América", "tipo": "LESION", "confirmacion": "NO_CONFIRMADA", "dominio": "medio-b.mx"},
    ]
    assert cwp._estado_eventos(eventos) == "PROBABLE"


def test_xi_confirmado_tiene_estado_especial():
    eventos = [
        {
            "equipo": "América",
            "tipo": "ALINEACION_CONFIRMADA",
            "confirmacion": "CONFIRMADA",
            "dominio": "clubamerica.com.mx",
        }
    ]
    assert cwp._estado_eventos(eventos) == "CONFIRMADO_CON_XI"


def test_jornada_a_cinco_dias_entra_en_monitoreo():
    calendario = [
        {
            "jornada": 3,
            "fecha_inicio": "2026-08-03",
            "fecha_fin": "2026-08-05",
            "partidos": [{"home_team": "América", "away_team": "Santos Laguna"}],
        }
    ]
    with (
        mock.patch.dict("os.environ", {"TAVILY_API_KEY": "t", "WEB_CONTEXT_MATCH_LIMIT": "9"}, clear=False),
        mock.patch("src.planificador_survivor.cargar_calendario", return_value=calendario),
        mock.patch.object(cwp, "investigar_partido", return_value={"disponible": True, "cache": False}) as investigar,
    ):
        resultado = cwp.actualizar_contexto_automatico(hoy=date(2026, 7, 29))
    assert resultado["jornadas"] == [3]
    investigar.assert_called_once()


def test_contexto_cache_partido_no_busca_en_web():
    cache = {"disponible": True, "estado": "REVISAR", "cache": True}
    with (
        mock.patch.object(cwp, "_leer_cache", return_value=cache) as leer,
        mock.patch.object(cwp, "_buscar") as buscar,
    ):
        assert cwp.contexto_cache_partido("América", "Santos", 3) is cache
    leer.assert_called_once_with("América", "Santos", "previa", 3)
    buscar.assert_not_called()
