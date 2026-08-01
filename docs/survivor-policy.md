# Política adaptativa de Survivor

El planificador modela una sola vida de empate.

- Con vida: victoria conserva la vida, empate la consume y derrota elimina.
- Sin vida: únicamente una victoria permite continuar.
- La probabilidad total corresponde a una política adaptativa: los picks futuros pueden cambiar según el estado real de la vida.
- `plan` muestra una ruta representativa y `alternativa_si_estado_vida_cambia` informa cuando el pick sería diferente bajo el otro estado.
- La optimización es lexicográfica: primero maximiza supervivencia y después usa victorias esperadas como desempate. Nunca sacrifica supervivencia por una bonificación arbitraria.

La respuesta incluye `estados_dp_evaluados` para vigilar la complejidad de la programación dinámica.
