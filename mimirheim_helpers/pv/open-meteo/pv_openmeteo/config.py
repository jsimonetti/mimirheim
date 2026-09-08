"""Configuration schema for the mimirheim Open-Meteo PV forecast fetcher.

This module defines the Pydantic models that represent the pv_openmeteo YAML
configuration file. It is the single source of truth for field names, types,
constraints, and defaults.

The schema mirrors the parameter surface of the ``open_meteo_solar_forecast``
library, with one deliberate difference: the library takes parallel lists of
per-plane values, while this schema nests a list of planes under each array.
The conversion happens in fetcher.py.

What this module does not do:
- It does not import from mimirheim or any other helper tool.
- It does not perform any HTTP or MQTT operations.
- It does not call the open_meteo_solar_forecast library.
- It does not read the file. ``helper_common.config.load_helper_config``
  parses the YAML, applies the Supervisor MQTT environment overrides and
  validates the result against ``PvOpenMeteoConfig``; ``__main__`` calls it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from helper_common.config import HomeAssistantConfig, MqttConfig
import helper_common.topics as _topics


class OpenMeteoApiConfig(BaseModel):
    """Open-Meteo API parameters shared by every array.

    Attributes:
        api_key: API key for a commercial Open-Meteo subscription. When None
            (the default), the free public endpoint is used. The free tier
            allows 10000 calls per day and does not require registration.
        base_url: API base URL. Point this at ``https://customer-api.open-meteo.com``
            for a commercial subscription, or at a self-hosted Open-Meteo
            instance on the local network.
        weather_model: Name of the Open-Meteo weather model to request, for
            example ``icon_seamless`` or ``ecmwf_ifs025``. When None, Open-Meteo
            picks the best available model per location (its ``best_match``
            behaviour), which is the right choice unless a specific model is
            known to perform better for the site.
        forecast_days: Number of days of forecast to request, including today.
            The API supports up to 16. Requesting more than the mimirheim solve
            horizon needs only inflates the payload; three days covers a 48 hour
            horizon with room to spare.
        past_hours: How much already-elapsed forecast to keep in the published
            payload. A small amount of history guarantees that a step exists at
            or before the start of the solve horizon, so the resampler in
            mimirheim never has to extrapolate backwards. Set to 0 to publish
            only future steps.
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(
        default=None,
        description="Open-Meteo API key. Null = the free public endpoint.",
        json_schema_extra={"ui_label": "API key", "ui_group": "advanced"},
    )
    base_url: str = Field(
        default="https://api.open-meteo.com",
        description="API base URL. Change for a commercial or self-hosted endpoint.",
        json_schema_extra={"ui_label": "API base URL", "ui_group": "advanced"},
    )
    weather_model: str | None = Field(
        default=None,
        description="Open-Meteo weather model name. Null lets Open-Meteo choose per location.",
        json_schema_extra={"ui_label": "Weather model", "ui_group": "advanced"},
    )
    forecast_days: int = Field(
        default=3,
        ge=1,
        le=16,
        description="Days of forecast to request, including today.",
        json_schema_extra={"ui_label": "Forecast days", "ui_group": "advanced"},
    )
    past_hours: float = Field(
        default=1.0,
        ge=0.0,
        le=48.0,
        description="Hours of already-elapsed forecast to keep in the payload.",
        json_schema_extra={"ui_label": "Past hours retained", "ui_group": "advanced"},
    )


class SiteConfig(BaseModel):
    """Geographic location of the installation.

    Every plane inherits these coordinates unless it overrides them. Open-Meteo
    resolves the local timezone from the coordinates, and the library rejects a
    set of planes whose coordinates fall in different UTC offsets, so overrides
    are meant for planes a few streets away, not a few time zones away.

    Attributes:
        latitude: Site latitude in decimal degrees (positive = north).
        longitude: Site longitude in decimal degrees (positive = east).
    """

    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(
        ge=-90.0,
        le=90.0,
        description="Site latitude in decimal degrees.",
        json_schema_extra={"ui_label": "Latitude", "ui_group": "basic"},
    )
    longitude: float = Field(
        ge=-180.0,
        le=180.0,
        description="Site longitude in decimal degrees.",
        json_schema_extra={"ui_label": "Longitude", "ui_group": "basic"},
    )


