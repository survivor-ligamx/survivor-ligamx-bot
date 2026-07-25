from src.telegram_webhook import _formatear_mi_survivor


def test_mipick_muestra_vida_consumida_y_pendientes():
    mensaje = _formatear_mi_survivor(
        {
            "temporada": "Apertura-2026",
            "usados": ["Monterrey", "Cruz Azul"],
            "pick_actual": None,
            "picks": [
                {
                    "jornada": 1,
                    "estado": "resuelto",
                    "resultado": "empate",
                    "equipo": "Monterrey",
                    "fecha": "2026-07-16",
                },
                {"jornada": 2, "estado": "resuelto", "resultado": "gano", "equipo": "Cruz Azul", "fecha": "2026-07-21"},
                {"jornada": 3, "estado": "bloqueado", "resultado": None, "equipo": "América", "fecha": "2026-07-31"},
            ],
        }
    )

    assert "Vida de empate" in mensaje
    assert "CONSUMIDA" in mensaje
    assert "otro empate elimina" in mensaje
    assert "Picks pendientes (1)" in mensaje
    assert "América" in mensaje
    assert "Empates: <b>1</b>" in mensaje
