#!/usr/bin/env python3
"""Caché TTL en memoria por combinación de argumentos."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
import time
from typing import Any, ParamSpec, TypeVar, cast

P = ParamSpec("P")
R = TypeVar("R")


def _congelar(valor: Any) -> Any:
    """Convierte estructuras mutables comunes en una clave hashable estable."""
    if isinstance(valor, dict):
        pares = ((_congelar(k), _congelar(v)) for k, v in valor.items())
        return ("dict", tuple(sorted(pares, key=repr)))
    if isinstance(valor, list):
        return ("list", tuple(_congelar(v) for v in valor))
    if isinstance(valor, tuple):
        return ("tuple", tuple(_congelar(v) for v in valor))
    if isinstance(valor, (set, frozenset)):
        return ("set", tuple(sorted((_congelar(v) for v in valor), key=repr)))
    try:
        hash(valor)
    except TypeError:
        return ("repr", type(valor).__qualname__, repr(valor))
    return ("hash", valor)


def ttl_cache(segundos: int = 600) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Cachea cada combinación de ``args`` y ``kwargs`` durante el TTL."""

    def deco(fn: Callable[P, R]) -> Callable[P, R]:
        estado: dict[Any, tuple[float, R]] = {}

        @wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            ahora = time.monotonic()
            clave = (_congelar(args), _congelar(kwargs))
            entrada = estado.get(clave)
            if entrada is None or ahora - entrada[0] > segundos:
                valor = fn(*args, **kwargs)
                estado[clave] = (ahora, valor)
                return valor
            return entrada[1]

        def cache_clear() -> None:
            estado.clear()

        setattr(wrapper, "cache_clear", cache_clear)
        return cast(Callable[P, R], wrapper)

    return deco
