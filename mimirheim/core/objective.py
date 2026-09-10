"""ObjectiveBuilder — assembles the MILP objective for each strategy.

This module translates the ``strategy`` field of ``SolveBundle`` into a concrete
minimisation objective on the solver, and adds any hard-cap constraints from
``ConstraintsConfig`` before setting the objective.

Three strategies are supported:

- ``minimize_cost``: minimise net energy cost against time-varying import and
  export prices, with each step weighted by a per-step confidence value.
- ``minimize_consumption``: minimise total grid import lexicographically, then
  maximise export revenue subject to the import bound phase 1 found. This is
  the only strategy that calls ``ctx.solver.solve()`` internally (phase-1
  solve). The caller must call ``ctx.solver.solve()`` once more to complete
  phase 2, using the budget that ``build`` returns. When phase 1 cannot
  establish the bound, phase 2 runs with a weaker one or none and
  ``strategy_degraded`` says so; see ``_minimize_consumption``.
- ``balanced``: weighted sum of cost and self-sufficiency objectives, blended
  according to ``config.objectives.balanced_weights``.

This module imports from ``mimirheim.core`` and ``mimirheim.devices`` but never from
``mimirheim.io``. It does not read configuration files, connect to MQTT, or perform
any I/O.
"""

import logging
from typing import Any

from mimirheim.config.schema import BalancedWeightsConfig, MimirheimConfig
from mimirheim.core.bundle import SolveBundle
from mimirheim.core.confidence import weight_by_confidence
from mimirheim.core.context import ModelContext
from mimirheim.devices.grid import Grid

logger = logging.getLogger("mimirheim.solver")


