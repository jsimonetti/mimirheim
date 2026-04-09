"""ObjectiveBuilder — assembles the MILP objective for each strategy.

This module translates the ``strategy`` field of ``SolveBundle`` into a concrete
minimisation objective on the solver, and adds any hard-cap constraints from
``ConstraintsConfig`` before setting the objective.

Three strategies are supported:

- ``minimize_cost``: minimise net energy cost against time-varying import and
  export prices, with each step weighted by a per-step confidence value.
- ``minimize_consumption``: minimise total grid import lexicographically, then
  maximise export revenue subject to the optimal import bound. This is the only
  strategy that calls ``ctx.solver.solve()`` internally (phase-1 solve). The
  caller must call ``ctx.solver.solve()`` once more to complete phase 2.
- ``balanced``: weighted sum of cost and self-sufficiency objectives, blended
  according to ``config.objectives.balanced_weights``.

This module imports from ``mimirheim.core`` and ``mimirheim.devices`` but never from
``mimirheim.io``. It does not read configuration files, connect to MQTT, or perform
any I/O.
"""

from typing import Any

from mimirheim.config.schema import BalancedWeightsConfig, MimirheimConfig
from mimirheim.core.bundle import SolveBundle
from mimirheim.core.confidence import weight_by_confidence
from mimirheim.core.context import ModelContext
from mimirheim.devices.grid import Grid


