from src.telegram.oracion import oracion_para_pick


def test_oracion_incluye_equipo_principal():
    mensaje = oracion_para_pick([{"equipo": "Cruz Azul"}])

    assert "Que DIOS bendiga a Cruz Azul" in mensaje
    assert "salir a ganar" in mensaje
    assert "Con el poder de DIOS" in mensaje


def test_oracion_tiene_fallback_si_no_hay_top():
    mensaje = oracion_para_pick(None)

    assert "Que DIOS bendiga a el equipo elegido" in mensaje