class HorizonPoint(BaseModel):
    """One point on the skyline seen from the panels.

    The horizon describes how high obstacles rise above the horizontal at a
    given compass bearing. When the sun is below that elevation, the direct
    beam is blocked and only diffuse light reaches the panels.

    Note the different azimuth convention: horizon points use compass bearings
    (0 = north, 90 = east, 180 = south, 270 = west), because that is how a
    horizon survey is recorded and how the library interpolates it. Panel
    azimuth uses the Open-Meteo convention (0 = south) instead.

    Attributes:
        azimuth: Compass bearing in degrees, 0 to 360.
        elevation: Obstacle elevation above the horizontal in degrees, 0 to 90.
    """

    model_config = ConfigDict(extra="forbid")

    azimuth: float = Field(
        ge=0.0,
        le=360.0,
        description="Compass bearing in degrees: 0 = north, 90 = east, 180 = south.",
        json_schema_extra={"ui_label": "Bearing", "ui_group": "advanced"},
    )
    elevation: float = Field(
        ge=0.0,
        le=90.0,
        description="Obstacle elevation above the horizontal in degrees.",
        json_schema_extra={"ui_label": "Elevation", "ui_group": "advanced"},
    )


class PlaneConfig(BaseModel):
    """One coplanar group of modules within an array.

    A plane is the unit the physics is computed on: all modules in it share a
    tilt, an orientation, and a location, so a single tilted-irradiance series
    describes them all. An east-west roof is two planes; a single-orientation
    roof is one.

    Attributes:
        label: Optional name used in log messages. Purely cosmetic.
        latitude: Plane latitude. Defaults to the site latitude.
        longitude: Plane longitude. Defaults to the site longitude.
        declination: Panel tilt in degrees from horizontal. 0 = flat,
            90 = vertical (a facade or balcony installation).
        azimuth: Panel orientation in degrees from south. 0 = south,
            -90 = east, 90 = west, +-180 = north. To convert a compass bearing,
            subtract 180: an east-facing roof at bearing 90 becomes -90.
        peak_power_kwp: Combined nameplate DC power of the modules in this
            plane, in kWp.
        inverter_kwp: AC power limit of this plane's own inverter, in kW. Set
            this when each plane has its own inverter or its own set of
            micro-inverters. Leave unset when the planes share one inverter and
            set the array-level ``inverter_kwp`` instead.
        efficiency_factor: Everything between the module nameplate and the AC
            terminals that scales linearly: inverter efficiency, cable losses,
            soiling, and module degradation. 1.0 models a lossless system;
            0.9 is a common starting point for an ageing string installation.
        tracking: Axis tracking mode. ``none`` for a fixed mount,
            ``azimuth`` for a vertical-axis tracker that follows the sun's
            bearing, ``tilt`` for a horizontal-axis tracker that follows its
            elevation, ``dual`` for both. A tracked axis makes the corresponding
            fixed value irrelevant.
        damping_morning: Fraction of production to remove at sunrise, tapering
            linearly to none at solar noon. Use it when a fixed obstruction cuts
            morning yield that the model does not know about. 0.0 = no damping.
        damping_evening: The same for the afternoon, tapering from none at solar
            noon to the full fraction at sunset.
        max_snowcover_depth_cm: Snow depth at which the plane is treated as
            fully covered and producing nothing. Production is scaled down
            linearly from no snow to this depth. 0.0 disables the correction,
            which is the right setting for a steep roof that sheds snow.
        horizon: Skyline profile seen from this plane, as a list of at least two
            points in ascending compass bearing. When set, the direct beam is
            treated as blocked whenever the sun sits below the interpolated
            skyline. When null, no horizon shading is applied.
        partial_shading: When the horizon blocks the direct beam, still credit
            the diffuse component of the irradiance rather than dropping output
            to zero. This is the physically realistic behaviour for a distant
            skyline; leave it off to model a hard shadow. Has no effect unless
            ``horizon`` is set.
    """

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(
        default=None,
        description="Optional plane name, used in log messages only.",
        json_schema_extra={"ui_label": "Label", "ui_group": "basic"},
    )
    latitude: float | None = Field(
        default=None,
        ge=-90.0,
        le=90.0,
        description="Plane latitude. Defaults to the site latitude.",
        json_schema_extra={"ui_label": "Latitude override", "ui_group": "advanced"},
    )
    longitude: float | None = Field(
        default=None,
        ge=-180.0,
        le=180.0,
        description="Plane longitude. Defaults to the site longitude.",
        json_schema_extra={"ui_label": "Longitude override", "ui_group": "advanced"},
    )
    declination: float = Field(
        ge=0.0,
        le=90.0,
        description="Panel tilt in degrees from horizontal. 0 = flat, 90 = vertical.",
        json_schema_extra={"ui_label": "Panel tilt", "ui_group": "basic"},
    )
    azimuth: float = Field(
        ge=-180.0,
        le=180.0,
        description="Panel orientation in degrees from south: 0 = south, -90 = east, 90 = west.",
        json_schema_extra={"ui_label": "Panel azimuth", "ui_group": "basic"},
    )
    peak_power_kwp: float = Field(
        gt=0.0,
        description="Nameplate DC power of this plane in kWp.",
        json_schema_extra={"ui_label": "Peak power (kWp)", "ui_group": "basic"},
    )
    inverter_kwp: float | None = Field(
        default=None,
        gt=0.0,
        description="AC limit of this plane's own inverter in kW. Null = no per-plane limit.",
        json_schema_extra={"ui_label": "Plane inverter (kW)", "ui_group": "basic"},
    )
    efficiency_factor: float = Field(
        default=1.0,
        gt=0.0,
        le=1.0,
        description="Linear system efficiency: inverter, cabling, soiling, degradation.",
        json_schema_extra={"ui_label": "Efficiency factor", "ui_group": "basic"},
    )
    tracking: Literal["none", "azimuth", "tilt", "dual"] = Field(
        default="none",
        description="Axis tracking mode: none, azimuth, tilt, or dual.",
        json_schema_extra={"ui_label": "Tracking", "ui_group": "advanced"},
    )
    damping_morning: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Fraction of production removed at sunrise, tapering to none at solar noon.",
        json_schema_extra={"ui_label": "Morning damping", "ui_group": "advanced"},
    )
    damping_evening: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Fraction of production removed at sunset, tapering from none at solar noon.",
        json_schema_extra={"ui_label": "Evening damping", "ui_group": "advanced"},
    )
    max_snowcover_depth_cm: float = Field(
        default=0.0,
        ge=0.0,
        description="Snow depth at which the plane produces nothing. 0 disables the correction.",
        json_schema_extra={"ui_label": "Snow cover depth (cm)", "ui_group": "advanced"},
    )
    horizon: list[HorizonPoint] | None = Field(
        default=None,
        description="Skyline profile in ascending compass bearing. Null disables horizon shading.",
        json_schema_extra={"ui_label": "Horizon profile", "ui_group": "advanced"},
    )
    partial_shading: bool = Field(
        default=False,
        description="Credit diffuse light while the horizon blocks the direct beam.",
        json_schema_extra={"ui_label": "Partial shading", "ui_group": "advanced"},
    )

    @property
    def use_horizon(self) -> bool:
        """Whether horizon shading applies to this plane.

        Derived rather than configured: a horizon profile that is present but
        switched off, or switched on but absent, are both configuration states
        that only exist to be got wrong.
        """
        return self.horizon is not None

    @model_validator(mode="after")
    def _check_horizon_profile(self) -> "PlaneConfig":
        """Reject a horizon profile that cannot be interpolated.

        The library interpolates the profile against the sun's bearing with
        ``numpy.interp``, which requires at least two points on a strictly
        increasing bearing axis. Fewer points, an unsorted list, or two
        elevations at the same bearing all produce silently wrong shading
        rather than an error, so they are rejected here.
        """
        if self.horizon is None:
            return self
        if len(self.horizon) < 2:
            raise ValueError(
                "horizon needs at least two points to be interpolated; "
                f"got {len(self.horizon)}"
            )
        bearings = [point.azimuth for point in self.horizon]
        for previous, current in zip(bearings, bearings[1:], strict=False):
            if current <= previous:
                raise ValueError(
                    "horizon points must be listed in strictly ascending azimuth "
                    f"order; {current} does not come after {previous}"
                )
        return self


