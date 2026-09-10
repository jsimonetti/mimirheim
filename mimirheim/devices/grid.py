"""Grid device — models the physical connection between the home and the public grid.

The Grid device is architecturally different from battery, EV, and load devices:

- There is exactly one Grid instance per solve (a single ``grid:`` config section,
  not a named map).
- Its variables (``import_[t]``, ``export_[t]``) are the primary economic variables
  referenced directly by ``ObjectiveBuilder``.
- It has no MQTT runtime inputs — its physical limits come entirely from config.

A physical grid connection cannot simultaneously import and export; see
IMPLEMENTATION_DETAILS.md §8, subsection "Grid device", for the single-binary
direction encoding used to enforce this and why one binary suffices instead of two.

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
    - ``_grid_dir[t]``: a single binary that encodes the allowed direction (0
      = import, 1 = export). See the module docstring for why one binary is
      enough to enforce mutual exclusion.

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

        For each step: ``import_[t]`` in kW, bounded by ``import_limit_kw``
        (the DNO agreement or main fuse limit); ``export_[t]`` in kW, bounded
        by ``export_limit_kw`` (zero for zero-export mode); and
        ``_grid_dir[t]``, the single binary direction selector coupled to both
        by the Big-M constraints in ``add_constraints``.

        Args:
            ctx: The current solve context. Variables are registered on
                ``ctx.solver`` via ``add_var``.
        """
        for t in ctx.T:
            self.import_[t] = ctx.solver.add_var(
                lb=0.0,
                ub=self.config.import_limit_kw,
            )
            self.export_[t] = ctx.solver.add_var(
                lb=0.0,
                ub=self.config.export_limit_kw,
            )
            self._grid_dir[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

    def add_constraints(self, ctx: ModelContext, inputs: None) -> None:
        """Couple the direction binary to the import and export variables.

        Two Big-M constraints per step: ``import_[t] <= import_limit_kw * (1 -
        grid_dir[t])`` and ``export_[t] <= export_limit_kw * grid_dir[t]``.
        Together they force whichever direction ``grid_dir[t]`` does not
        select to zero, with no separate mutual-exclusion constraint needed —
        see IMPLEMENTATION_DETAILS.md §8, subsection "Grid device".

        Args:
            ctx: The current solve context.
            inputs: Always ``None`` for the Grid device.
        """
        for t in ctx.T:
            ctx.solver.add_constraint(
                self.import_[t] <= self.config.import_limit_kw * (1 - self._grid_dir[t])
            )
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
