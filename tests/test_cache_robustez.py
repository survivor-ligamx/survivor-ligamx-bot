#!/usr/bin/env python3
"""Regresiones para caché por argumentos y antigüedad de momios."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from src.cache_ttl import ttl_cache
from src import comparador_mercado as cm


def test_ttl_cache_distingue_argumentos_y_acepta_estructuras_mutables() -> None:
    llamadas: list[tuple] = []

    @ttl_cache(600)
    def calcular(datos, **opciones):
        llamadas.append((datos, opciones))
        return len(llamadas)

    assert calcular([1, 2], config={"a": 1, "b": 2}) == 1
    assert calcular([1, 2], config={"b": 2, "a": 1}) == 1
    assert calcular([1, 3], config={"a": 1, "b": 2}) == 2
    assert len(llamadas) == 2


def test_ttl_cache_cache_clear_y_expiracion() -> None:
    llamadas = 0

    @ttl_cache(10)
    def calcular(valor: int) -> int:
        nonlocal llamadas
        llamadas += 1
        return valor + llamadas

    with mock.patch("src.cache_ttl.time.monotonic", side_effect=[100.0, 105.0, 111.0, 112.0]):
        assert calcular(1) == 2
        assert calcular(1) == 2
        assert calcular(1) == 3
        calcular.cache_clear()
        assert calcular(1) == 4


def _guardar(path: Path, generado_utc, momios=None) -> None:
    payload = {"momios": momios or {"a|b": {"ml": {"local": 2.0}}}}
    if generado_utc is not None:
        payload["generado_utc"] = generado_utc
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_cargar_momios_acepta_timestamp_reciente(tmp_path: Path) -> None:
    path = tmp_path / "momios.json"
    reciente = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _guardar(path, reciente)

    assert cm.cargar_momios(max_edad_horas=72, path=path)


def test_cargar_momios_rechaza_timestamp_vencido(tmp_path: Path) -> None:
    path = tmp_path / "momios.json"
    vencido = (datetime.now(timezone.utc) - timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _guardar(path, vencido)

    assert cm.cargar_momios(max_edad_horas=72, path=path) == {}


def test_cargar_momios_rechaza_timestamp_ausente_o_corrupto(tmp_path: Path) -> None:
    ausente = tmp_path / "ausente.json"
    corrupto = tmp_path / "corrupto.json"
    _guardar(ausente, None)
    _guardar(corrupto, "fecha-invalida")

    assert cm.cargar_momios(max_edad_horas=72, path=ausente) == {}
    assert cm.cargar_momios(max_edad_horas=72, path=corrupto) == {}


def test_cargar_momios_sin_limite_no_exige_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "momios.json"
    _guardar(path, None)

    assert cm.cargar_momios(max_edad_horas=0, path=path)