class ObjectiveBuilder:
    """Assembles the MILP objective and hard-cap constraints for a single solve.

    ``strategy_degraded`` is set when the requested strategy could not be
    carried out and a weaker one was used instead; the caller copies it onto
    the result so the substitution is visible rather than silent.

    An instance carries state — ``strategy_degraded`` and whether the hard caps
    have been added — and ``build()`` resets neither, so a builder is good for
    one solve. ``build_and_solve`` constructs a fresh one each cycle. It is a
    class rather than a plain function so it can be replaced by a test double
    or subclassed to inject alternative objective logic.
    """

    def __init__(self) -> None:
        # Set when a strategy falls back to a weaker objective. Read by the
        # caller after build().
        self.strategy_degraded = False
        # add_hard_cap_constraints is idempotent per builder, so a caller that
        # needs the caps in place before the objective exists can add them
        # early without build() duplicating every row afterwards.
        self._hard_caps_added = False

    def build(
        self,
        ctx: ModelContext,
        devices: list[Any],
        grid: Grid,
        bundle: SolveBundle,
        config: MimirheimConfig,
    ) -> float:
        """Set the objective on ctx.solver according to bundle.strategy.

        This is the sole entry point for objective assembly. It:

        1. Adds hard-cap constraints from ``config.constraints`` (import and
           export power limits) at every time step.
        2. Dispatches to the appropriate strategy implementation.
        3. Returns the wall-clock solver budget the caller has left.

        Most strategies only assemble an objective and leave the whole budget
        to the caller. ``minimize_consumption`` is lexicographic and has to
        solve once inside this method to find the minimum import volume before
        it can constrain phase 2, so it spends part of the budget here.

        The return value exists to make that asymmetry impossible to miss. The
        caller must pass it to its own ``ctx.solver.solve(...)`` rather than
        assuming the full ``config.solver.time_limit_seconds`` is still
        available; doing otherwise lets a two-phase solve run for twice the
        configured budget and block the single-threaded solve loop.

        Args:
            ctx: The model context holding the solver and time horizon.
            devices: All non-grid devices whose ``objective_terms(t)`` values
                contribute wear-cost penalties to the objective.
            grid: The grid device. Its import and export variables are the
                primary economic variables in the objective.
            bundle: Runtime inputs including the strategy name, time-varying
                prices, and per-step confidence values.
            config: Static configuration including strategy weights, optional
                hard-cap constraints, and the solver time budget.

        Returns:
            Seconds of solver wall-clock budget remaining for the caller's
            solve. Equal to ``config.solver.time_limit_seconds`` for
            single-phase strategies, and the unspent remainder for
            ``minimize_consumption``.

        Raises:
            ValueError: If ``bundle.strategy`` is not one of the three
                supported strings.
        """
        self.add_hard_cap_constraints(ctx, grid, config)
        budget = config.solver.time_limit_seconds

        if bundle.strategy == "minimize_cost":
            self._minimize_cost(ctx, devices, grid, bundle, config)
            return budget
        if bundle.strategy == "minimize_consumption":
            return self._minimize_consumption(ctx, devices, grid, bundle, config)
        if bundle.strategy == "balanced":
            self._balanced(ctx, devices, grid, bundle, config)
            return budget
        raise ValueError(f"Unknown strategy: {bundle.strategy!r}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def add_hard_cap_constraints(
        self, ctx: ModelContext, grid: Grid, config: MimirheimConfig
    ) -> None:
        """Add per-step hard caps on import and export power, if configured.

        These caps are hard inequality constraints, not objective penalties.
        The solver will never return a solution that violates them. They are
        independent of strategy and are applied before any objective is set.

        ``build`` calls this itself, so most callers need not. It is public and
        idempotent for the one caller that must: anything solving the model
        *before* the objective is built sees a model without these caps unless
        it adds them first, and would draw conclusions from a trajectory the
        final model forbids.

        Args:
            ctx: Model context with solver and time horizon.
            grid: The grid device whose variables are capped.
            config: Static configuration containing optional maximum limits.
        """
        if self._hard_caps_added:
            return
        self._hard_caps_added = True

        if config.constraints.max_import_kw is not None:
            for t in ctx.T:
                ctx.solver.add_constraint(
                    grid.import_[t] <= config.constraints.max_import_kw
                )
        if config.constraints.max_export_kw is not None:
            for t in ctx.T:
                ctx.solver.add_constraint(
                    grid.export_[t] <= config.constraints.max_export_kw
                )

    def _cost_objective_terms(
        self,
        ctx: ModelContext,
        devices: list[Any],
        grid: Grid,
        bundle: SolveBundle,
        config: MimirheimConfig,
    ) -> list[Any]:
        """Build the confidence-weighted cost objective as a list of terms.

        Shared by ``_minimize_cost`` and by phase 2 of
        ``_minimize_consumption``, which optimise exactly the same expression.
        Phase 2 differs only in the import-volume constraint added before it,
        not in the objective itself, so the two must not be allowed to drift
        apart.

        Terms are produced in step order, each step contributing its economic
        term followed by the wear terms of every device, then the terminal SoC
        and exchange-shaping terms at the end. The order is part of the
        contract: floating-point addition is not associative, so reordering can
        shift the objective in the last bits and change which of two equally
        good schedules the solver returns.

        Args:
            ctx: Model context.
            devices: Non-grid devices contributing wear and terminal SoC terms.
            grid: Grid device providing import and export variables.
            bundle: Runtime inputs with prices and per-step confidence.
            config: Static configuration.

        Returns:
            The objective terms, ready to pass to ``_set_objective``. May be
            empty when every term is a numeric zero.
        """
        terms: list[Any] = []
        for t in ctx.T:
            economic = weight_by_confidence(
                bundle.horizon_prices[t] * grid.import_[t]
                - bundle.horizon_export_prices[t] * grid.export_[t],
                bundle.horizon_confidence[t],
            )
            # weight_by_confidence returns Python int 0 when confidence == 0;
            # only append solver expressions (non-numeric values).
            if not isinstance(economic, (int, float)):
                terms.append(economic)
            for d in devices:
                terms.extend(self._wear_terms(d, t))

        terms.extend(self._terminal_soc_terms(ctx, devices, bundle))
        terms.extend(self._exchange_shaping_terms(ctx, grid, config))
        return terms

    @staticmethod
    def _wear_terms(device: Any, t: int) -> list[Any]:
        """Return a device's objective terms at step ``t`` as a flat list.

        ``Device.objective_terms`` has three possible return shapes: a scalar
        zero when the device has no cost to contribute, a single solver
        expression, or a list of expressions. Numeric values are dropped rather
        than added, because adding a Python int to a solver expression is
        pointless work and some backends object to it.

        Args:
            device: Any device implementing ``objective_terms(t)``.
            t: Time step index.

        Returns:
            Zero or more solver expressions.
        """
        wear = device.objective_terms(t)
        if isinstance(wear, list):
            return [w for w in wear if not isinstance(w, (int, float))]
        if isinstance(wear, (int, float)):
            return []
        return [wear]

    @staticmethod
    def _set_objective(ctx: ModelContext, terms: list[Any]) -> None:
        """Sum ``terms`` and set the result as the minimisation objective.

        An empty list means every contribution was a numeric zero, which
        happens when confidence is zero at every step and no device has a wear
        cost. A constant objective is set instead, leaving the solver to return
        any feasible schedule.

        Args:
            ctx: Model context holding the solver.
            terms: Solver expressions to sum, in the order they should be
                added. See ``_cost_objective_terms`` on why order matters.
        """
        if not terms:
            ctx.solver.set_objective_minimize(0)
            return
        obj: Any = terms[0]
        for term in terms[1:]:
            obj = obj + term
        ctx.solver.set_objective_minimize(obj)

    def _minimize_cost(
        self,
        ctx: ModelContext,
        devices: list[Any],
        grid: Grid,
        bundle: SolveBundle,
        config: MimirheimConfig,
    ) -> None:
        """Set a cost-minimisation objective weighted by per-step confidence.

        For each time step t, the contribution to the objective is:

            confidence[t] × (import_price[t] × import[t] − export_price[t] × export[t])
            + Σ_d device.objective_terms(t)

        A terminal SoC value term is also added for each storage device (battery
        and V2H-capable EV when plugged in) — see ``_terminal_soc_terms`` for
        the coefficient and why the division by ``dt`` is necessary.

        The confidence weighting means that steps with low-quality forecasts
        contribute less to the objective. A step with confidence=0 is treated
        as economically neutral: the solver is indifferent to the actions taken
        at that step.

        Device ``objective_terms`` (typically battery wear cost) are added
        unconditionally — they are not confidence-weighted because wear occurs
        regardless of forecast quality.

        When ``config.objectives.exchange_shaping_weight > 0``, an optional
        secondary term ``lambda * sum_t(import_t + export_t)`` is appended.
        This is several orders of magnitude smaller than typical economic terms
        and does not distort dispatch decisions; it breaks indifference among
        solutions with equal primary cost by favouring lower total exchange.

        Args:
            ctx: Model context.
            devices: Non-grid devices contributing wear-cost terms and
                optionally a terminal SoC variable.
            grid: Grid device providing import and export variables.
            bundle: Runtime inputs with prices and confidence per step.
            config: Static configuration containing objective weights.
        """
        self._set_objective(
            ctx, self._cost_objective_terms(ctx, devices, grid, bundle, config)
        )

    def _minimize_consumption(
        self,
        ctx: ModelContext,
        devices: list[Any],
        grid: Grid,
        bundle: SolveBundle,
        config: MimirheimConfig,
    ) -> float:
        """Set a two-phase lexicographic consumption-minimisation objective.

        The strategy uses two sequential solver calls to achieve a
        lexicographic optimum: first minimise total grid import volume, then
        minimise the full net cost subject to the minimum import found.

        Phase 1 (executed inside this method):
            Minimise ``Σ_t import[t]``. ``ctx.solver.solve()`` is called here
            to find the optimal total import I*. Two outcomes fall short of
            that and both set ``strategy_degraded``: a time-limited incumbent
            ("feasible") is locked as I* although it is an achievable volume
            rather than the proven minimum; no incumbent at all ("infeasible")
            leaves nothing to lock, so phase 2 is set up as the plain cost
            objective and this method returns early.

        Phase 2 (set up here; executed by the caller):
            Add a hard constraint ``Σ_t import[t] <= I* + ε`` to preserve
            the minimum import, then minimise the confidence-weighted net cost
            (import cost minus export revenue) plus device wear cost plus
            terminal SoC value — exactly the same objective as
            ``_minimize_cost``. This shifts imports to the cheapest time slots
            among all schedules that achieve the phase-1 minimum import volume,
            and simultaneously maximises export at the highest-priced steps.

        The terminal SoC value is added only in phase 2. Phase 1 minimises
        import volume and must not be influenced by terminal SoC (which would
        incorrectly incentivise over-charging during phase 1).

        The epsilon value (1e-4 kWh) prevents numeric infeasibility caused by
        floating-point differences between the two solve calls.

        The exchange-shaping secondary term (when enabled) is added only in
        phase 2, where it acts as a tiebreaker among solutions with equal
        phase-1 import volume.

        **Time budget.** ``config.solver.time_limit_seconds`` covers the whole
        solve cycle, not one solver invocation, so the two phases split it
        evenly rather than each taking the full amount. An even split is used
        instead of measuring phase-1 elapsed time because it keeps the model
        build free of clock reads and gives a predictable worst case. Phase 1
        is a pure volume minimisation with no price terms and normally
        finishes well inside its half.

        Args:
            ctx: Model context.
            devices: Non-grid devices. Storage devices contribute wear cost and
                terminal SoC value terms in phase 2.
            grid: Grid device providing import and export variables.
            bundle: Runtime inputs with per-step prices and confidence values.
            config: Static configuration containing objective weights and the
                solver time budget.

        Returns:
            Seconds of solver budget left for the caller's phase-2 solve.
        """
        phase_budget = config.solver.time_limit_seconds / 2.0

        # Phase 1: minimise total import.
        import_vars = [grid.import_[t] for t in ctx.T]
        import_sum: Any = import_vars[0]
        for v in import_vars[1:]:
            import_sum = import_sum + v

        ctx.solver.set_objective_minimize(import_sum)
        phase_1_status = ctx.solver.solve(time_limit_seconds=phase_budget)

        if phase_1_status == "feasible":
            # An incumbent, but not a proven minimum. Locking it still bounds
            # phase 2 by a volume the model can actually achieve, so the
            # schedule is sound — but it is not the minimum this strategy
            # promises, and reporting it as one would overstate what was
            # solved.
            logger.warning(
                "minimize_consumption phase 1 hit its time limit without "
                "proving the minimum import volume; locking the incumbent."
            )
            self.strategy_degraded = True

        if phase_1_status == "infeasible":
            # No incumbent, so there are no variable values to read. var_value
            # is float(var.x) and var.x is None here, which would raise a
            # TypeError out of the solve loop rather than returning an
            # infeasible SolveResult — the schedule topic would keep its
            # previous contents and the policy state would never be published.
            #
            # Skipping the lock leaves phase 2 to solve the unconstrained cost
            # objective. Either it finds nothing and the caller reports
            # infeasible honestly, or it succeeds, in which case a
            # cost-optimal schedule is a better answer than a crash. The
            # strategy's volume guarantee is lost for this cycle; that is
            # already lost the moment phase 1 cannot answer.
            logger.warning(
                "minimize_consumption phase 1 produced no solution; skipping "
                "the import-volume lock and solving for cost alone this cycle."
            )
            # The schedule that comes out is cost-optimal, not volume-optimal,
            # and the two genuinely differ: with a cheap early tariff the cost
            # objective will import to bank value in the battery where the
            # volume objective would import nothing. Publishing it under the
            # requested strategy name without saying so would misrepresent it.
            self.strategy_degraded = True
            self._set_objective(
                ctx, self._cost_objective_terms(ctx, devices, grid, bundle, config)
            )
            return config.solver.time_limit_seconds - phase_budget

        # Record the optimal total import and lock it in with a small slack.
        # The slack prevents numeric infeasibility if the phase-1 optimal value
        # is fractionally below the sum of individual var_value readings.
        i_star = sum(ctx.solver.var_value(v) for v in import_vars)

        # Build the same sum a second time rather than reusing import_sum.
        # mip.minimize() mutates the expression it is handed, stamping a sense
        # of "MIN" onto it, so the object above is no longer a neutral linear
        # expression. Feeding it into a constraint happens to work today, but
        # relying on that couples this code to an implementation detail of the
        # backend that SolverBackend exists to hide.
        import_sum_for_constr: Any = import_vars[0]
        for v in import_vars[1:]:
            import_sum_for_constr = import_sum_for_constr + v
        ctx.solver.add_constraint(import_sum_for_constr <= i_star + 1e-4)

        # Phase 2: minimise confidence-weighted net cost subject to the locked-in
        # import volume from Phase 1. This shifts imports to the cheapest time slots
        # and simultaneously maximises export revenue. Device wear cost and terminal
        # SoC value are included exactly as in _minimize_cost.
        self._set_objective(
            ctx, self._cost_objective_terms(ctx, devices, grid, bundle, config)
        )

        return config.solver.time_limit_seconds - phase_budget

    def _balanced(
        self,
        ctx: ModelContext,
        devices: list[Any],
        grid: Grid,
        bundle: SolveBundle,
        config: MimirheimConfig,
    ) -> None:
        """Set a weighted-sum objective balancing cost and self-sufficiency.

        The objective is:

            cost_weight_norm × cost_obj
            + self_sufficiency_weight_norm × self_suf_obj
            + Σ_t Σ_d device.objective_terms(t)

        where the normalised weights sum to 1, and:

            cost_obj      = Σ_t confidence[t] × (price[t] × import[t]
                                                  − export_price[t] × export[t])
            self_suf_obj  = Σ_t import[t]   (minimising import = more self-sufficient)

        Normalising weights allows intuitive per-dimension tuning: doubling
        ``cost_weight`` shifts the blend toward cost minimisation without
        changing the scale of the objective.

        If ``config.objectives.balanced_weights`` is None, both weights default
        to 1.0, giving an equal blend.

        Args:
            ctx: Model context.
            devices: Non-grid devices contributing wear-cost terms.
            grid: Grid device providing import and export variables.
            bundle: Runtime inputs with prices and confidence.
            config: Static configuration with optional balanced_weights.
        """
        weights = config.objectives.balanced_weights or BalancedWeightsConfig()
        total_weight = weights.cost_weight + weights.self_sufficiency_weight
        cw = weights.cost_weight / total_weight
        sw = weights.self_sufficiency_weight / total_weight

        # Cost component: confidence-weighted net energy cost.
        cost_terms: list[Any] = []
        for t in ctx.T:
            economic = weight_by_confidence(
                bundle.horizon_prices[t] * grid.import_[t]
                - bundle.horizon_export_prices[t] * grid.export_[t],
                bundle.horizon_confidence[t],
            )
            # weight_by_confidence returns Python int 0 when confidence==0;
            # only scale and append solver expressions.
            if not isinstance(economic, (int, float)):
                cost_terms.append(cw * economic)

        # Self-sufficiency component: minimise total grid import.
        for t in ctx.T:
            cost_terms.append(sw * grid.import_[t])

        # Device wear cost terms (unconditional, not confidence-weighted).
        for t in ctx.T:
            for d in devices:
                cost_terms.extend(self._wear_terms(d, t))

        # Terminal SoC value: preserves stored energy across the horizon
        # boundary. Same semantics as in _minimize_cost.
        cost_terms.extend(self._terminal_soc_terms(ctx, devices, bundle))
        cost_terms.extend(self._exchange_shaping_terms(ctx, grid, config))

        self._set_objective(ctx, cost_terms)

    def _exchange_shaping_terms(
        self,
        ctx: ModelContext,
        grid: Grid,
        config: MimirheimConfig,
    ) -> list[Any]:
        """Build the optional exchange-shaping secondary objective terms.

        Returns ``[w * import[0], w * export[0], w * import[1], ...]`` across
        all steps (empty when ``exchange_shaping_weight == 0.0``). See
        IMPLEMENTATION_DETAILS.md §10 for why the weight must stay orders of
        magnitude below real energy prices.

        Args:
            ctx: Model context providing the time horizon.
            grid: Grid device providing import and export variables.
            config: Static configuration containing the weight value.

        Returns:
            A list of solver expressions to append to the objective, or an
            empty list when the weight is zero.
        """
        w = config.objectives.exchange_shaping_weight
        if w == 0.0:
            return []
        terms: list[Any] = []
        for t in ctx.T:
            terms.append(w * grid.import_[t])
            terms.append(w * grid.export_[t])
        return terms

    def _terminal_soc_terms(
        self,
        ctx: ModelContext,
        devices: list[Any],
        bundle: SolveBundle,
    ) -> list[Any]:
        """Build terminal SoC value terms for every storage device.

        For each device that exposes a ``terminal_soc_var(ctx)`` method and
        returns a non-None solver variable, this method produces a term:

            −(avg_import_price / dt) × soc[T-1]

        The negative sign causes the minimiser to prefer higher end-of-horizon
        SoC. The average is taken over the horizon steps actually used by this
        solve cycle, which is variable in length.

        **Why divide by dt?**

        The economic terms in the objective are price times power: EUR/kWh
        times kW, evaluated once per step. ``soc`` is an energy in kWh, so a
        bare EUR/kWh coefficient on it is a factor of ``dt`` smaller than the
        per-step terms it competes against. At the 15-minute step that is a
        factor of four, enough that the terminal value fails to outweigh export
        revenue whenever the export price is above roughly a quarter of the
        average import price, and the solver drains storage at the end of the
        horizon anyway.

        **Why average import price?**

        The terminal SoC value represents the expected cost of re-acquiring
        1 kWh after the horizon ends. In the absence of future price
        information, the best estimate is the average price during the current
        horizon. This prevents the solver from draining storage to export at
        a price below what it would cost to refill — the classic end-of-horizon
        artefact in finite-horizon Model Predictive Control.

        Args:
            ctx: The current solve context. Determines which step is T-1.
            devices: All non-grid devices. Only devices with a
                ``terminal_soc_var`` method contribute terms.
            bundle: Runtime inputs. Import prices are read from
                ``bundle.horizon_prices`` to compute the average.

        Returns:
            A list of linear expressions, one per storage device with a
            non-trivial terminal SoC variable. Empty when no storage devices
            are present or all return ``None``.
        """
        avg_import_price = (
            sum(bundle.horizon_prices[t] for t in ctx.T) / len(ctx.T)
        )
        terminal_value_coeff = avg_import_price / ctx.dt

        terms: list[Any] = []
        for d in devices:
            get_soc = getattr(d, "terminal_soc_var", None)
            if get_soc is None:
                continue
            soc_var = get_soc(ctx)
            if soc_var is None or isinstance(soc_var, (int, float)):
                continue
            # Subtract from the minimisation objective: makes the solver prefer
            # higher terminal SoC. The coefficient is in EUR/(kWh·step).
            terms.append(-terminal_value_coeff * soc_var)
        return terms
