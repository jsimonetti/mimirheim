"""Confidence weighting helpers for the objective builder.

This module provides helpers that consume per-step confidence values: one
scales a solver expression (or constant) by a confidence value, the other
derives the confidence-weighted running minimum import price used to value
heat store boost heat. Confidence is an externally
supplied value in ``SolveBundle.horizon_confidence`` that represents how
reliable the forecast is at each time step.

A step with confidence=1.0 is fully trusted; its economic terms contribute
their full value to the objective. A step with confidence=0.0 is completely
untrusted; its economic terms are zeroed out, making the solver indifferent
to actions at that step.

This module does not produce, decay, or update confidence values. It only
consumes them. Confidence is computed externally (e.g. by a forecast quality
service) and supplied to mimirheim as part of ``SolveBundle``.

This module has no imports from other ``mimirheim`` modules.
"""

from typing import Any


def weight_by_confidence(expr: Any, confidence: float) -> Any:
    """Scale a solver expression or constant by a confidence value.

    Args:
        expr: A solver variable, linear expression, or numeric constant.
        confidence: A value in [0, 1]. If 0, all economic weight is removed
            from this step. If 1, the expression is returned unchanged.

    Returns:
        ``expr * confidence``, or ``0`` if confidence is exactly 0 (to avoid
        multiplying solver expressions by zero, which may produce unexpected
        types in some solver backends).
    """
    if confidence == 0.0:
        return 0
    if confidence == 1.0:
        return expr
    return expr * confidence


def running_min_reference_prices(
    import_prices: list[float], confidence: list[float]
) -> list[float]:
    """Return the reference price at which saved import electricity is valued.

    For each step t the reference price is

        p_ref[t] = max(0, min over s <= t of confidence[s] * import_prices[s])

    The minimum is taken over the confidence-weighted price, the same scaling
    the grid cost terms use, so the curve never increases over the horizon.
    See mimirheim/docs/adr/0001-boost-heat-valued-at-running-minimum-price.md for why
    the running minimum, and not the price at the step, is the right value.

    Args:
        import_prices: Import price per step in EUR/kWh.
        confidence: Forecast confidence per step, in [0, 1].

    Returns:
        Reference price per step in EUR/kWh, non-negative and non-increasing.
        Same length as ``import_prices``.
    """
    reference: list[float] = []
    running = float("inf")
    for price, conf in zip(import_prices, confidence, strict=True):
        running = min(running, conf * price)
        reference.append(max(0.0, running))
    return reference
