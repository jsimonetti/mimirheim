"""ModelContext — shared solve-cycle state threaded through all model-building calls.

This module defines ``ModelContext``, a short-lived container created once at the
start of each solve in ``build_and_solve()`` and passed to every device and
objective builder method.

``ModelContext`` holds exactly three attributes: the solver instance, the time
index range, and the time step duration. It deliberately does not carry
``SolveBundle`` or ``MimirheimConfig`` — those are passed explicitly at each call site
so the data flow remains visible in the model-building code.

This module imports only from ``mimirheim.core.solver_backend``.
"""

from mimirheim.core.solver_backend import SolverBackend


class ModelContext:
    """Shared context for a single solve cycle.

    Created once per solve in ``build_and_solve()`` and threaded through every
    ``add_variables``, ``add_constraints``, ``net_power``, and
    ``objective_terms`` call across all devices and the objective builder.

    Attributes:
        solver: The live solver backend instance. Devices add variables and
            constraints to this object. All solver interaction goes through
            ``SolverBackend``; devices never import ``mip`` directly.
        T: A ``range`` object covering the time step indices for this solve
            horizon. ``len(ctx.T)`` equals the number of prices in
            ``SolveBundle.horizon_prices``. Devices iterate ``for t in ctx.T``
            to create one variable or constraint per step.
        dt: Time step duration in hours. For a 96-step quarter-hourly horizon,
            ``dt == 0.25``. Used to convert power (kW) to energy (kWh) in SOC
            update constraints and wear cost terms.
    """

    def __init__(self, solver: SolverBackend, horizon: int, dt: float) -> None:
        """Initialise the context for a single solve horizon.

        Args:
            solver: A ``SolverBackend`` instance (typically
                ``CBCSolverBackend``) that has not yet been populated with
                any variables or constraints. The caller must ensure this is
                a fresh instance for each solve cycle.
            horizon: Number of time steps in the optimisation horizon. Must
                equal ``len(bundle.horizon_prices)``.
            dt: Duration of each time step in hours (e.g. ``0.25`` for
                quarter-hourly steps).
        """
        self.solver = solver
        self.T = range(horizon)
        self.dt = dt
