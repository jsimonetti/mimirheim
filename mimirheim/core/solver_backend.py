"""Solver backend abstraction and CBC implementation.

This module defines the ``SolverBackend`` Protocol — the only interface through
which the rest of mimirheim interacts with an MILP solver — and the default
``CBCSolverBackend`` that wraps CBC (COIN-OR Branch and Cut) via the
``python-mip`` Python bindings.

Device classes, the objective builder, and the model builder all call methods on
``SolverBackend``. None of them import ``mip`` directly. This keeps the
solver dependency contained here and makes it possible to substitute a
different solver by implementing this Protocol without touching any model-building
code.

This module does not import from any other ``mimirheim`` module.
"""

from typing import Any, Protocol, runtime_checkable

import mip
from mip import OptimizationStatus as MipStatus
from mip.cbc import cbclib


@runtime_checkable
class SolverBackend(Protocol):
    """Interface for the MILP solver used by mimirheim.

    All model-building code targets this Protocol. The concrete implementation
    (``CBCSolverBackend``) wraps HiGHS. An alternative backend can be
    injected via ``ModelContext`` without modifying any device or objective code.

    Variable and constraint objects returned by ``add_var`` and ``add_constraint``
    are opaque handles; callers pass them back into ``add_constraint``,
    ``set_objective_minimize``, ``set_objective_maximize``, and ``var_value``.
    The type is ``Any`` because the concrete type is solver-specific and the
    Protocol must remain solver-agnostic.
    """

    def add_var(self, lb: float = 0.0, ub: float = 1e30, integer: bool = False) -> Any:
        """Declare a new decision variable and return an opaque variable handle.

        Args:
            lb: Lower bound. The solver will not return solutions where this
                variable is below this value.
            ub: Upper bound. Use ``1e30`` (effectively unbounded) when only a
                lower bound is needed.
            integer: If True, constrain the variable to integer values. This
                turns the LP into a MIP, which is more expensive to solve but
                is required for binary mode guards and activation variables.

        Returns:
            An opaque variable handle suitable for passing to ``add_constraint``,
            ``set_objective_minimize``/``maximize``, and ``var_value``.
        """
        ...

    def add_constraint(self, expr: Any) -> None:
        """Add a linear constraint to the model.

        Args:
            expr: A constraint expression produced by combining variable handles
                with comparison operators (``<=``, ``>=``, ``==``). The exact
                type depends on the backend.
        """
        ...

    def set_objective_minimize(self, expr: Any) -> None:
        """Set the solver objective to minimise the given linear expression.

        Only one objective may be active at a time. Calling this method again
        replaces any previously set objective.

        Args:
            expr: A linear expression of variable handles and constants.
        """
        ...

    def set_objective_maximize(self, expr: Any) -> None:
        """Set the solver objective to maximise the given linear expression.

        Args:
            expr: A linear expression of variable handles and constants.
        """
        ...

    def solve(self, time_limit_seconds: float = 59.0) -> str:
        """Run the solver and return the solution status as a string.

        A time limit prevents the solver from blocking the re-solve loop
        indefinitely. If the limit is hit and an incumbent solution exists,
        the status is ``"feasible"`` (not ``"optimal"``). The incumbent is
        still available via ``var_value``.

        Args:
            time_limit_seconds: Maximum wall-clock time to spend solving.
                Defaults to 59 seconds to fit inside a 60-second solve cycle.

        Returns:
            One of:
            - ``"optimal"``: the solver proved the solution is globally optimal.
            - ``"feasible"``: a solution was found but optimality is not proven
              (time limit reached with an incumbent).
            - ``"infeasible"``: no solution satisfies all constraints. The
              schedule from the previous cycle should be retained unchanged.
        """
        ...

    def var_value(self, var: Any) -> float:
        """Return the value of a variable in the most recent solution.

        Calling this before ``solve()`` or after an infeasible solve returns
        an undefined value. Callers are responsible for checking the status
        returned by ``solve()`` before reading variable values.

        Args:
            var: An opaque variable handle previously returned by ``add_var``.

        Returns:
            The numeric value assigned to this variable by the solver.
        """
        ...

    def add_sos2(self, variables: list[Any], weights: list[float]) -> None:
        """Add a SOS type-2 constraint over the given variables.

        A SOS2 constraint specifies that at most two adjacent variables (in the
        order defined by their weights) may be nonzero simultaneously. This is
        used to enforce piecewise-linear interpolation: only one linear segment
        of the efficiency curve can be active at each time step, and the solver
        interpolates between the two breakpoints bounding that segment.

        Because the installed ``python-mip`` version does not expose a native SOS2
        API, the constraint is implemented via a set of binary auxiliary variables
        — one per segment — with Big-M upper bounds on the weights:

        .. code-block::

            sum(b_i) == 1                           (exactly one segment active)
            variables[0]   <= b_0
            variables[i]   <= b_{i-1} + b_i         (interior weights)
            variables[-1]  <= b_{N-2}

        When b_i = 1, only variables[i] and variables[i+1] can be nonzero.
        All other variables are forced to zero by the upper bound on the right.

        Args:
            variables: Solver variable objects to include in the SOS2 constraint.
                Must have at least two elements.
            weights: Numeric ordering weights, one per variable. Determines
                adjacency: ``variables[i]`` and ``variables[i+1]`` are adjacent.
                In practice these are the power breakpoint values.
        """
        ...

    def objective_value(self) -> float:
        """Return the objective function value from the most recent solve.

        Returns 0.0 before any solve or after an infeasible solve. The sign
        convention follows the solver: for minimisation objectives, a lower
        value means a better solution.

        Returns:
            The objective value from the most recent ``solve()`` call.
        """
        ...

    def model_stats(self) -> tuple[int, int, int, int]:
        """Return a summary of the built model's size.

        Intended for logging and diagnostics. Must be called after all
        variables and constraints have been added — the values are meaningless
        on an empty model.

        Returns:
            A four-tuple ``(num_cols, num_rows, num_int, num_nz)``:
            - ``num_cols``: total number of decision variables (columns).
            - ``num_rows``: total number of constraints (rows).
            - ``num_int``: number of integer/binary decision variables.
            - ``num_nz``: number of non-zero entries in the constraint matrix.
        """
        ...


