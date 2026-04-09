"""Grid device — models the physical connection between the home and the public grid.

The Grid device is architecturally different from battery, EV, and load devices:

- There is exactly one Grid instance per solve (a single ``grid:`` config section,
  not a named map).
- Its variables (``import_[t]``, ``export_[t]``) are the primary economic variables
  referenced directly by ``ObjectiveBuilder``.
- It has no MQTT runtime inputs — its physical limits come entirely from config.

A physical grid connection cannot simultaneously import and export. This constraint
is always enforced via a single binary variable ``_grid_dir[t]`` per step. When it
is 0 the grid may import; when it is 1 the grid may export. Using one binary instead
of two (one per direction) halves the number of integer variables added to the MILP,
reducing the size of the branch-and-bound search tree.

This module does not import from ``mimirheim.io`` or ``mimirheim.config`` beyond accepting a
``GridConfig`` argument at construction. It does not import ``python-mip`` directly;
all solver interaction goes through ``ModelContext.solver`` (a ``SolverBackend``).
"""

from typing import Any

from mimirheim.config.schema import GridConfig
from mimirheim.core.context import ModelContext


class Grid:
    """Models the grid connection limits as MIP variables and bounds.

    Declares three variables per time step:

    - ``import_[t]``: power drawn from the grid (kW), bounded by
      ``import_limit_kw``.
    - ``export_[t]``: power fed into the grid (kW), bounded by
      ``export_limit_kw``.
    - ``_grid_dir[t]``: a single binary that encodes the allowed direction.
      0 means the grid may import (export is forced to zero).
      1 means the grid may export (import is forced to zero).

    A grid connection cannot simultaneously import and export — this is a
    physical property of the meter, not a configurable policy. One binary per
    step (rather than two) is sufficient to enforce mutual exclusion; see
    ``add_constraints`` for the formulation.

    Attributes:
        name: Fixed string ``"grid"``. Used by the power balance assembler and
            the MQTT publisher to identify this device.
        config: The static grid configuration loaded at startup.
        import_: Mapping from time step index to the import variable handle.
            Populated by ``add_variables``; empty before that call.
        export_: Mapping from time step index to the export variable handle.
            Populated by ``add_variables``; empty before that call.
    """

    name: str = "grid"

    def __init__(self, config: GridConfig) -> None:
        """Initialise the Grid device with its static configuration.

        Args:
            config: Validated grid configuration containing import and export
                power limits.
        """
        self.config = config
        self.import_: dict[int, Any] = {}
        self.export_: dict[int, Any] = {}
        # Single binary per step that encodes the allowed flow direction.
        # Populated by add_variables.
        self._grid_dir: dict[int, Any] = {}

    def add_variables(self, ctx: ModelContext) -> None:
        """Declare import, export, and direction variables for every time step.

        For each step ``t`` in ``ctx.T``:

        - ``import_[t]``: Power imported from the grid in kW. Upper bound is
          ``config.import_limit_kw``, the physical limit of the grid connection
          (DNO agreement or main fuse). Without this bound, the solver could
          import unlimited power to charge batteries and export for arbitrage.

        - ``export_[t]``: Power exported to the grid in kW. Upper bound is
          ``config.export_limit_kw``. Without this bound, the solver could
          export unlimited power, violating the DNO connection agreement.

        - ``_grid_dir[t]``: Single binary direction variable.
          0 = importing step (import may be nonzero, export is forced to 0).
          1 = exporting step (export may be nonzero, import is forced to 0).
          The Big-M constraints in ``add_constraints`` enforce this encoding.

        Args:
            ctx: The current solve context. Variables are registered on
                ``ctx.solver`` via ``add_var``.
        """
        for t in ctx.T:
            # import_[t] represents the power drawn from the public grid at
            # time step t, in kilowatts. Lower bound: 0 (import is always
            # non-negative; the sign of the net flow is determined by which
            # of import or export is nonzero). Upper bound: import_limit_kw,
            # the maximum power the grid connection agreement permits.
            self.import_[t] = ctx.solver.add_var(
                lb=0.0,
                ub=self.config.import_limit_kw,
            )

            # export_[t] represents the power fed into the public grid at
            # time step t, in kilowatts. Upper bound: export_limit_kw, the
            # maximum export permitted by the DNO or inverter settings. A
            # zero export limit (zero_export mode) is expressed here as
            # export_limit_kw=0 in config.
            self.export_[t] = ctx.solver.add_var(
                lb=0.0,
                ub=self.config.export_limit_kw,
            )

            # _grid_dir[t]: binary direction selector.
            # The solver chooses 0 (import) or 1 (export) at each step.
            # Big-M constraints in add_constraints link this choice to the
            # continuous import_[t] and export_[t] variables.
            self._grid_dir[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

    def add_constraints(self, ctx: ModelContext, inputs: None) -> None:
        """Couple the direction binary to the import and export variables.

        Two Big-M constraints per time step enforce the single-binary
        direction-exclusion formulation:

        1. ``import_[t] <= import_limit_kw × (1 − grid_dir[t])``
           When ``grid_dir[t] = 1`` (export step), ``(1 − 1) = 0`` and this
           reduces to ``import_[t] <= 0``, forcing import to zero. When
           ``grid_dir[t] = 0`` (import step), this reduces to
           ``import_[t] <= import_limit_kw``, which the variable bound already
           enforces, so the constraint is non-binding. ``import_limit_kw`` is
           the Big-M value: the tightest valid upper bound on ``import_[t]``.

        2. ``export_[t] <= export_limit_kw × grid_dir[t]``
           When ``grid_dir[t] = 0`` (import step) this forces ``export_[t]``
           to zero. When ``grid_dir[t] = 1`` (export step) the constraint is
           non-binding. ``export_limit_kw`` is the Big-M value.

        Together these two constraints mean the solver must choose a direction
        at each step: it sets ``grid_dir[t]`` to 0 or 1 and the Big-M
        constraints silence the inactive direction. No explicit mutual exclusion
        constraint is needed — it is implicit in the single-binary encoding.

        Args:
            ctx: The current solve context.
            inputs: Always ``None`` for the Grid device.
        """
        for t in ctx.T:
            # Big-M for import: import_[t] <= import_limit_kw * (1 - grid_dir[t]).
            # When grid_dir[t]=1: forces import_[t] = 0 (export step).
            # When grid_dir[t]=0: reduces to import_[t] <= import_limit_kw (non-binding).
            ctx.solver.add_constraint(
                self.import_[t] <= self.config.import_limit_kw * (1 - self._grid_dir[t])
            )
            # Big-M for export: export_[t] <= export_limit_kw * grid_dir[t].
            # When grid_dir[t]=0: forces export_[t] = 0 (import step).
            # When grid_dir[t]=1: reduces to export_[t] <= export_limit_kw (non-binding).
            ctx.solver.add_constraint(
                self.export_[t] <= self.config.export_limit_kw * self._grid_dir[t]
            )

    def net_power(self, t: int) -> Any:
        """Return the net power expression at time step t.

        Net power is defined as import minus export. A positive value means
        the home is drawing power from the grid; a negative value means the
        home is feeding power into the grid.

        This expression is used by ``build_and_solve()`` when assembling the
        system-wide power balance constraint:

            sum of all device net_power(t) == 0   (for each t)

        Args:
            t: Time step index within ``ctx.T``.

        Returns:
            A linear expression ``import_[t] - export_[t]``.
        """
        return self.import_[t] - self.export_[t]

    def objective_terms(self, t: int) -> int:
        """Return zero — the Grid device contributes no objective terms directly.

        All economic terms (import cost, export revenue, export penalty) are
        built by ``ObjectiveBuilder``, which holds a direct reference to the
        Grid instance and accesses ``import_[t]`` and ``export_[t]`` directly.
        Placing economic terms here would give the Grid device knowledge of
        prices and strategy, which belongs in the objective layer.

        Args:
            t: Time step index (unused).

        Returns:
            Zero, always.
        """
        return 0