class ObjectiveBuilder:
    """Assembles the MILP objective and hard-cap constraints for a single solve.

    ``ObjectiveBuilder`` is a stateless helper class — it has no instance
    variables and every call to ``build`` is independent. It is a class rather
    than a plain function so that it can be replaced by a test double or
    subclassed to inject alternative objective logic.
    """

    def build(
        self,
        ctx: ModelContext,
        devices: list[Any],
        grid: Grid,
        bundle: SolveBundle,
        config: MimirheimConfig,
    ) -> None:
        """Set the objective on ctx.solver according to bundle.strategy.

        This is the sole entry point for objective assembly. It:

        1. Adds hard-cap constraints from ``config.constraints`` (import and
           export power limits) at every time step.
        2. Dispatches to the appropriate strategy implementation.

        For ``minimize_consumption``, this method calls ``ctx.solver.solve()``
        once internally (phase-1 solve). The caller must call
        ``ctx.solver.solve()`` once more after ``build`` returns to complete the
        phase-2 solve. This is the only strategy with two solver invocations per
        ``build_and_solve`` cycle.

        Args:
            ctx: The model context holding the solver and time horizon.
            devices: All non-grid devices whose ``objective_terms(t)`` values
                contribute wear-cost penalties to the objective.
            grid: The grid device. Its import and export variables are the
                primary economic variables in the objective.
            bundle: Runtime inputs including the strategy name, time-varying
                prices, and per-step confidence values.
            config: Static configuration including strategy weights and
                optional hard-cap constraints.

        Raises:
            ValueError: If ``bundle.strategy`` is not one of the three
                supported strings.
        """
        self._add_hard_cap_constraints(ctx, grid, config)

        if bundle.strategy == "minimize_cost":
            self._minimize_cost(ctx, devices, grid, bundle, config)
        elif bundle.strategy == "minimize_consumption":
            self._minimize_consumption(ctx, devices, grid, bundle, config)
        elif bundle.strategy == "balanced":
            self._balanced(ctx, devices, grid, bundle, config)
        else:
            raise ValueError(f"Unknown strategy: {bundle.strategy!r}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _add_hard_cap_constraints(
        self, ctx: ModelContext, grid: Grid, config: MimirheimConfig
    ) -> None:
        """Add per-step hard caps on import and export power, if configured.

        These caps are hard inequality constraints, not objective penalties.
        The solver will never return a solution that violates them. They are
        independent of strategy and are applied before any objective is set.

        Args:
            ctx: Model context with solver and time horizon.
            grid: The grid device whose variables are capped.
            config: Static configuration containing optional maximum limits.
        """
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
        and V2H-capable EV when plugged in):

            −avg_import_price × soc[T-1]

        The negative sign causes the minimiser to prefer higher terminal SoC.
        The coefficient is the average import price over the horizon — the
        expected cost of re-acquiring 1 kWh after the horizon ends. Without
        this term the solver treats end-of-horizon stored energy as worthless
        and drains storage whenever there is any positive export price, even
        when that price is below the cost of refilling.

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
        obj_terms: list[Any] = []
        for t in ctx.T:
            economic = weight_by_confidence(
                bundle.horizon_prices[t] * grid.import_[t]
                - bundle.horizon_export_prices[t] * grid.export_[t],
                bundle.horizon_confidence[t],
            )
            # weight_by_confidence returns Python int 0 when confidence==0;
            # only append solver expressions (non-numeric values).
            if not isinstance(economic, (int, float)):
                obj_terms.append(economic)
            for d in devices:
                wear = d.objective_terms(t)
                # objective_terms may return a scalar 0 (no wear cost), a
                # single solver expression, or a list of solver expressions.
                if isinstance(wear, list):
                    obj_terms.extend(
                        w for w in wear if not isinstance(w, (int, float))
                    )
                elif not isinstance(wear, (int, float)):
                    obj_terms.append(wear)

        for term in self._terminal_soc_terms(ctx, devices, bundle):
            obj_terms.append(term)

        for term in self._exchange_shaping_terms(ctx, grid, config):
            obj_terms.append(term)

        if obj_terms:
            obj: Any = obj_terms[0]
            for term in obj_terms[1:]:
                obj = obj + term
            ctx.solver.set_objective_minimize(obj)
        else:
            ctx.solver.set_objective_minimize(0)

    def _minimize_consumption(
        self,
        ctx: ModelContext,
        devices: list[Any],
        grid: Grid,
        bundle: SolveBundle,
        config: MimirheimConfig,
    ) -> None:
        """Set a two-phase lexicographic consumption-minimisation objective.

        The strategy uses two sequential solver calls to achieve a
        lexicographic optimum: first minimise total grid import volume, then
        minimise the full net cost subject to the minimum import found.

        Phase 1 (executed inside this method):
            Minimise ``Σ_t import[t]``. ``ctx.solver.solve()`` is called here
            to find the optimal total import I*.

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

        Args:
            ctx: Model context.
            devices: Non-grid devices. Storage devices contribute wear cost and
                terminal SoC value terms in phase 2.
            grid: Grid device providing import and export variables.
            bundle: Runtime inputs with per-step prices and confidence values.
            config: Static configuration containing objective weights.
        """
        # Phase 1: minimise total import.
        import_vars = [grid.import_[t] for t in ctx.T]
        import_sum: Any = import_vars[0]
        for v in import_vars[1:]:
            import_sum = import_sum + v

        ctx.solver.set_objective_minimize(import_sum)
        ctx.solver.solve()

        # Record the optimal total import and lock it in with a small slack.
        # The slack prevents numeric infeasibility if the phase-1 optimal value
        # is fractionally below the sum of individual var_value readings.
        i_star = sum(ctx.solver.var_value(v) for v in import_vars)
        import_sum_for_constr: Any = import_vars[0]
        for v in import_vars[1:]:
            import_sum_for_constr = import_sum_for_constr + v
        ctx.solver.add_constraint(import_sum_for_constr <= i_star + 1e-4)

        # Phase 2: minimise confidence-weighted net cost subject to the locked-in
        # import volume from Phase 1. This shifts imports to the cheapest time slots
        # and simultaneously maximises export revenue. Device wear cost and terminal
        # SoC value are included exactly as in _minimize_cost.
        obj_terms: list[Any] = []
        for t in ctx.T:
            economic = weight_by_confidence(
                bundle.horizon_prices[t] * grid.import_[t]
                - bundle.horizon_export_prices[t] * grid.export_[t],
                bundle.horizon_confidence[t],
            )
            # weight_by_confidence returns Python int 0 when confidence == 0;
            # only append solver expressions.
            if not isinstance(economic, (int, float)):
                obj_terms.append(economic)
            for d in devices:
                wear = d.objective_terms(t)
                if isinstance(wear, list):
                    obj_terms.extend(
                        w for w in wear if not isinstance(w, (int, float))
                    )
                elif not isinstance(wear, (int, float)):
                    obj_terms.append(wear)

        for term in self._terminal_soc_terms(ctx, devices, bundle):
            obj_terms.append(term)

        for term in self._exchange_shaping_terms(ctx, grid, config):
            obj_terms.append(term)

        if obj_terms:
            obj: Any = obj_terms[0]
            for term in obj_terms[1:]:
                obj = obj + term
            ctx.solver.set_objective_minimize(obj)
        else:
            ctx.solver.set_objective_minimize(0)

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
                wear = d.objective_terms(t)
                # objective_terms may return a scalar 0 (no wear cost), a
                # single solver expression, or a list of solver expressions.
                if isinstance(wear, list):
                    cost_terms.extend(
                        w for w in wear if not isinstance(w, (int, float))
                    )
                elif not isinstance(wear, (int, float)):
                    cost_terms.append(wear)

        # Terminal SoC value: preserves stored energy across the horizon
        # boundary. Same semantics as in _minimize_cost.
        for term in self._terminal_soc_terms(ctx, devices, bundle):
            cost_terms.append(term)

        for term in self._exchange_shaping_terms(ctx, grid, config):
            cost_terms.append(term)

        if cost_terms:
            obj: Any = cost_terms[0]
            for term in cost_terms[1:]:
                obj = obj + term
            ctx.solver.set_objective_minimize(obj)
        else:
            ctx.solver.set_objective_minimize(0)

    def _exchange_shaping_terms(
        self,
        ctx: ModelContext,
        grid: Grid,
        config: MimirheimConfig,
    ) -> list[Any]:
        """Build the optional exchange-shaping secondary objective terms.

        When ``config.objectives.exchange_shaping_weight > 0``, returns a list
        of terms ``[w * import[0], w * export[0], w * import[1], ...]`` for
        all time steps t. When appended to the primary objective, these terms
        add ``w * sum_t(import_t + export_t)`` to the minimisation target.

        The weight must be orders of magnitude smaller than typical energy
        prices (e.g. 1e-4 EUR/kWh vs 0.20 EUR/kWh for retail electricity). At
        that scale the term cannot reverse a dispatch decision that is
        economically justified; it only breaks indifference among solutions with
        equal primary cost, favouring lower total exchange volume.

        Returns an empty list when ``exchange_shaping_weight == 0.0``, leaving
        existing objective behaviour completely unchanged.

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

            −avg_import_price × soc[T-1]

        The negative sign causes the minimiser to prefer higher end-of-horizon
        SoC. The coefficient is the average import price over the horizon
        steps actually used by this solve cycle (not the full 96-step bundle).

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
        # Compute the average import price across the steps in this horizon.
        # This is the expected cost of re-acquiring 1 kWh after the horizon.
        avg_import_price = (
            sum(bundle.horizon_prices[t] for t in ctx.T) / len(ctx.T)
        )

        # The economic objective uses price × power (EUR/kWh × kW), while soc
        # is in kWh. Dividing by dt converts EUR/kWh to EUR/(kWh·step), making
        # the terminal coefficient commensurate with the per-step power terms.
        # Without this factor the terminal value is 1/dt times too small and
        # fails to outweigh export revenue when export_price < avg_import_price.
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