class ArrayConfig(BaseModel):
    """One mimirheim PV array: the planes behind a single forecast topic.

    An array corresponds to one entry under ``pv_arrays`` in the mimirheim
    configuration, which is to say one thing the solver can curtail. Group
    planes into an array when they are metered and controlled together, which
    in practice means when they hang off the same inverter.

    Attributes:
        output_topic: MQTT topic for the forecast payload, published retained.
            When not set, derived as ``'{mimir_topic_prefix}/input/pv/{key}/forecast'``
            from the array's key in the ``arrays`` map. Set it explicitly when
            the key does not match the mimirheim ``pv_arrays`` device name.
        inverter_kwp: AC limit of the inverter shared by every plane in this
            array, in kW. The combined output of the planes is clamped to this
            value, which is what makes an east-west array on one inverter behave
            correctly: the two halves peak at different times, so the clamp
            binds far less often than the summed nameplate suggests. Mutually
            exclusive with the per-plane ``inverter_kwp``.
        planes: The coplanar module groups making up this array. At least one.
    """

    model_config = ConfigDict(extra="forbid")

    output_topic: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the forecast payload. Retained. "
            "Defaults to '{mimir_topic_prefix}/input/pv/{array_key}/forecast' when not set."
        ),
        json_schema_extra={
            "ui_label": "Output topic",
            "ui_group": "advanced",
            "ui_placeholder": "{mimir_topic_prefix}/input/pv/{array_key}/forecast",
            "ui_source": "pv_arrays",
        },
    )
    inverter_kwp: float | None = Field(
        default=None,
        gt=0.0,
        description="AC limit of the inverter shared by all planes in this array, in kW.",
        json_schema_extra={"ui_label": "Shared inverter (kW)", "ui_group": "basic"},
    )
    planes: list[PlaneConfig] = Field(
        min_length=1,
        description="Coplanar module groups making up this array.",
        json_schema_extra={"ui_label": "Planes", "ui_group": "basic"},
    )

    @model_validator(mode="after")
    def _check_inverter_placement(self) -> "ArrayConfig":
        """Reject a shared inverter and per-plane inverters in the same array.

        The library takes one ``ac_kwp`` argument whose type decides the
        meaning: a scalar clamps the combined output, a list clamps each plane
        on its own. Setting both here would silently discard the shared value.
        """
        if self.inverter_kwp is None:
            return self
        if any(plane.inverter_kwp is not None for plane in self.planes):
            raise ValueError(
                "an array sets either a shared inverter_kwp or per-plane inverter_kwp, "
                "not both"
            )
        return self


