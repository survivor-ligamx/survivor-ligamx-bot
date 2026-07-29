"""Registro curado de fuentes para disponibilidad de plantillas Liga MX."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from src.team_normalizer import canonical_team_key


@dataclass(frozen=True)
class FuenteEquipo:
    nombre: str
    url: str
    nivel: str

    @property
    def dominio(self) -> str:
        return urlparse(self.url).netloc.lower().removeprefix("www.")


FUENTES_POR_EQUIPO: dict[str, tuple[FuenteEquipo, ...]] = {
    "america": (FuenteEquipo("Club América", "https://www.clubamerica.com.mx/", "oficial"), FuenteEquipo("Águilas Monumental", "https://americamonumental.bolavip.com/", "especializada"), FuenteEquipo("TUDN América", "https://www.tudn.com/futbol/liga-mx/america", "respaldo"), FuenteEquipo("MARCA América", "https://www.marca.com/mx/organizacion/club-america.html", "respaldo")),
    "guadalajara": (FuenteEquipo("Chivas", "https://www.chivasdecorazon.com.mx/", "oficial"), FuenteEquipo("Rebaño Pasión", "https://chivaspasion.bolavip.com/", "especializada"), FuenteEquipo("El Informador Chivas", "https://www.informador.mx/chivas-t23", "especializada"), FuenteEquipo("SI Chivas", "https://www.si.com/es-us/futbol/equipo/chivas", "respaldo")),
    "pumas unam": (FuenteEquipo("Pumas", "https://pumas.mx/", "oficial"), FuenteEquipo("TUDN Pumas", "https://www.tudn.com/futbol/liga-mx/pumas-unam", "respaldo"), FuenteEquipo("Bolavip Pumas", "https://bolavip.com/mx/pumas", "especializada"), FuenteEquipo("ESPN Pumas", "https://espndeportes.espn.com/futbol/equipo/_/id/233/pumas-unam", "respaldo")),
    "cruz azul": (FuenteEquipo("Cruz Azul", "https://cfcruzazul.com/noticias/", "oficial"), FuenteEquipo("TUDN Cruz Azul", "https://www.tudn.com/futbol/liga-mx/cruz-azul", "respaldo"), FuenteEquipo("SI Cruz Azul", "https://www.si.com/es-us/futbol/equipo/cruz-azul", "respaldo"), FuenteEquipo("ESPN Cruz Azul", "https://espndeportes.espn.com/futbol/equipo/_/id/218/cruz-azul", "respaldo")),
    "monterrey": (FuenteEquipo("Rayados", "https://www.rayados.com/", "oficial"), FuenteEquipo("SI Monterrey", "https://www.si.com/es-us/futbol/equipo/monterrey", "respaldo"), FuenteEquipo("TUDN Monterrey", "https://www.tudn.com/futbol/liga-mx/monterrey", "respaldo"), FuenteEquipo("ABC Rayados", "https://abcnoticias.mx/temas/rayados-416.html", "especializada")),
    "tigres uanl": (FuenteEquipo("Tigres", "https://www.tigres.com.mx/es/noticias/", "oficial"), FuenteEquipo("Mediotiempo Tigres", "https://www.mediotiempo.com/temas/tigres", "especializada"), FuenteEquipo("TUDN Tigres", "https://www.tudn.com/futbol/liga-mx/tigres", "respaldo"), FuenteEquipo("ESPN Tigres", "https://espndeportes.espn.com/futbol/equipo/_/id/232/mex.un_leon", "respaldo")),
    "toluca": (FuenteEquipo("Toluca", "https://www.tolucafc.com/", "oficial"), FuenteEquipo("Bolavip Toluca", "https://bolavip.com/mx/toluca-fc", "especializada"), FuenteEquipo("TUDN Toluca", "https://www.tudn.com/futbol/liga-mx/toluca", "respaldo"), FuenteEquipo("SI Toluca", "https://www.si.com/es-us/futbol/equipo/toluca", "respaldo")),
    "pachuca": (FuenteEquipo("Tuzos", "https://tuzos.com.mx/", "oficial"), FuenteEquipo("Hidalgo Sport", "https://hidalgosport.com/category/tuzos/", "especializada"), FuenteEquipo("Transfermarkt Pachuca", "https://www.transfermarkt.es/cf-pachuca/sperrenundverletzungen/verein/4035", "respaldo"), FuenteEquipo("Tineus Pachuca", "https://tineus.mx/deportes/futbol/club-de-futbol-pachuca", "respaldo")),
    "santos": (FuenteEquipo("Santos", "https://www.clubsantos.mx/", "oficial"), FuenteEquipo("El Siglo Santos", "https://www.elsiglodetorreon.com.mx/noticias/santos-laguna.html", "especializada"), FuenteEquipo("TUDN Santos", "https://www.tudn.com/futbol/liga-mx/santos-laguna", "respaldo"), FuenteEquipo("SI Santos", "https://www.si.com/es-us/futbol/equipo/santos-laguna", "respaldo")),
    "atlas": (FuenteEquipo("Atlas", "https://www.atlasfc.com.mx/noticias", "oficial"), FuenteEquipo("El Informador Atlas", "https://www.informador.mx/atlas-t24", "especializada"), FuenteEquipo("TUDN Atlas", "https://www.tudn.com/futbol/liga-mx/atlas", "respaldo"), FuenteEquipo("SI Atlas", "https://www.si.com/es-us/futbol/equipo/atlas", "respaldo")),
    "necaxa": (FuenteEquipo("Necaxa", "https://www.clubnecaxa.mx/noticias", "oficial"), FuenteEquipo("TUDN Necaxa", "https://www.tudn.com/futbol/liga-mx/necaxa", "respaldo"), FuenteEquipo("SI Necaxa", "https://www.si.com/es-us/futbol/equipo/necaxa", "respaldo"), FuenteEquipo("Mediotiempo Necaxa", "https://www.mediotiempo.com/temas/necaxa", "especializada")),
    "puebla": (FuenteEquipo("Club Puebla", "https://x.com/ClubPueblaMX", "oficial"), FuenteEquipo("Grada Puebla", "https://grada.mx/tags/club-puebla", "especializada"), FuenteEquipo("TUDN Puebla", "https://www.tudn.com/futbol/liga-mx/puebla", "respaldo"), FuenteEquipo("Milenio Puebla", "https://www.milenio.com/temas/club-puebla", "respaldo")),
    "leon": (FuenteEquipo("Club León", "https://x.com/clubleonfc", "oficial"), FuenteEquipo("Soy Fiera", "https://www.soyfiera.com/", "especializada"), FuenteEquipo("TUDN León", "https://www.tudn.com/futbol/liga-mx/leon", "respaldo"), FuenteEquipo("SI León", "https://www.si.com/es-us/futbol/equipo/leon", "respaldo")),
    "tijuana": (FuenteEquipo("Xolos", "https://www.xolos.com.mx/", "oficial"), FuenteEquipo("Transfermarkt Tijuana", "https://www.transfermarkt.es/club-tijuana/sperrenundverletzungen/verein/13353", "respaldo"), FuenteEquipo("TUDN Tijuana", "https://www.tudn.com/futbol/liga-mx/club-tijuana", "respaldo"), FuenteEquipo("SI Tijuana", "https://www.si.com/es-us/futbol/equipo/tijuana", "respaldo")),
    "queretaro": (FuenteEquipo("Querétaro", "https://www.qfc.mx/", "oficial"), FuenteEquipo("Diario de Querétaro", "https://oem.com.mx/diariodequeretaro/tags/temas/gallos-blancos", "especializada"), FuenteEquipo("MARCA Querétaro", "https://www.marca.com/mx/organizacion/queretaro-fc.html", "respaldo"), FuenteEquipo("SI Querétaro", "https://www.si.com/es-us/futbol/equipo/queretaro", "respaldo")),
    "juarez": (FuenteEquipo("FC Juárez", "https://fcjuarez.com/", "oficial"), FuenteEquipo("SI Juárez", "https://www.si.com/es-us/futbol/equipo/juarez", "respaldo"), FuenteEquipo("MARCA Juárez", "https://www.marca.com/mx/organizacion/fc-juarez.html", "respaldo"), FuenteEquipo("FC Juárez X", "https://x.com/fcjuarezoficial", "oficial")),
    "atletico de san luis": (FuenteEquipo("Atlético San Luis", "https://www.atleticodesanluis.mx/", "oficial"), FuenteEquipo("TUDN San Luis", "https://www.tudn.com/futbol/liga-mx/atletico-san-luis", "respaldo"), FuenteEquipo("SI San Luis", "https://www.si.com/es-us/futbol/equipo/san-luis", "respaldo"), FuenteEquipo("MARCA San Luis", "https://www.marca.com/mx/organizacion/atletico-san-luis.html", "respaldo")),
    "atlante": (FuenteEquipo("Atlante", "https://atlantefutbol.com/category/noticias/", "oficial"), FuenteEquipo("MARCA Atlante", "https://www.marca.com/mx/organizacion/atlante-fc.html", "respaldo"), FuenteEquipo("TUDN Atlante", "https://www.tudn.com/futbol/ascenso-mx/atlante", "respaldo"), FuenteEquipo("ESPN Atlante", "https://espndeportes.espn.com/futbol/equipo/_/id/226/atlante", "respaldo")),
}


def fuentes_equipo(equipo: str) -> tuple[FuenteEquipo, ...]:
    return FUENTES_POR_EQUIPO.get(canonical_team_key(equipo), ())


def dominios_equipo(equipo: str) -> list[str]:
    return list(dict.fromkeys(f.dominio for f in fuentes_equipo(equipo) if f.dominio))


def nivel_url(url: str, equipo: str) -> str:
    dominio = urlparse(str(url)).netloc.lower().removeprefix("www.")
    for fuente in fuentes_equipo(equipo):
        if dominio == fuente.dominio or dominio.endswith("." + fuente.dominio):
            return fuente.nivel
    return "desconocida"
