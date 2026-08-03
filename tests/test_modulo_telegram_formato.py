"""Tests del modulo extraido ``src/telegram_formato.py``.

Ojo: ``tests/test_telegram_formato.py`` NO prueba este modulo, sino las copias
que siguen viviendo dentro de ``telegram_pronosticos`` (importa
``telegram_pronosticos as tp``). De ahi que este archivo marcara 0% de
cobertura con 98 sentencias: no lo importa nadie.

Mientras las dos copias convivan, estos tests evitan que la extraida derive en
silencio respecto de la original.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.telegram_formato import (
    _cerca_de_jornada,
    _dividir_mensaje,
    _fecha_mx,
    _linea_goles,
    _marcador_a_favor,
    _norm_simple,
    _pct,
    _totales_jornada,
)


class TestPct:
    def test_redondea_sin_decimales(self):
        assert _pct(55.0) == "55"
        assert _pct(54.6) == "55"
        assert _pct(2.4) == "2"

    def test_valor_no_numerico_se_devuelve_tal_cual(self):
        assert _pct("s/d") == "s/d"
        assert _pct(None) == "None"


class TestNormSimple:
    def test_colapsa_espacios_y_baja_a_minusculas(self):
        assert _norm_simple("  Club   AMERICA  ") == "club america"

    def test_conserva_acentos(self):
        # Divergencia deliberada con canonical_team_key, que si los quita.
        assert _norm_simple("Le\u00f3n") == "le\u00f3n"

    def test_cadena_vacia_no_rompe(self):
        assert _norm_simple("") == ""


class TestFechaMx:
    def test_iso_valida_se_formatea(self):
        out = _fecha_mx("2026-08-15T22:00:00Z")
        # Con zoneinfo disponible convierte a CDMX; si no, cae a UTC.
        assert "h (CDMX)" in out or "UTC" in out
        assert out

    def test_texto_invalido_cae_al_fallback(self):
        assert _fecha_mx("no-es-fecha") == "no-es-fecha"

    def test_vacio_no_rompe(self):
        assert _fecha_mx("") == ""


class TestCercaDeJornada:
    @staticmethod
    def _en(dias: int) -> str:
        return (datetime.now(timezone.utc).date() + timedelta(days=dias)).isoformat()

    def test_jornada_inminente(self):
        assert _cerca_de_jornada([{"fecha": self._en(1)}]) is True

    def test_jornada_lejana(self):
        assert _cerca_de_jornada([{"fecha": self._en(10)}]) is False

    def test_sin_pronosticos(self):
        assert _cerca_de_jornada([]) is False

    def test_fechas_invalidas_no_rompen(self):
        assert _cerca_de_jornada([{"fecha": "sin-formato"}]) is False

    def test_manda_el_partido_mas_proximo(self):
        datos = [{"fecha": self._en(9)}, {"fecha": self._en(1)}]
        assert _cerca_de_jornada(datos) is True


class TestLineaGoles:
    def test_sin_datos_devuelve_vacio(self):
        assert _linea_goles({}) == ""

    def test_over_muestra_su_porcentaje(self):
        linea = _linea_goles({"pick_ou": "Over", "prob_over_pct": 62.0, "marcador_pick": "2-1"})
        assert "Goles: Over 2.5 (62%)" in linea
        assert "Marcador probable: 2-1" in linea

    def test_under_usa_el_complemento(self):
        linea = _linea_goles({"pick_ou": "Under", "prob_over_pct": 40.0, "marcador_pick": "1-0"})
        assert "Under 2.5 (60%)" in linea

    def test_btts_solo_si_hay_dato(self):
        con = _linea_goles({"pick_ou": "Over", "prob_over_pct": 55.0, "pick_btts": "S\u00ed"})
        assert "BTTS S\u00ed" in con
        sin = _linea_goles({"pick_ou": "Over", "prob_over_pct": 55.0})
        assert "BTTS" not in sin

    def test_marcador_pick_manda_sobre_mas_probable(self):
        linea = _linea_goles({"marcador_pick": "3-0", "marcador_mas_probable": "1-0"})
        assert "Marcador probable: 3-0" in linea

    def test_cae_a_marcador_mas_probable(self):
        linea = _linea_goles({"marcador_mas_probable": "1-0"})
        assert "Marcador probable: 1-0" in linea

    def test_aclara_choque_over_con_moda_baja(self):
        linea = _linea_goles({"pick_ou": "Over", "prob_over_pct": 60.0, "marcador_pick": "1-1"})
        assert "por eso el pick es Over" in linea

    def test_aclara_choque_under_con_moda_alta(self):
        linea = _linea_goles({"pick_ou": "Under", "prob_over_pct": 35.0, "marcador_pick": "2-1"})
        assert "por eso el pick es Under" in linea

    def test_sin_choque_no_aclara_nada(self):
        linea = _linea_goles({"pick_ou": "Over", "prob_over_pct": 70.0, "marcador_pick": "2-2"})
        assert "por eso el pick" not in linea

    def test_marcador_no_numerico_no_rompe(self):
        linea = _linea_goles({"pick_ou": "Over", "prob_over_pct": 60.0, "marcador_pick": "a-b"})
        assert "Marcador probable: a-b" in linea


class TestDividirMensaje:
    def test_mensaje_corto_no_se_parte(self):
        assert _dividir_mensaje("hola") == ["hola"]

    def test_respeta_el_limite_y_no_parte_lineas(self):
        texto = "\n".join(f"linea {i}" for i in range(50))
        partes = _dividir_mensaje(texto, limite=40)
        assert len(partes) > 1
        assert all(len(p) <= 40 for p in partes)
        # Reensamblar las partes devuelve el texto original intacto.
        assert "\n".join(partes) == texto

    def test_linea_mas_larga_que_el_limite_se_corta_duro(self):
        partes = _dividir_mensaje("x" * 90, limite=40)
        assert all(len(p) <= 40 for p in partes)
        assert "".join(partes) == "x" * 90


class TestTotalesJornada:
    def test_sin_pronosticos_devuelve_ceros(self):
        t = _totales_jornada([])
        assert t["partidos"] == 0
        assert t["goles_esperados_total"] == 0.0
        assert t["promedio_goles_partido"] == 0.0
        assert t["over_25_count"] == 0
        assert t["under_25_count"] == 0
        assert t["btts_si_count"] == 0
        assert t["btts_no_count"] == 0

    def test_cuenta_picks_y_promedia_goles(self):
        pronosticos = [
            {
                "goles_esperados_local": 1.5,
                "goles_esperados_visitante": 1.0,
                "pick_ou": "Over",
                "pick_btts": "S\u00ed",
            },
            {
                "goles_esperados_local": 0.8,
                "goles_esperados_visitante": 0.7,
                "pick_ou": "Under",
                "pick_btts": "No",
            },
        ]
        t = _totales_jornada(pronosticos)
        assert t["partidos"] == 2
        assert t["over_25_count"] == 1
        assert t["under_25_count"] == 1
        assert t["btts_si_count"] == 1
        assert t["btts_no_count"] == 1
        assert abs(t["goles_esperados_total"] - 4.0) < 0.05
        assert abs(t["promedio_goles_partido"] - 2.0) < 0.05


class TestMarcadorAFavor:
    def test_local_conserva_el_orden(self):
        assert _marcador_a_favor("2-1", True) == "2-1"

    def test_visitante_invierte(self):
        assert _marcador_a_favor("2-1", False) == "1-2"

    def test_limpia_espacios(self):
        assert _marcador_a_favor(" 3 - 0 ", True) == "3-0"

    def test_texto_invalido_se_devuelve_igual(self):
        assert _marcador_a_favor("sin marcador", True) == "sin marcador"

    def test_vacio(self):
        assert _marcador_a_favor("", True) == ""