class CBCSolverBackend:
    """CBC-backed implementation of ``SolverBackend`` via ``python-mip``.

    Wraps ``mip.Model`` (CBC solver) and translates the ``SolverBackend``
    interface into python-mip API calls. Variable handles returned by
    ``add_var`` are ``mip.Var`` objects; constraint and objective expressions
    are ``mip.LinExpr`` or ``mip.LinConstr`` objects produced via the overloaded
    comparison and arithmetic operators on those variables.

    CBC (COIN-OR Branch and Cut) is the default solver for mimirheim. It is
    bundled inside the ``mip`` package (via ``cbcbox``) as a compiled shared
    library; no external CBC installation is required.
    See https://www.python-mip.com/.

    CBC is chosen over HiGHS because it is approximately 100x faster on the
    temperature-coupled binary chains that thermal device constraints produce.
    The aggressive Gomory cuts in CBC's default cut strategy tighten the LP
    relaxation at the root node, allowing it to prove optimality in very few
    branch-and-bound nodes where HiGHS requires hundreds or thousands.
    """

    def __init__(self, threads: int = -1) -> None:
        # Create a fresh CBC model. verbose=0 suppresses all CBC console
        # output — mimirheim owns its own logging.
        self._m = mip.Model(solver_name=mip.CBC)
        self._m.verbose = 0
        # Accept a solution whose objective value is within 0.5 % of the true
        # optimum. For a residential energy schedule this is imperceptible in
        # practice: on a €50/day schedule the error is at most 25 cents.
        #
        # The threshold must be at least as large as the model's natural
        # integrality gap — the gap that cannot be closed by branch-and-bound
        # within the 59-second wall-clock limit.  The prosumer_ev_48h benchmark
        # (192 steps, 768 binary variables) has an integrality gap of ~0.18 %:
        # CBC finds a near-optimal feasible solution quickly but can only prove
        # the bound once the gap setting permits early termination.  0.5 %
        # gives comfortable margin above the observed gap across all benchmark
        # scenarios, ensuring CBC exits as soon as it has a good solution rather
        # than spending the remaining budget on negligible improvements.
        self._m.max_mip_gap = 5e-3
        # Direct solver effort towards finding a first feasible integer solution
        # quickly.  On the 672-step worst_case_7d scenario the default heuristic
        # budget is too small to find any feasible solution within the 59-second
        # wall-clock limit.  FEASIBILITY emphasis tells CBC to run 50 feasibility
        # pump passes and enable proximity search before branching, which brings
        # the time-to-first-feasible solution from >59 s to a few seconds on
        # that scenario.  It has no effect on the gap acceptance threshold.
        self._m.emphasis = mip.SearchEmphasis.FEASIBILITY
        # Number of threads CBC may use during branch-and-bound. python-mip
        # interprets -1 as "use all available CPU cores". On a dedicated home
        # server that is otherwise idle between solve cycles, -1 is the
        # recommended setting and is the default.
        self._m.threads = threads
        # Enable the LP-rounding heuristic. After solving the LP relaxation at
        # each node, CBC rounds fractional binary variables to the nearest
        # integer and checks whether the resulting assignment is feasible. The
        # rounding pass has near-zero cost and provides a fast fallback when the
        # feasibility pump does not converge immediately. Without it, some
        # models with many near-integer LP solutions spend extra time in the
        # branch-and-bound tree before a first feasible incumbent is found.
        #
        # python-mip does not expose a high-level property for this parameter,
        # so it is set through the low-level cbclib C binding. INT_PARAM_ROUND_INT_VARS
        # maps to CBC's roundingHeuristic flag; value 1 enables it.
        # self._m.solver._model is the opaque C pointer to the CBC model.
        cbclib.Cbc_setIntParam(
            self._m.solver._model, cbclib.INT_PARAM_ROUND_INT_VARS, 1
        )

    def add_var(self, lb: float = 0.0, ub: float = 1e30, integer: bool = False) -> Any:
        """Declare a new continuous or integer variable.

        Args:
            lb: Lower bound on the variable's value.
            ub: Upper bound. Defaults to 1e30 (effectively unbounded).
            integer: If True, declare an integer variable using
                ``mip.INTEGER``.

        Returns:
            A ``mip.Var`` handle.
        """
        var_type = mip.INTEGER if integer else mip.CONTINUOUS
        return self._m.add_var(lb=lb, ub=ub, var_type=var_type)

    def add_constraint(self, expr: Any) -> None:
        """Add a linear inequality or equality constraint.

        Args:
            expr: A ``mip.LinConstr`` constraint such as ``x + y <= 10`` or
                ``x >= 1``, produced via ``mip.Var`` arithmetic operators.
        """
        self._m += expr

    def set_objective_minimize(self, expr: Any) -> None:
        """Set the objective to minimise the given expression.

        When ``expr`` is a plain scalar constant, no objective is set and CBC
        will find any feasible solution. python-mip does not accept bare
        integers or floats as objectives; a constant objective is equivalent
        to no objective.

        Args:
            expr: A ``mip.Var`` or ``mip.LinExpr``, or a plain numeric
                constant (treated as a no-op).
        """
        if isinstance(expr, (int, float)):
            return
        self._m.objective = mip.minimize(expr)

    def set_objective_maximize(self, expr: Any) -> None:
        """Set the objective to maximise the given expression.

        When ``expr`` is a plain scalar constant, no objective is set and CBC
        will find any feasible solution.

        Args:
            expr: A ``mip.Var`` or ``mip.LinExpr``, or a plain numeric
                constant (treated as a no-op).
        """
        if isinstance(expr, (int, float)):
            return
        self._m.objective = mip.maximize(expr)

    def solve(self, time_limit_seconds: float = 59.0) -> str:
        """Run CBC and return the solve status.

        Calls ``model.optimize(max_seconds=time_limit_seconds)``, then maps
        the ``OptimizationStatus`` enum to the three strings the rest of mimirheim
        uses: ``"optimal"``, ``"feasible"``, or ``"infeasible"``.

        Args:
            time_limit_seconds: Wall-clock time budget for this solve.

        Returns:
            ``"optimal"``, ``"feasible"``, or ``"infeasible"``.
        """
        status = self._m.optimize(max_seconds=time_limit_seconds)
        if status == MipStatus.OPTIMAL:
            return "optimal"
        if status == MipStatus.FEASIBLE:
            return "feasible"
        if status in (MipStatus.INFEASIBLE, MipStatus.INT_INFEASIBLE):
            return "infeasible"
        # NO_SOLUTION_FOUND, UNBOUNDED, LOADED, ERROR, or any other status.
        # Treat as infeasible: no schedule can be extracted safely.
        return "infeasible"

    def var_value(self, var: Any) -> float:
        """Return the solver-assigned value of a variable.

        Args:
            var: A ``mip.Var`` handle returned by ``add_var``.

        Returns:
            The numeric value of the variable in the current solution.
        """
        return float(var.x)

    def add_sos2(self, variables: list[Any], weights: list[float]) -> None:
        """Enforce SOS2 adjacency on ``variables`` via binary auxiliary variables.

        python-mip does not expose a native SOS2 constraint API that maps
        cleanly to the ``SolverBackend`` Protocol, so the constraint is modelled
        with N-1 binary variables (one per segment in the N-variable set):

        .. code-block::

            sum(b_i) == 1
            variables[0]   <= b[0]
            variables[i]   <= b[i-1] + b[i]      (interior variables)
            variables[-1]  <= b[-1]

        When b[i] = 1, only variables[i] and variables[i+1] can be nonzero;
        all other variables are forced to zero by their upper-bound constraints.

        This emulation is solver-agnostic: it relies only on ``self.add_var``
        and ``self.add_constraint``, which are defined in terms of this
        Protocol. The same emulation is used in ``CBCSolverBackend``.

        Args:
            variables: List of solver variable handles to constrain. Must
                contain at least two elements.
            weights: Numeric ordering values (one per variable). Not used by
                this implementation (ordering is implied by index), but must
                be provided for Protocol conformance and documentation.
        """
        n = len(variables)
        if n < 2:
            raise ValueError(
                f"add_sos2 requires at least 2 variables, got {n}."
            )
        # One binary variable per segment (n-1 segments for n breakpoints).
        # b[i] = 1 means the operating point lies on segment [i, i+1].
        n_seg = n - 1
        binaries = [self.add_var(lb=0.0, ub=1.0, integer=True) for _ in range(n_seg)]

        # Exactly one segment must be active per time step.
        total_b: Any = binaries[0]
        for b in binaries[1:]:
            total_b = total_b + b
        self.add_constraint(total_b == 1)

        # Upper-bound each weight variable by the sum of binaries for its
        # adjacent segments.
        self.add_constraint(variables[0] <= binaries[0])
        for i in range(1, n - 1):
            self.add_constraint(variables[i] <= binaries[i - 1] + binaries[i])
        self.add_constraint(variables[-1] <= binaries[-1])

    def objective_value(self) -> float:
        """Return the objective function value from the most recent solve.

        Returns:
            The objective value, or 0.0 if no solve has completed.
        """
        val = self._m.objective_value
        return float(val) if val is not None else 0.0

    def model_stats(self) -> tuple[int, int, int, int]:
        """Return a summary of the built model's size.

        Returns:
            ``(num_cols, num_rows, num_int, num_nz)``.
        """
        return (
            self._m.num_cols,
            self._m.num_rows,
            self._m.num_int,
            self._m.num_nz,
        )
