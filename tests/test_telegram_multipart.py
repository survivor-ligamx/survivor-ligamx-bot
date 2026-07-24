#!/usr/bin/env python3
"""Pruebas focalizadas de división y entrega multipart de Telegram."""

from __future__ import annotations

import os
from unittest import mock

from src.telegram import envio


def test_dividir_mensaje_dentro_del_limite_no_modifica_contenido():
    mensaje = "a" * 4000

    assert envio._dividir_mensaje(mensaje) == [mensaje]


def test_dividir_mensaje_largo_respeta_limite_orden_y_contenido():
    lineas = [f"línea-{indice}:" + (str(indice) * 500) for indice in range(12)]
    mensaje = "\n".join(lineas)

    partes = envio._dividir_mensaje(mensaje)

    assert len(partes) > 1
    assert all(len(parte) <= 4000 for parte in partes)
    assert "\n".join(partes) == mensaje


def test_dividir_linea_individual_extremadamente_larga():
    mensaje = "x" * 8501

    partes = envio._dividir_mensaje(mensaje)

    assert [len(parte) for parte in partes] == [4000, 4000, 501]
    assert all(len(parte) <= 4000 for parte in partes)
    assert "".join(partes) == mensaje


def test_fallo_de_una_parte_no_impide_intentar_las_siguientes():
    respuestas = [
        mock.Mock(status_code=200, text="ok"),
        mock.Mock(status_code=500, text="error"),
        mock.Mock(status_code=200, text="ok"),
    ]
    with (
        mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "chat"}),
        mock.patch.object(envio, "_dividir_mensaje", return_value=["uno", "dos", "tres"]),
        mock.patch.object(envio.requests, "post", side_effect=respuestas) as post,
    ):
        resultado = envio.enviar_mensaje("mensaje")

    assert resultado is False
    assert post.call_count == 3
    assert [llamada.kwargs["data"]["text"] for llamada in post.call_args_list] == ["uno", "dos", "tres"]


def test_idempotencia_es_independiente_distinta_y_estable_por_parte():
    respuesta = mock.Mock(status_code=200, text="ok")
    claves: list[str] = []

    def reclamar(clave: str) -> bool:
        claves.append(clave)
        return True

    with (
        mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "chat"}),
        mock.patch.object(envio, "_dividir_mensaje", return_value=["uno", "dos"]),
        mock.patch.object(envio.requests, "post", return_value=respuesta),
        mock.patch("src.database.reclamar_entrega_telegram", side_effect=reclamar),
        mock.patch("src.database.completar_entrega_telegram"),
    ):
        assert envio.enviar_mensaje("mensaje", idempotency_key="plan:25")
        assert envio.enviar_mensaje("mensaje", idempotency_key="plan:25")

    assert claves[0] != claves[1]
    assert claves[:2] == claves[2:]
    assert claves[0].startswith("plan:25:parte:1:")
    assert claves[1].startswith("plan:25:parte:2:")
