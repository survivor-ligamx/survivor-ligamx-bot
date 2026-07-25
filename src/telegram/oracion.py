from __future__ import annotations

from typing import Any, Dict, List, Optional


def oracion_para_pick(tops: Optional[List[Dict[str, Any]]]) -> str:
    """Oración breve para acompañar el equipo principal de cada jornada."""
    equipo = "el equipo elegido"
    if tops and tops[0].get("equipo"):
        equipo = str(tops[0]["equipo"])
    return (
        f"🙏 <b>Que DIOS bendiga a {equipo}, proteja a sus jugadores y los guíe "
        "con fuerza, unión y valentía para salir a ganar. ¡Con el poder de DIOS!</b>"
    )
