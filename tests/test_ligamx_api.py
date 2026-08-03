#!/usr/bin/env python3
"""Tests para src/ligamx_api.py (cliente Liga MX API). Sin red: requests mockeado."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import ligamx_api as api  # noqa: E402


def _resp(status=200, payload=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    return r


_CALENDAR = {
    "season": "vigente",
    "total_matches": 4,
    "jornadas": [
        {
            "jornada": 2,
            "matches": [
                {
                    "id": 1,
                    "date": "2026-07-24T01:00:00",
                    "status": "scheduled",
                    "home_team": {"id": 1, "name": "Club América"},
                    "away_team": {"id": 2, "name": "Chivas Guadalajara"},
                    "venue": "Estadio Azteca",
                },
            ],
        },
        {
            "jornada": 1,
            "matches": [
                {
                    "id": 2,
                    "date": "2026-07-17T01:00:00",
                    "status": "scheduled",
                    "home_team": {"id": 3, "name": "Necaxa"},
                    "away_team": {"id": 4, "name": "Atlante"},
                    "venue": "Estadio Victoria",
                },
                # partido incompleto (sin visitante) -> se descarta:
                {
                    "id": 3,
                    "date": "2026-07-17T03:00:00",
                    "status": "scheduled",
                    "home_team": {"id": 5, "name": "Tijuana"},
                    "away_team": {},
                    "venue": "Estadio Caliente",
                },
            ],
        },
    ],
}


class TestGet(unittest.TestCase):
    def test_get_ok(self):
        with mock.patch.object(api.requests, "get", return_value=_resp(200, {"a": 1})) as g:
            self.assertEqual(api._get("/season"), {"a": 1})
            g.assert_called_once()

    def test_get_http_error(self):
        with mock.patch.object(api.requests, "get", return_value=_resp(503)):
            with self.assertRaises(RuntimeError):
                api._get("/season")

    def test_get_network_error(self):
        with mock.patch.object(api.requests, "get", side_effect=api.requests.RequestException("boom")):
            with self.assertRaises(RuntimeError):
                api._get("/season")


class TestBaseUrl(unittest.TestCase):
    def test_default(self):
        import os

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LIGAMX_API_URL", None)
            self.assertEqual(api.base_url(), api.DEFAULT_BASE_URL)

    def test_env_override_strips_trailing_slash(self):
        import os

        with mock.patch.dict(os.environ, {"LIGAMX_API_URL": "https://x.test/"}):
            self.assertEqual(api.base_url(), "https://x.test")


class TestUsarComoFuente(unittest.TestCase):
    def test_off_por_defecto(self):
        import os

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LIGAMX_API_AS_SOURCE", None)
            self.assertFalse(api.usar_como_fuente())

    def test_on_con_1(self):
        import os

        with mock.patch.dict(os.environ, {"LIGAMX_API_AS_SOURCE": "1"}):
            self.assertTrue(api.usar_como_fuente())


class TestDisponible(unittest.TestCase):
    def test_true(self):
        with mock.patch.object(api.requests, "get", return_value=_resp(200, {})):
            self.assertTrue(api.disponible())

    def test_false_on_error(self):
        with mock.patch.object(api.requests, "get", side_effect=Exception("down")):
            self.assertFalse(api.disponible())


class TestCalendarioPlanificador(unittest.TestCase):
    def test_mapeo_y_orden(self):
        with mock.patch.object(api.requests, "get", return_value=_resp(200, _CALENDAR)):
            cal = api.calendario_para_planificador()
        self.assertEqual([j["jornada"] for j in cal], [1, 2])
        self.assertEqual(len(cal[0]["partidos"]), 1)  # descarta el incompleto
        self.assertEqual(cal[0]["partidos"][0]["home_team"], "Necaxa")
        self.assertEqual(cal[0]["partidos"][0]["away_team"], "Atlante")
        self.assertIn("home_team", cal[0]["partidos"][0])
        self.assertIn("away_team", cal[0]["partidos"][0])

    def test_nombres_normalizados(self):
        with mock.patch.object(api.requests, "get", return_value=_resp(200, _CALENDAR)):
            cal = api.calendario_para_planificador()
        self.assertEqual(cal[1]["partidos"][0]["home_team"], "América")

    def test_calendario_vacio(self):
        with mock.patch.object(api.requests, "get", return_value=_resp(200, {"jornadas": []})):
            self.assertEqual(api.calendario_para_planificador(), [])


class TestFixturesPlanos(unittest.TestCase):
    def test_lista_plana_con_fecha(self):
        with mock.patch.object(api.requests, "get", return_value=_resp(200, _CALENDAR)):
            fx = api.fixtures_planos()
        # 2 partidos completos (descarta el de Tijuana sin visitante).
        self.assertEqual(len(fx), 2)
        for f in fx:
            self.assertIn("fecha", f)
            self.assertIn("home_team", f)
            self.assertIn("away_team", f)
        # Nombres normalizados.
        nombres = {f["home_team"] for f in fx}
        self.assertIn("América", nombres)


class TestResultadosHistoricos(unittest.TestCase):
    def test_mapea_finalizados(self):
        finished = [
            {
                "home_team": {"name": "Club América"},
                "away_team": {"name": "Necaxa"},
                "home_score": 2,
                "away_score": 1,
                "match_date": "2026-08-01T01:00:00",
            },
            # sin marcador -> se descarta:
            {
                "home_team": {"name": "Toluca"},
                "away_team": {"name": "Atlas"},
                "home_score": None,
                "away_score": None,
                "match_date": "2026-08-02T01:00:00",
            },
        ]
        # Primera página devuelve la lista; segunda vacía para cortar el loop.
        with mock.patch.object(api, "obtener_partidos", side_effect=[finished, []]):
            res = api.resultados_historicos()
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["home_team"], "América")
        self.assertEqual(res[0]["home_goals"], 2)
        self.assertEqual(res[0]["away_goals"], 1)
        self.assertEqual(res[0]["fecha"], "2026-08-01")

    def test_pretemporada_vacio(self):
        with mock.patch.object(api, "obtener_partidos", return_value=[]):
            self.assertEqual(api.resultados_historicos(), [])


class TestPredecir(unittest.TestCase):
    def test_pasa_parametros(self):
        with mock.patch.object(api.requests, "get", return_value=_resp(200, {"ok": 1})) as g:
            api.predecir(229, 226)
            _, kwargs = g.call_args
            self.assertEqual(kwargs["params"]["home"], 229)
            self.assertEqual(kwargs["params"]["away"], 226)


class TestJugadoresEnRiesgo(unittest.TestCase):
    def test_compacto_con_limit(self):
        raw = {
            "season": "Apertura 2026",
            "count": 3,
            "players": [
                {"player": "A", "team": "T1", "yellow_cards": 4},
                {"player": "B", "team": "T2", "yellow_cards": 4},
                {"player": "C", "team": "T3", "yellow_cards": 4},
            ],
        }
        with mock.patch.object(api, "jugadores_en_riesgo", return_value=raw):
            out = api.jugadores_en_riesgo_liga(limit=2)
        self.assertEqual(out["season"], "Apertura 2026")
        self.assertEqual(out["count"], 3)
        self.assertEqual(len(out["jugadores"]), 2)

    def test_pretemporada_vacio(self):
        with mock.patch.object(api, "jugadores_en_riesgo", return_value={"season": "x", "count": 0, "players": []}):
            out = api.jugadores_en_riesgo_liga()
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["jugadores"], [])


class TestNoticias(unittest.TestCase):
    _NEWS = [
        {"id": 1, "title": "Vieja", "source": "X", "link": "http://x", "published_at": "2026-07-01T10:00:00"},
        {"id": 2, "title": "Nueva", "source": "Y", "link": "http://y", "published_at": "2026-07-03T10:00:00"},
        {"id": 3, "title": "Media", "source": "Z", "link": "http://z", "published_at": "2026-07-02T10:00:00"},
    ]

    def test_noticias_365_normaliza(self):
        # /365scores/news usa 'url'/'image', no 'link'/'image_url'.
        raw365 = [
            {
                "id": 9,
                "title": "Fichaje bomba",
                "url": "http://bolavip/x",
                "image": "http://img",
                "published_at": "2026-07-03",
                "is_magazine": False,
            }
        ]
        with mock.patch.object(api, "_get", return_value=raw365) as g:
            out = api.noticias_365()
            g.assert_called_once_with("/365scores/news")
        self.assertEqual(out[0]["title"], "Fichaje bomba")
        self.assertEqual(out[0]["link"], "http://bolavip/x")  # url -> link
        self.assertEqual(out[0]["source"], "365Scores")
        self.assertEqual(out[0]["image_url"], "http://img")  # image -> image_url

    def test_noticias_combina_365_y_google_dedup(self):
        s365 = [{"title": "Nota A", "link": "a", "source": "365Scores", "published_at": "2026-07-03"}]
        goog = [
            {"title": "Nota A", "link": "a2", "source": "MARCA", "published_at": "2026-07-03"},  # dup por título
            {"title": "Nota B", "link": "b", "source": "ESPN", "published_at": "2026-07-02"},
        ]
        with (
            mock.patch.object(api, "noticias_365", return_value=s365),
            mock.patch.object(api, "noticias_google", return_value=goog),
        ):
            out = api.noticias()
        titulos = [n["title"] for n in out]
        self.assertEqual(titulos, ["Nota A", "Nota B"])  # 365 primero, B de relleno
        self.assertEqual(out[0]["source"], "365Scores")  # gana 365 en el dup

    def test_noticias_tolerante_si_una_fuente_falla(self):
        with (
            mock.patch.object(api, "noticias_365", side_effect=RuntimeError("down")),
            mock.patch.object(api, "noticias_google", return_value=[{"title": "Solo Google", "link": "g"}]),
        ):
            out = api.noticias()
        self.assertEqual([n["title"] for n in out], ["Solo Google"])

    def test_compacta_y_ordena_por_fecha(self):
        with mock.patch.object(api, "noticias", return_value=self._NEWS):
            items = api.noticias_recientes(limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["titulo"], "Nueva")  # más reciente primero
        self.assertEqual(items[1]["titulo"], "Media")
        self.assertEqual(set(items[0].keys()), {"titulo", "fuente", "publicado", "link"})

    def test_limit_cero(self):
        with mock.patch.object(api, "noticias", return_value=self._NEWS):
            self.assertEqual(api.noticias_recientes(limit=0), [])


class TestNoticiasDeEquipos(unittest.TestCase):
    _NEWS = [
        {
            "title": "Toluca ficha a un delantero",
            "description": "",
            "source": "A",
            "link": "l1",
            "published_at": "2026-07-03",
        },
        {
            "title": "Cruz Azul golea en amistoso",
            "description": "",
            "source": "B",
            "link": "l2",
            "published_at": "2026-07-02",
        },
        {
            "title": "Lesión de un jugador del América",
            "description": "",
            "source": "C",
            "link": "l3",
            "published_at": "2026-07-01",
        },
    ]

    def test_filtra_por_equipos(self):
        with mock.patch.object(api, "noticias", return_value=self._NEWS):
            res = api.noticias_de_equipos(["América", "Toluca"], limit=5, dias=365)
        titulos = [n["titulo"] for n in res]
        self.assertTrue(any("Toluca" in t for t in titulos))
        self.assertTrue(any("América" in t for t in titulos))
        self.assertFalse(any("Cruz Azul" in t for t in titulos))  # no es del match

    def test_sin_equipos(self):
        with mock.patch.object(api, "noticias", return_value=self._NEWS):
            self.assertEqual(api.noticias_de_equipos([], limit=5), [])


_TEAMS = [
    {"id": 205, "name": "Club América"},
    {"id": 234, "name": "Pachuca"},
    {"id": 232, "name": "Tigres UANL"},
]

_STANDINGS = [
    {
        "position": 2,
        "team": {"name": "Pachuca"},
        "played": 3,
        "won": 2,
        "drawn": 1,
        "lost": 0,
        "goals_for": 5,
        "goals_against": 2,
        "goal_difference": 3,
        "points": 7,
    },
    {
        "position": 1,
        "team": {"name": "Club América"},
        "played": 3,
        "won": 3,
        "drawn": 0,
        "lost": 0,
        "goals_for": 8,
        "goals_against": 1,
        "goal_difference": 7,
        "points": 9,
    },
]


class TestEquiposResolver(unittest.TestCase):
    def test_mapa_e_id(self):
        with mock.patch.object(api, "obtener_equipos", return_value=_TEAMS):
            m = api.mapa_equipos()
            # canonical_team_key("Club América") == canonical_team_key("America")
            self.assertEqual(api.id_de_equipo("America", m), 205)
            self.assertEqual(api.id_de_equipo("Tigres", m), 232)
            self.assertIsNone(api.id_de_equipo("Equipo Inexistente", m))


class TestTablaNormalizada(unittest.TestCase):
    def test_mapea_y_ordena(self):
        def _side(path, params=None):
            if path == "/standings":
                return _STANDINGS
            if path == "/season":
                return {"tournament_now": "Apertura 2026"}
            return {}

        with mock.patch.object(api, "_get", side_effect=_side):
            t = api.tabla_normalizada()
        self.assertEqual(t["torneo"], "Apertura 2026")
        # Ordenada por posición: América (1) antes que Pachuca (2).
        self.assertEqual([f["posicion"] for f in t["tabla"]], [1, 2])
        self.assertEqual(t["tabla"][0]["equipo"], "América")
        self.assertEqual(t["tabla"][0]["puntos"], 9)
        self.assertEqual(t["tabla"][1]["ganados"], 2)


class TestAnalisisPartido(unittest.TestCase):
    _MAPA = {api.canonical_team_key("América"): 227, api.canonical_team_key("Toluca"): 223}

    def test_dossier_agrega_senales(self):
        with (
            mock.patch.object(api, "mapa_equipos", return_value=self._MAPA),
            mock.patch.object(api, "predecir", return_value={"p1": 0.5}),
            mock.patch.object(api, "forma_equipo", return_value={"form": "WWD"}),
            mock.patch.object(api, "disciplina_equipo", return_value={"at_risk": []}),
            mock.patch.object(api, "racha_equipo", return_value={"streaks": {}}),
            mock.patch.object(api, "h2h_resumen", return_value={"played": 10}),
        ):
            d = api.analisis_partido("America", "Toluca")
        self.assertEqual(d["home"], "América")
        self.assertEqual(d["home_id"], 227)
        self.assertEqual(d["away_id"], 223)
        self.assertEqual(d["prediccion_api"], {"p1": 0.5})
        self.assertEqual(d["h2h_resumen"], {"played": 10})
        self.assertIn("decision", d)

    def test_tolerante_si_una_senal_falla(self):
        # predecir lanza (pretemporada); el resto sigue y no rompe.
        with (
            mock.patch.object(api, "mapa_equipos", return_value=self._MAPA),
            mock.patch.object(api, "predecir", side_effect=RuntimeError("sin partidos")),
            mock.patch.object(api, "forma_equipo", return_value={"form": ""}),
            mock.patch.object(api, "disciplina_equipo", return_value={"at_risk": []}),
            mock.patch.object(api, "racha_equipo", return_value={}),
            mock.patch.object(api, "h2h_resumen", return_value={}),
        ):
            d = api.analisis_partido("America", "Toluca")
        self.assertIsNone(d["prediccion_api"])  # falló -> None, sin romper
        self.assertEqual(d["forma_local"], {"form": ""})

    def test_equipo_no_resuelto(self):
        with mock.patch.object(api, "mapa_equipos", return_value=self._MAPA):
            d = api.analisis_partido("America", "Equipo Inexistente")
        self.assertIsNone(d["away_id"])
        self.assertIn("nota", d)


class TestResumenPartido(unittest.TestCase):
    _MAPA = {api.canonical_team_key("América"): 227, api.canonical_team_key("Toluca"): 223}

    def test_resumen_compacto(self):
        pred = {
            "probabilities": {"home_win": 0.55, "draw": 0.25, "away_win": 0.20},
            "expected_goals": {"home": 1.8, "away": 1.0},
        }
        disc = {"at_risk": [{"player": "Jugador X"}, {"player": "Jugador Y"}]}
        with (
            mock.patch.object(api, "mapa_equipos", return_value=self._MAPA),
            mock.patch.object(api, "predecir", return_value=pred),
            mock.patch.object(api, "forma_equipo", return_value={"form": "WWDLW"}),
            mock.patch.object(api, "disciplina_equipo", return_value=disc),
            mock.patch.object(api, "h2h_resumen", return_value={"played": 12}),
            mock.patch.object(api, "noticias_de_equipos", return_value=[{"titulo": "N"}]),
            mock.patch.object(api, "alineacion_de_partido", return_value={"disponible": False}),
        ):
            r = api.resumen_partido("America", "Toluca")
        self.assertEqual(r["prediccion_api"]["prob_local_pct"], 55.0)
        self.assertEqual(r["prediccion_api"]["goles_esp"], "1.8-1.0")
        self.assertEqual(r["forma_local"], "WWDLW")
        self.assertEqual(r["en_riesgo_local"], ["Jugador X", "Jugador Y"])
        self.assertEqual(r["h2h"], {"played": 12})
        self.assertEqual(r["noticias"], [{"titulo": "N"}])

    def test_resumen_pretemporada_tolerante(self):
        # predecir falla (sin partidos); forma/disciplina vacías -> sin romper.
        with (
            mock.patch.object(api, "mapa_equipos", return_value=self._MAPA),
            mock.patch.object(api, "predecir", side_effect=RuntimeError("sin partidos")),
            mock.patch.object(api, "forma_equipo", return_value={"form": ""}),
            mock.patch.object(api, "disciplina_equipo", return_value={"at_risk": []}),
            mock.patch.object(api, "h2h_resumen", return_value={}),
            mock.patch.object(api, "noticias_de_equipos", return_value=[]),
            mock.patch.object(api, "alineacion_de_partido", return_value=None),
        ):
            r = api.resumen_partido("America", "Toluca")
        self.assertIsNone(r["prediccion_api"])
        self.assertEqual(r["en_riesgo_local"], [])
        self.assertIsNone(r["h2h"])
        self.assertEqual(r["noticias"], [])


class TestAlineacion(unittest.TestCase):
    _EVENTOS = [
        {"event_id": 111, "home_team": "Club América", "away_team": "Toluca"},
        {"event_id": 222, "home_team": "Necaxa", "away_team": "Atlante"},
    ]

    def test_evento_365_id_match_flexible(self):
        with mock.patch.object(api, "eventos_365", return_value=self._EVENTOS):
            self.assertEqual(api.evento_365_id("America", "Toluca"), 111)
            self.assertIsNone(api.evento_365_id("Pumas", "Cruz Azul"))

    def test_alineacion_365_normaliza_y_disponible(self):
        raw = {
            "teams": [
                {
                    "team_name": "América",
                    "home_away": "home",
                    "formation": "4-3-3",
                    "players": [{"name": "P1"}, {"name": "P2"}],
                },
                {"team_name": "Toluca", "home_away": "away", "formation": None, "players": []},
            ]
        }
        with mock.patch.object(api, "_get", return_value=raw):
            r = api.alineacion_365(111)
        self.assertTrue(r["disponible"])  # hay jugadores en un equipo
        self.assertEqual(r["equipos"][0]["formacion"], "4-3-3")
        self.assertEqual(r["equipos"][0]["titulares"], ["P1", "P2"])

    def test_alineacion_365_vacia(self):
        raw = {"teams": [{"team_name": "A", "home_away": "home", "players": []}]}
        with mock.patch.object(api, "_get", return_value=raw):
            r = api.alineacion_365(111)
        self.assertFalse(r["disponible"])

    def test_alineacion_de_partido_sin_evento(self):
        with mock.patch.object(api, "evento_365_id", return_value=None):
            r = api.alineacion_de_partido("X", "Y")
        self.assertFalse(r["disponible"])
        self.assertIn("nota", r)

    def test_alineacion_de_partido_ok(self):
        with (
            mock.patch.object(api, "evento_365_id", return_value=111),
            mock.patch.object(api, "alineacion_365", return_value={"disponible": True, "equipos": []}),
        ):
            r = api.alineacion_de_partido("America", "Toluca")
        self.assertTrue(r["disponible"])
        self.assertEqual(r["event_id"], 111)


if __name__ == "__main__":
    unittest.main()


class TestJugadoresASeguir(unittest.TestCase):
    def test_goleadores_por_equipo_agrupa(self):
        data = [
            {"player": "A. Vega", "team": "Toluca", "goals": 8},
            {"player": "P. Aguilar", "team": "Toluca", "goals": 5},
            {"player": "X. Tercero", "team": "Toluca", "goals": 3},
            {"player": "H. Herrera", "team": "Cruz Azul", "goals": 6},
        ]
        with mock.patch.object(api, "goleadores", return_value=data):
            mapa = api.goleadores_por_equipo(por_equipo=2)
        self.assertIn("Toluca", mapa)
        self.assertEqual(len(mapa["Toluca"]), 2)  # respeta por_equipo=2
        self.assertEqual(mapa["Toluca"][0]["nombre"], "A. Vega")
        self.assertIn("Cruz Azul", mapa)

    def test_goleadores_por_equipo_vacio_tolerante(self):
        with mock.patch.object(api, "goleadores", return_value=[]):
            self.assertEqual(api.goleadores_por_equipo(), {})

    def test_match_id_de_partido_resuelve(self):
        proximos = [
            {"id": 77, "home_team": {"name": "Club América"}, "away_team": {"name": "Toluca"}},
        ]
        with mock.patch.object(api, "partidos_proximos", return_value=proximos):
            self.assertEqual(api.match_id_de_partido("América", "Toluca"), 77)

    def test_match_id_none_si_no_encuentra(self):
        with mock.patch.object(api, "partidos_proximos", return_value=[]):
            with mock.patch.object(api, "obtener_partidos", return_value=[]):
                self.assertIsNone(api.match_id_de_partido("América", "Toluca"))

    def test_jugadores_a_seguir_partido_forma_home_away(self):
        payload = {"home": [{"player": "A. Vega"}], "away": [{"name": "H. Herrera"}]}
        with mock.patch.object(api, "match_id_de_partido", return_value=5):
            with mock.patch.object(api, "jugadores_a_seguir", return_value=payload):
                res = api.jugadores_a_seguir_partido("Toluca", "Cruz Azul")
        self.assertEqual(res["local"], ["A. Vega"])
        self.assertEqual(res["visita"], ["H. Herrera"])

    def test_jugadores_a_seguir_partido_forma_plana(self):
        payload = {
            "players_to_watch": [
                {"player": "A. Vega", "team": "Toluca"},
                {"player": "H. Herrera", "team": "Cruz Azul"},
            ]
        }
        with mock.patch.object(api, "match_id_de_partido", return_value=5):
            with mock.patch.object(api, "jugadores_a_seguir", return_value=payload):
                res = api.jugadores_a_seguir_partido("Toluca", "Cruz Azul")
        self.assertIn("A. Vega", res["local"])
        self.assertIn("H. Herrera", res["visita"])

    def test_jugadores_a_seguir_partido_sin_id(self):
        with mock.patch.object(api, "match_id_de_partido", return_value=None):
            res = api.jugadores_a_seguir_partido("A", "B")
        self.assertEqual(res, {"local": [], "visita": []})


class TestPorteros(unittest.TestCase):
    def test_porteros_por_equipo_mejor_valla(self):
        data = [
            {"player": "K. Mier", "team": "Cruz Azul", "clean_sheets": 6, "goals_conceded": 8},
            {"player": "Suplente CAZ", "team": "Cruz Azul", "clean_sheets": 1},
            {"player": "L. Malagón", "team": "Club América", "clean_sheets": 5},
        ]
        with mock.patch.object(api, "porteros", return_value=data):
            mapa = api.porteros_por_equipo()
        self.assertEqual(mapa["Cruz Azul"]["nombre"], "K. Mier")  # el de más vallas
        self.assertEqual(mapa["Cruz Azul"]["vallas_invictas"], 6)
        self.assertIn("América", mapa)

    def test_porteros_por_equipo_vacio_tolerante(self):
        with mock.patch.object(api, "porteros", return_value=[]):
            self.assertEqual(api.porteros_por_equipo(), {})


class TestTransfers365(unittest.TestCase):
    _DATA = {
        "season": "Apertura 2026",
        "disponible": True,
        "equipos": {
            "América": {
                "altas": [{"jugador": "Borja Iglesias", "desde": "Celta", "tipo": "transfer"}],
                "bajas": [{"jugador": "Kevin Álvarez", "hacia": "Pachuca", "tipo": "transfer"}],
            },
            "Guadalajara": {"altas": [], "bajas": []},
        },
    }

    def test_transfers_equipo_formatea(self):
        r = api.transfers_equipo("América", self._DATA)
        self.assertEqual(r["altas"], ["Borja Iglesias (Celta)"])
        self.assertEqual(r["bajas"], ["Kevin Álvarez (Pachuca)"])

    def test_transfers_equipo_alias(self):
        # "Club América" empareja con "América".
        r = api.transfers_equipo("Club América", self._DATA)
        self.assertTrue(r["altas"])

    def test_transfers_equipo_sin_datos(self):
        r = api.transfers_equipo("Toluca", self._DATA)
        self.assertEqual(r, {"altas": [], "bajas": []})

    def test_transfers_365_tolerante(self):
        # Con data vacía, transfers_equipo devuelve listas vacías (no rompe).
        self.assertEqual(api.transfers_equipo("América", {}), {"altas": [], "bajas": []})


class TestLineupImpact(unittest.TestCase):
    def test_lineup_impact_partido_resuelve(self):
        payload = {"disponible": True, "equipos": {"América": {"fuerza_xi_pct": 82.5}}}
        with mock.patch.object(api, "evento_365_id", return_value=99):
            with mock.patch.object(api, "lineup_impact", return_value=payload):
                r = api.lineup_impact_partido("América", "Toluca")
        self.assertTrue(r["disponible"])
        self.assertIn("América", r["equipos"])

    def test_lineup_impact_partido_sin_evento(self):
        with mock.patch.object(api, "evento_365_id", return_value=None):
            self.assertEqual(api.lineup_impact_partido("A", "B"), {})


class TestProbableLineup(unittest.TestCase):
    def test_probable_lineup_partido_resuelve(self):
        payload = {
            "disponible": True,
            "fuente": "365scores",
            "equipos": [
                {
                    "equipo": "América",
                    "condicion": "home",
                    "formacion": "4-3-3",
                    "confirmada": False,
                    "titulares_probables": ["A", "B"],
                }
            ],
        }
        with mock.patch.object(api, "evento_365_id", return_value=99):
            with mock.patch.object(api, "probable_lineup", return_value=payload):
                r = api.probable_lineup_partido("América", "Toluca")
        self.assertTrue(r["disponible"])
        self.assertEqual(r["equipos"][0]["formacion"], "4-3-3")

    def test_probable_lineup_partido_sin_evento(self):
        with mock.patch.object(api, "evento_365_id", return_value=None):
            self.assertEqual(api.probable_lineup_partido("A", "B"), {})