class ConfidenceDecayConfig(BaseModel):
    """Confidence values assigned to forecast steps by how far ahead they are.

    Steps further in the future are less reliable. mimirheim never computes
    decay itself; confidence is always supplied by whoever produces the
    forecast, so the schedule lives here. The values are applied per step
    based on the distance from the fetch time to the step timestamp.

    Note what the solver does with them today: the objective weights each step
    by ``SolveBundle.horizon_confidence``, which ``readiness.py`` sources from
    the *price* steps alone. ``resample_power`` reduces a PV forecast to plain
    power values, so these confidences go no further than the retained MQTT
    payload: they do not reach the solve bundle, the debug dumps, or the
    schedule. They are still worth setting correctly, because the input
    contract asks for them and they become live the moment PV confidence is
    folded into the weighting.

    Attributes:
        hours_0_to_6: Confidence for steps 0-6 hours ahead. Default 0.90.
        hours_6_to_24: Confidence for steps 6-24 hours ahead. Default 0.75.
        hours_24_to_48: Confidence for steps 24-48 hours ahead. Default 0.55.
        hours_48_plus: Confidence for steps more than 48 hours ahead. Default 0.35.
    """

    model_config = ConfigDict(extra="forbid")

    hours_0_to_6: float = Field(
        default=0.90,
        ge=0.0,
        le=1.0,
        description="Confidence for steps 0-6 h ahead.",
        json_schema_extra={"ui_label": "Confidence 0-6 h", "ui_group": "advanced"},
    )
    hours_6_to_24: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description="Confidence for steps 6-24 h ahead.",
        json_schema_extra={"ui_label": "Confidence 6-24 h", "ui_group": "advanced"},
    )
    hours_24_to_48: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description="Confidence for steps 24-48 h ahead.",
        json_schema_extra={"ui_label": "Confidence 24-48 h", "ui_group": "advanced"},
    )
    hours_48_plus: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="Confidence for steps 48+ h ahead.",
        json_schema_extra={"ui_label": "Confidence 48+ h", "ui_group": "advanced"},
    )


