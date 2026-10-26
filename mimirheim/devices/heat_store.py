"""HeatStoreDevice: models optional boosts of a thermal store in the MILP.

A heat store is a DHW heat pump boiler, a pool, a hot tub or any other store of
heat whose own program keeps it at a normal setpoint. mimirheim does not model
or control that program; it remains part of the static load forecast. This
module plans only optional boosts above the normal setpoint, at one of the
configured boost stages, and values the heat they store with a credit for the
normal-program electricity that heat later displaces.

The device works in energy terms. ``heat_store_energy`` is a pure translation
from the temperature-based configuration and the measured temperature into the
energy parameters the model uses, so a later energy-based configuration mode
needs only a second translation function.

The module does not import from ``mimirheim.io``. The measured temperature, the
prices and the pre-solve import baseline are passed to ``add_constraints``
explicitly. Hot water draws, minimum run length and deny windows are not
modelled here. See IMPLEMENTATION_DETAILS.md §8, subsection "Heat store model".
"""

import math
from dataclasses import dataclass
from typing import Any

from mimirheim.config.schema import HeatStoreConfig
from mimirheim.core.bundle import HeatStoreInputs
from mimirheim.core.context import ModelContext


@dataclass(frozen=True)
class HeatStoreEnergy:
    """Energy parameters of a heat store for one solve.

    All energies are heat above the normal setpoint, in kWh.

    Attributes:
        capacity_kwh_per_k: Heat needed to raise the store by 1 K.
        stage_cap_kwh: Stored heat at which each stage stops heating, in stage
            order.
        max_stored_kwh: Highest stage cap; the most heat the store can hold.
        stage_power_kw: Electrical power of each stage at full power.
        stage_cop: Efficiency of each stage.
        maintenance_kwh: Heat the store loses per step at the normal setpoint.
            Stored heat covers this loss instead of the normal program.
        decay: Fraction of stored heat lost per step on top of the
            maintenance loss, because a hotter store loses heat faster.
        initial_stored_kwh: Stored heat at the start of the horizon.
    """

    capacity_kwh_per_k: float
    stage_cap_kwh: tuple[float, ...]
    max_stored_kwh: float
    stage_power_kw: tuple[float, ...]
    stage_cop: tuple[float, ...]
    maintenance_kwh: float
    decay: float
    initial_stored_kwh: float


def heat_store_energy(
    config: HeatStoreConfig, measured_temp_c: float, dt: float
) -> HeatStoreEnergy:
    """Translate a temperature-based heat store configuration into energy terms.

    A pure function: the same configuration, measurement and step length
    always give the same parameters. See IMPLEMENTATION_DETAILS.md §8,
    subsection "Heat store model", for the derivation of each parameter.

    Args:
        config: Validated heat store configuration.
        measured_temp_c: Measured store temperature in degrees C. Below the
            normal setpoint it counts as no stored heat; above the highest
            stage cap it counts as a full store.
        dt: Step duration in hours.

    Returns:
        The energy parameters for this solve.
    """
    c = config.heat_capacity_kwh_per_k
    setpoint = config.normal_setpoint_c
    stage_cap = tuple(c * (s.max_temp_c - setpoint) for s in config.boost_stages)
    max_stored = max(stage_cap)
    # Newton cooling: extra heat above the setpoint decays with the same time
    # constant as the store loses its temperature difference to ambient.
    decay = 1.0 - math.exp(-dt * config.drift_k_per_hour / (setpoint - config.ambient_c))
    initial = min(max(c * (measured_temp_c - setpoint), 0.0), max_stored)
    return HeatStoreEnergy(
        capacity_kwh_per_k=c,
        stage_cap_kwh=stage_cap,
        max_stored_kwh=max_stored,
        stage_power_kw=tuple(s.power_kw for s in config.boost_stages),
        stage_cop=tuple(s.cop for s in config.boost_stages),
        maintenance_kwh=c * config.drift_k_per_hour * dt,
        decay=decay,
        initial_stored_kwh=initial,
    )


def _covered_steps(energy: HeatStoreEnergy, horizon: int) -> set[int]:
    """Return the steps in which heat already in the store covers the maintenance loss.

    Follows the no-boost trajectory from the initial stored heat: each step
    loses its extra decay, then the maintenance loss. A step is covered when
    the heat available at its start, after decay, is at least the
    maintenance loss.

    Args:
        energy: Energy parameters for this solve.
        horizon: Number of steps.

    Returns:
        Indices of the covered steps. They form a prefix of the horizon.
    """
    covered: set[int] = set()
    stored = energy.initial_stored_kwh
    for t in range(horizon):
        available = (1 - energy.decay) * stored
        if available < energy.maintenance_kwh:
            break
        covered.add(t)
        stored = available - energy.maintenance_kwh
    return covered


