"""Static load device — treats inflexible household consumption as a fixed parameter.

A static load represents non-controllable power consumption: always-on appliances,
lighting, fridge, cooking, etc. The solver cannot shift or curtail these loads.
They are provided as a per-step forecast and enter the power balance as a fixed
negative term (consuming power).

``StaticLoad`` is structurally identical to ``PvDevice`` except for the sign of
``net_power``. Both have no decision variables; both store a forecast list during
``add_constraints`` and read from it in ``net_power``.

``StaticLoadInputs`` holds the per-step forecast. It is not given a staleness
check (unlike ``BatteryInputs``) because the static load forecast is typically
derived from a rolling average or external forecast service and is not expected
to have a real-time hardware reading with a tight validity window.

This module does not import from ``mimirheim.io`` or ``python-mip``.
"""

from pydantic import BaseModel, ConfigDict, Field

from mimirheim.config.schema import StaticLoadConfig
from mimirheim.core.context import ModelContext


class StaticLoadInputs(BaseModel):
    """Runtime static load forecast delivered to the device each solve cycle.

    Attributes:
        forecast_kw: Per-step power consumption forecast in kW. Must contain at
            least one value. Values should be non-negative (a negative forecast
            would imply generation, which should be modelled as PV instead).
    """

    model_config = ConfigDict(extra="forbid")

    forecast_kw: list[float] = Field(
        min_length=1, description="Per-step static load forecast in kW."
    )


class StaticLoad:
    """Models inflexible household load as a fixed power draw parameter.

    The solver cannot reduce or shift this load. It enters the system power
    balance as a fixed negative contribution (consuming power) at each step.
    Other devices (battery, EV, grid import) must together supply this demand.

    Attributes:
        name: Device name matching the key in ``config.static_loads`` (if any)
            or a well-known name such as ``"base_load"``.
        config: Static configuration for this load.
    """

    def __init__(self, name: str, config: StaticLoadConfig) -> None:
        """Initialise the static load device.

        Args:
            name: Device name.
            config: Validated static configuration.
        """
        self.name = name
        self.config = config
        self._forecast: list[float] = []

    def add_variables(self, ctx: ModelContext) -> None:
        """No-op — static load has no decision variables.

        Args:
            ctx: The current solve context (unused).
        """

    def add_constraints(self, ctx: ModelContext, inputs: StaticLoadInputs) -> None:
        """Store the forecast for use by ``net_power``.

        No solver constraints are added. The forecast is stored as a plain
        Python list.

        Args:
            ctx: The current solve context (unused).
            inputs: The per-step static load forecast for this solve cycle.
        """
        self._forecast = inputs.forecast_kw

    def net_power(self, t: int) -> float:
        """Return the static load at step ``t`` as a negative constant.

        Negative because the load consumes power. The sign convention matches
        the system power balance: a positive value contributes to available
        power; a negative value represents demand that must be met.

        Args:
            t: Time step index.

        Returns:
            ``-forecast_kw[t]`` in kW.
        """
        return -self._forecast[t]

    def objective_terms(self, t: int) -> int:
        """Return zero — static load has no cost terms in the objective.

        Args:
            t: Time step index (unused).

        Returns:
            Zero, always.
        """
        return 0
