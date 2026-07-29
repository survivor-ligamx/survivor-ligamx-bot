from unittest import mock

from src.telegram.formato import _contexto_web_top, _render_contexto_web


def test_top_pick_lee_cache_sin_busqueda_externa():
    tops = [{"equipo": "América", "rival": "Santos", "condicion": "Local", "jornada": 3}]
    cache = {"disponible": True, "estado": "REVISAR", "frescura": "FRESCA", "cache": True}
    with (
        mock.patch("src.contexto_web_partidos.contexto_cache_partido", return_value=cache) as leer,
        mock.patch("src.contexto_web_partidos._buscar") as buscar,
    ):
        assert _contexto_web_top(tops) is cache
    leer.assert_called_once_with("América", "Santos", 3)
    buscar.assert_not_called()


def test_render_avisa_que_noticia_no_cambia_probabilidad():
    contexto = {
        "disponible": True,
        "estado": "REVISAR",
        "frescura": "FRESCA",
        "actualizado_en": "2026-07-29T18:00:00+00:00",
        "eventos": [{"equipo": "América", "tipo": "DEBUT_POSIBLE", "titulo": "Perea podría debutar"}],
        "fuentes": ["https://americamonumental.bolavip.com/noticias/perea"],
    }
    texto = "\n".join(_render_contexto_web(contexto))
    assert "Estado: <b>REVISAR</b>" in texto
    assert "DEBUT_POSIBLE" in texto
    assert "No cambia probabilidades" in texto