class PvOpenMeteoConfig(BaseModel):
    """Top-level configuration for the pv_openmeteo daemon.

    Attributes:
        mqtt: MQTT broker connection parameters.
        mimir_topic_prefix: The ``mqtt.topic_prefix`` configured in mimirheim.
            Used to derive the default ``output_topic`` of each array and the
            default ``mimir_trigger_topic``.
        trigger_topic: MQTT topic that triggers one fetch-and-publish cycle.
        open_meteo: Open-Meteo API settings shared by every array.
        site: Geographic location, inherited by every plane.
        arrays: Named map of PV arrays. The key is used as the mimirheim
            ``pv_arrays`` device name for topic derivation unless the array's
            ``output_topic`` is set explicitly.
        confidence_decay: Confidence values by forecast horizon.
        signal_mimir: If True, publish an empty message to ``mimir_trigger_topic``
            once per cycle, after at least one array has been published. A cycle
            in which every array failed publishes nothing and sends no trigger.
        mimir_trigger_topic: mimirheim's trigger topic. Defaults to the canonical
            trigger topic derived from ``mimir_topic_prefix``.
        ha_discovery: Home Assistant MQTT discovery configuration.
        stats_topic: MQTT topic where per-cycle run statistics are published.
    """

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig = Field(
        description="MQTT broker connection settings.",
        json_schema_extra={"ui_label": "MQTT", "ui_group": "basic"},
    )
    mimir_topic_prefix: str = Field(
        default="mimir",
        description="mimirheim mqtt.topic_prefix. Used to derive default array and trigger topics.",
        json_schema_extra={"ui_label": "mimirheim topic prefix", "ui_group": "advanced"},
    )
    trigger_topic: str = Field(
        description="MQTT topic that triggers a fetch cycle.",
        json_schema_extra={"ui_label": "Trigger topic", "ui_group": "advanced"},
    )
    open_meteo: OpenMeteoApiConfig = Field(
        default_factory=OpenMeteoApiConfig,
        description="Open-Meteo API configuration.",
        json_schema_extra={"ui_label": "Open-Meteo API", "ui_group": "basic"},
    )
    site: SiteConfig = Field(
        description="Geographic location of the installation.",
        json_schema_extra={"ui_label": "Site", "ui_group": "basic"},
    )
    arrays: dict[str, ArrayConfig] = Field(
        min_length=1,
        description="Named map of PV arrays, one per mimirheim pv_arrays device.",
        json_schema_extra={"ui_label": "PV arrays", "ui_group": "basic"},
    )
    confidence_decay: ConfidenceDecayConfig = Field(
        default_factory=ConfidenceDecayConfig,
        description="Per-band confidence values. Optional; defaults apply.",
        json_schema_extra={"ui_label": "Confidence decay", "ui_group": "advanced"},
    )
    signal_mimir: bool = Field(
        default=False,
        description="Publish to mimir_trigger_topic once per cycle in which an array was published.",
        json_schema_extra={"ui_label": "Signal mimirheim", "ui_group": "advanced"},
    )
    mimir_trigger_topic: str | None = Field(
        default=None,
        description="mimirheim trigger topic. Defaults to '{mimir_topic_prefix}/input/trigger'.",
        json_schema_extra={
            "ui_label": "mimirheim trigger topic",
            "ui_group": "advanced",
            "ui_placeholder": "{mimir_topic_prefix}/input/trigger",
        },
    )
    ha_discovery: HomeAssistantConfig | None = Field(
        default=None,
        description="HA MQTT discovery configuration.",
        json_schema_extra={"ui_label": "HA discovery", "ui_group": "advanced"},
    )
    stats_topic: str | None = Field(
        default=None,
        description="MQTT topic where per-cycle run statistics are published.",
        json_schema_extra={"ui_label": "Stats topic", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _derive_mimir_topics(self) -> "PvOpenMeteoConfig":
        """Fill in the mimirheim-side topics that were not set explicitly."""
        prefix = self.mimir_topic_prefix
        for key, array in self.arrays.items():
            if array.output_topic is None:
                array.output_topic = _topics.pv_forecast_topic(prefix, key)
        if self.mimir_trigger_topic is None:
            self.mimir_trigger_topic = _topics.trigger_topic(prefix)
        return self

    @model_validator(mode="after")
    def _apply_site_coordinates(self) -> "PvOpenMeteoConfig":
        """Copy the site coordinates into every plane that did not override them.

        Doing this once here means the fetcher never has to decide where a
        plane's coordinates come from.
        """
        for array in self.arrays.values():
            for plane in array.planes:
                if plane.latitude is None:
                    plane.latitude = self.site.latitude
                if plane.longitude is None:
                    plane.longitude = self.site.longitude
        return self

    @model_validator(mode="after")
    def _set_client_id_default(self) -> "PvOpenMeteoConfig":
        """Set the default MQTT client identifier when not explicitly configured."""
        if not self.mqtt.client_id:
            self.mqtt.client_id = "mimir-pv-openmeteo"
        return self
