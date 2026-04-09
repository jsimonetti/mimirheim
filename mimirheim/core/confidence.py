"""Confidence weighting helpers for the objective builder.

This module provides a single utility function that scales a solver expression
(or constant) by a per-step confidence value. Confidence is an externally
supplied value in ``SolveBundle.horizon_confidence`` that represents how
reliable the forecast is at each time step.

A step with confidence=1.0 is fully trusted; its economic terms contribute
their full value to the objective. A step with confidence=0.0 is completely
untrusted; its economic terms are zeroed out, making the solver indifferent
to actions at that step.

This module does not produce, decay, or update confidence values. It only
multiplies. Confidence is computed externally (e.g. by a forecast quality
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