class HeatStoreDevice:
    """Models a heat store's optional boosts as a MILP sub-problem.

    Each instance corresponds to one entry in ``config.heat_stores``. The model
    builder calls ``add_variables`` once, then ``add_constraints`` with the
    measured temperature, the prices and the import baseline.

    The device exposes the following solver variables after ``add_variables``:

        _on[s, t]    Binary; 1 = boost commanded at stage s during step t.
        _p[s, t]     Electrical draw at stage s during step t, in kW.
        _at_cap[t]   Binary; 1 = the store ends step t at the active stage cap.
        _w[t]        Stored heat used instead of the normal program, in kWh.
        _imp[t]      Normal-program electricity _w[t] displaces that would have
                     been imported, in kWh.
        _rest[t]     The remainder of that electricity, in kWh.
        _S[t]        Stored heat above the normal setpoint at the end of step
                     t, in kWh.

    Attributes:
        name: Device name matching the key in ``config.heat_stores``.
        config: Static heat store configuration.
    """

    def __init__(self, name: str, config: HeatStoreConfig) -> None:
        """Initialise the HeatStoreDevice.

        Args:
            name: Device name, matching the key in ``MimirheimConfig.heat_stores``.
            config: Validated static configuration for this heat store.
        """
        self.name = name
        self.config = config

        # Solver variables populated by add_variables.
        self._on: dict[tuple[int, int], Any] = {}
        self._p: dict[tuple[int, int], Any] = {}
        self._at_cap: dict[int, Any] = {}
        self._w: dict[int, Any] = {}
        self._imp: dict[int, Any] = {}
        self._rest: dict[int, Any] = {}
        self._S: dict[int, Any] = {}

        # Per-step credit coefficients, set in add_constraints.
        self._import_credit: list[float] = []
        self._export_credit: list[float] = []
        self._dt: float = 0.25

    def add_variables(self, ctx: ModelContext) -> None:
        """Declare all MILP variables for this heat store.

        Bounds that depend on the energy translation (the stage caps and the
        maintenance loss) are added as constraints in ``add_constraints``,
        because the translation needs the measured temperature.

        Args:
            ctx: The current solve context. Variables are registered on
                ``ctx.solver``.
        """
        self._dt = ctx.dt
        stages = range(len(self.config.boost_stages))

        for t in ctx.T:
            for s in stages:
                # on[s, t]: 1 when the integration is told to boost at stage
                # s + 1 during step t. The command the integration acts on.
                self._on[s, t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)
                # p[s, t]: electrical draw in kW at stage s. Bounded by the
                # stage power and tied to on[s, t] in add_constraints.
                self._p[s, t] = ctx.solver.add_var(
                    lb=0.0, ub=self.config.boost_stages[s].power_kw
                )
            # at_cap[t]: 1 when the store ends step t at the active stage's
            # cap. Only then may the draw be below full stage power.
            self._at_cap[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)
            # w[t]: stored heat in kWh used instead of the normal program.
            # Upper bound (the maintenance loss) is set in add_constraints.
            self._w[t] = ctx.solver.add_var(lb=0.0)
            # imp[t], rest[t]: normal-program electricity in kWh that w[t]
            # displaces, split into the part that would have been imported
            # and the remainder, which would have run on surplus PV.
            self._imp[t] = ctx.solver.add_var(lb=0.0)
            self._rest[t] = ctx.solver.add_var(lb=0.0)
            # S[t]: stored heat above the normal setpoint at the end of step
            # t, in kWh. The stage-cap constraints bound it from above.
            self._S[t] = ctx.solver.add_var(lb=0.0)

    def add_constraints(
        self,
        ctx: ModelContext,
        inputs: HeatStoreInputs,
        import_prices: list[float],
        export_prices: list[float],
        baseline_import_kwh: list[float],
    ) -> None:
        """Add all MILP constraints for this heat store.

        See IMPLEMENTATION_DETAILS.md §8, subsection "Heat store model", for
        the derivation of each constraint.

        Args:
            ctx: The current solve context.
            inputs: Measured store temperature.
            import_prices: Import price per step in EUR/kWh.
            export_prices: Export price per step in EUR/kWh.
            baseline_import_kwh: Grid import per step before any device
                dispatch, in kWh, from ``_baseline_import_kwh``.
        """
        energy = heat_store_energy(self.config, inputs.current_temp_c, ctx.dt)
        stages = range(len(energy.stage_cap_kwh))
        s_max = energy.max_stored_kwh
        covered_steps = _covered_steps(energy, len(ctx.T))

        # Credit prices clamped at 0, so the credit is never negative and
        # never makes boosting look worse than not boosting.
        self._import_credit = [max(price, 0.0) for price in import_prices[: len(ctx.T)]]
        self._export_credit = [max(price, 0.0) for price in export_prices[: len(ctx.T)]]

        for t in ctx.T:
            on = [self._on[s, t] for s in stages]
            p = [self._p[s, t] for s in stages]

            # One stage at a time: the integration commands a single device
            # mode per step, so at most one stage binary may be 1.
            ctx.solver.add_constraint(sum(on) <= 1)

            for s in stages:
                power = energy.stage_power_kw[s]
                # p[s, t] in kW is zero unless stage s is commanded, and at
                # most the stage power; without this the draw would float
                # free of the command the integration acts on.
                ctx.solver.add_constraint(p[s] <= power * on[s])
                # Full stage power unless the store ends the step at its cap,
                # where the draw is only what fits.
                ctx.solver.add_constraint(
                    p[s] >= power * on[s] - power * self._at_cap[t]
                )
                # Stage s stops heating at its cap: while on[s, t] = 1, S[t]
                # in kWh may not exceed S_cap[s]. While it is 0, the bound
                # relaxes to S_max, which the store never exceeds anyway (a
                # "big-M" switch: a large constant M = S_max - S_cap[s] that
                # turns the limit off). This also stops a lower-cap stage
                # running once a higher-cap stage has heated past its cap.
                ctx.solver.add_constraint(
                    self._S[t]
                    <= energy.stage_cap_kwh[s] + (s_max - energy.stage_cap_kwh[s]) * (1 - on[s])
                )

            # S[t] in kWh never exceeds S_max, the highest stage cap: no stage
            # heats beyond it. With a stage on, the stage-cap constraint above
            # is tighter.
            ctx.solver.add_constraint(self._S[t] <= s_max)

            # at_cap[t] = 1 is allowed only when S[t] in kWh reaches the
            # active stage's cap. Without it the solver could claim "at cap"
            # anywhere and plan partial draw below full stage power. With
            # at_cap = 0 the bound relaxes to S_max below the cap (big-M).
            ctx.solver.add_constraint(
                self._S[t]
                >= sum(energy.stage_cap_kwh[s] * on[s] for s in stages)
                - s_max * (1 - self._at_cap[t])
            )

            # Energy balance: the previous stored heat less its extra decay,
            # plus boost heat, less stored heat used instead of the normal
            # program. S[-1] is the measured stored heat.
            prior: Any = energy.initial_stored_kwh if t == 0 else self._S[t - 1]
            ctx.solver.add_constraint(
                self._S[t]
                == (1 - energy.decay) * prior
                + sum(energy.stage_cop[s] * p[s] * ctx.dt for s in stages)
                - self._w[t]
            )

            # Stored heat replaces at most the normal program's maintenance
            # heat for this step.
            ctx.solver.add_constraint(self._w[t] <= energy.maintenance_kwh)
            # Where the store without any boost still holds enough heat, it
            # physically covers the full maintenance loss. Boosts only add
            # heat, so this holds in every plan. It stops the solver holding
            # back heat for steps with a higher credit. See
            # IMPLEMENTATION_DETAILS.md §8, subsection "Heat store model".
            if t in covered_steps:
                ctx.solver.add_constraint(self._w[t] == energy.maintenance_kwh)

            # The normal-program electricity in kWh that w[t] displaces,
            # w[t] / normal_cop, split into imp[t] (would have been imported)
            # and rest[t] (would have run on PV). imp[t] is capped by the
            # pre-solve import baseline in kWh; anything above it was PV.
            ctx.solver.add_constraint(
                self._w[t] * (1.0 / self.config.normal_cop) == self._imp[t] + self._rest[t]
            )
            ctx.solver.add_constraint(self._imp[t] <= baseline_import_kwh[t])

    def net_power(self, t: int) -> Any:
        """Net AC power drawn from the home bus at step t, in kW.

        Negative while boosting (the Device Protocol counts consumption as
        negative), zero otherwise.

        Args:
            t: Zero-based time step index.

        Returns:
            Solver expression for the negated sum of the stage draws.
        """
        return -sum(self._p[s, t] for s in range(len(self.config.boost_stages)))

    def objective_terms(self, t: int) -> list[Any]:
        """Return wear-style objective terms for step t.

        A heat store has none; its credit is an economic term.

        Args:
            t: Zero-based time step index.

        Returns:
            An empty list.
        """
        return []

    def economic_terms(self, t: int) -> list[Any]:
        """Return the credit for displaced normal-program electricity at step t.

        The credit is a negative cost in the confidence-weighted economic
        block, in the same EUR-per-kW-step units as grid cost, hence the
        division by dt. See IMPLEMENTATION_DETAILS.md §10, subsection "Device
        economic terms".

        Args:
            t: Zero-based time step index.

        Returns:
            A list with one solver expression.
        """
        return [
            -(
                self._import_credit[t] * self._imp[t]
                + self._export_credit[t] * self._rest[t]
            )
            * (1.0 / self._dt)
        ]

    def boost_stage(self, ctx: ModelContext, t: int) -> int:
        """Return the boost stage the solved plan commands at step t.

        Args:
            ctx: The solved model context.
            t: Zero-based time step index.

        Returns:
            The stage number counted from 1, or 0 when not boosting.
        """
        for s in range(len(self.config.boost_stages)):
            if ctx.solver.var_value(self._on[s, t]) > 0.5:
                return s + 1
        return 0

    def temp_c(self, ctx: ModelContext, t: int) -> float:
        """Return the planned store temperature at the end of step t.

        Args:
            ctx: The solved model context.
            t: Zero-based time step index.

        Returns:
            Planned temperature in degrees C: the normal setpoint plus the
            stored heat divided by the heat capacity.
        """
        return (
            self.config.normal_setpoint_c
            + ctx.solver.var_value(self._S[t]) / self.config.heat_capacity_kwh_per_k
        )
