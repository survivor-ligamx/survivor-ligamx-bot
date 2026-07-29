from src.fuentes_equipos import FUENTES_POR_EQUIPO, dominios_equipo, fuentes_equipo, nivel_url


def test_hay_cuatro_fuentes_para_los_18_equipos():
    assert len(FUENTES_POR_EQUIPO) == 18
    assert all(len(fuentes) >= 4 for fuentes in FUENTES_POR_EQUIPO.values())


def test_america_incluye_fuente_oficial_y_america_monumental():
    fuentes = fuentes_equipo("América")
    assert any(f.nivel == "oficial" for f in fuentes)
    assert "americamonumental.bolavip.com" in dominios_equipo("América")
    assert nivel_url("https://americamonumental.bolavip.com/noticias/perea", "América") == "especializada"


def test_aliases_resuelven_equipos_del_calendario():
    assert len(fuentes_equipo("Chivas Guadalajara")) >= 4
    assert len(fuentes_equipo("Xolos de Tijuana")) >= 4
    assert len(fuentes_equipo("Atlético de San Luis")) >= 4
