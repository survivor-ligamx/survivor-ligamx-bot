from pathlib import Path

p = Path('src/planificador_survivor.py')
s = p.read_text()
old = '''            score = superv + (max(0.0, peso_victoria) * 0.01 * vict)
            best_score = mejor_superv + (max(0.0, peso_victoria) * 0.01 * max(0.0, mejor_vict))
            if score > best_score + 1e-12 or (abs(score - best_score) <= 1e-12 and vict > mejor_vict):
                mejor_superv = superv
                mejor_vict = vict
                mejor_eq = k
                mejor_estado = prox_estado
'''
new = '''            # Objetivo lexicográfico: la supervivencia siempre manda. Las
            # victorias esperadas solo desempatan cuando peso_victoria > 0;
            # así no se sacrifica supervivencia por un multiplicador arbitrario.
            mejora_supervivencia = superv > mejor_superv + 1e-12
            empate_supervivencia = abs(superv - mejor_superv) <= 1e-12
            mejora_desempate = peso_victoria > 0 and vict > mejor_vict + 1e-12
            if mejora_supervivencia or (empate_supervivencia and mejora_desempate):
                mejor_superv = superv
                mejor_vict = vict
                mejor_eq = k
                mejor_estado = prox_estado
'''
assert old in s
s = s.replace(old, new)
old = '''            "vida_empate_disponible_asumida": bool(vida_estado),
            "nivel": _nivel_estrategico(c["p_no_perder"], c["p_ganar"], es_local, es_arranque),
        }
'''
new = '''            "vida_empate_disponible_asumida": bool(vida_estado),
            "ruta_representativa": True,
            "nivel": _nivel_estrategico(c["p_no_perder"], c["p_ganar"], es_local, es_arranque),
        }
        # La DP es una política adaptativa: el pick futuro puede cambiar según
        # se conserve o se consuma la vida. Exponemos la alternativa cuando existe.
        alt_idx = decision.get((i, usados_mask, 0 if vida_estado else 1))
        if alt_idx is not None and alt_idx != ki:
            alt = celdas.get((jnum, equipos[alt_idx]))
            if alt is not None:
                item["alternativa_si_estado_vida_cambia"] = {
                    "equipo": alt["equipo"],
                    "rival": alt["rival"],
                    "condicion": alt["condicion"],
                }
'''
assert old in s
s = s.replace(old, new)
old = '''        "prob_supervivencia_total_pct": round(100.0 * prob_superv, 2),
        "victorias_esperadas": round(vict_esp, 2),
'''
new = '''        "prob_supervivencia_total_pct": round(100.0 * prob_superv, 2),
        "tipo_plan": "politica_adaptativa_por_estado_de_vida",
        "nota_plan": (
            "La probabilidad total corresponde a una política adaptativa: "
            "los picks futuros se recalculan según la vida de empate siga disponible o se consuma. "
            "La lista principal muestra una ruta representativa."
        ),
        "estados_dp_evaluados": _dp.cache_info().currsize,
        "victorias_esperadas": round(vict_esp, 2),
'''
assert old in s
s = s.replace(old, new)
p.write_text(s)

t = Path('tests/test_planificador_survivor.py')
ts = t.read_text()
marker = '\ndef test_plan_expone_politica_adaptativa_y_complejidad_acotada():\n'
if marker not in ts:
    ts += '''\n\ndef test_plan_expone_politica_adaptativa_y_complejidad_acotada():
    fuerzas = ps.pm.calcular_fuerzas(_resultados())
    r = ps.planificar(_calendario(), fuerzas)
    assert r["tipo_plan"] == "politica_adaptativa_por_estado_de_vida"
    assert "política adaptativa" in r["nota_plan"]
    assert 0 < r["estados_dp_evaluados"] <= 2 * (2 ** r["equipos_disponibles"])
'''
    t.write_text(ts)

Path('tools/apply_manual_review_fixes.py').unlink()
Path('.github/workflows/apply-manual-review-fixes.yml').unlink()
