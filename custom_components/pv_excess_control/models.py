"""Data models for PV Excess Control. Pure Python - no HA dependencies."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .const import Action, BatteryStrategy, PlanReason


@dataclass
class HourlyForecast:
    """Expected solar production for a specific hour."""
    start: datetime
    end: datetime
    expected_kwh: float
    expected_watts: float  # average watts during this hour


@dataclass
class ForecastData:
    """Normalized forecast data from any provider."""
    remaining_today_kwh: float
    hourly_breakdown: list[HourlyForecast] = field(default_factory=list)
    tomorrow_total_kwh: float | None = None


@dataclass(frozen=True)
class PowerState:
    """Snapshot of the current power situation.

    Sensor-backed fields are ``float | None``: ``None`` means the
    underlying HA sensor was ``unavailable`` when this snapshot was
    taken, while ``0.0`` means the sensor reported a genuine zero.
    Downstream consumers must not conflate the two.
    """
    pv_production: float | None
    grid_export: float | None
    grid_import: float | None
    load_power: float | None
    excess_power: float | None
    battery_soc: float | None
    battery_power: float | None
    ev_soc: float | None
    timestamp: datetime


@dataclass
class ApplianceConfig:
    """Configuration for a managed appliance."""
    id: str
    name: str
    entity_id: str
    priority: int  # 1-1000, 1 = highest
    phases: int
    nominal_power: float
    actual_power_entity: str | None

    # Dynamic current
    dynamic_current: bool
    current_entity: str | None
    min_current: float
    max_current: float

    # EV-specific
    ev_soc_entity: str | None
    ev_connected_entity: str | None

    # Big consumer battery protection
    is_big_consumer: bool
    battery_max_discharge_override: float | None

    # Constraints
    on_only: bool
    min_daily_runtime: timedelta | None
    max_daily_runtime: timedelta | None
    schedule_deadline: time | None

    # Switch interval in seconds
    switch_interval: int

    # Grid supplementation
    allow_grid_supplement: bool
    max_grid_power: float | None

    # Fields with defaults (must come after non-default fields)
    cheap_price_threshold: float | None = None  # Per-appliance cheap threshold; None = use global
    ev_target_soc: float | None = None
    start_after: time | None = None
    end_before: time | None = None
    averaging_window: int | None = None  # Per-appliance history window in seconds (None = use global)
    requires_appliance: str | None = None  # Subentry ID of required dependency appliance
    helper_only: bool = False  # If True, this appliance runs only when at least one dependent (an appliance with requires_appliance pointing at it) is running. Has no effect on its own allocation.
    override_active: bool = False
    override_until: datetime | None = None

    # Preemption protection
    protect_from_preemption: bool = False

    # Dynamic current step size (default 0.1A)
    current_step: float = 0.1

    # Max daily activations (None = unlimited)
    max_daily_activations: int | None = None

    # Per-appliance activation buffer in watts (None = use type-dependent default)
    on_threshold: int | None = None

    # Completion power threshold: power below which runtime stops counting (None = disabled)
    completion_power_threshold: float | None = None

    # Cheap-window target current for dynamic-current appliances.
    # When set and a cheap tariff window is active and allow_grid_supplement is True,
    # the optimizer drives the appliance to this amperage instead of the conservative
    # min_current. Capped by max_grid_power and max_current.
    cheap_grid_target_current: float | None = None


@dataclass
class ApplianceState:
    """Runtime state of a managed appliance."""
    appliance_id: str
    is_on: bool
    current_power: float
    current_amperage: float | None
    runtime_today: timedelta
    energy_today: float  # kWh
    last_state_change: datetime | None
    ev_connected: bool | None  # None if not EV
    ev_soc: float | None = None  # EV state of charge percentage
    activations_today: int = 0


@dataclass(frozen=True)
class TariffWindow:
    """A time window with an energy price."""
    start: datetime
    end: datetime
    price: float
    is_cheap: bool


@dataclass
class TariffInfo:
    """Current tariff context for the optimizer."""
    current_price: float
    feed_in_tariff: float
    cheap_price_threshold: float
    battery_charge_price_threshold: float
    windows: list[TariffWindow] = field(default_factory=list)

    @property
    def net_savings_per_kwh(self) -> float:
        """Net savings per kWh when using solar instead of grid."""
        return self.current_price - self.feed_in_tariff


@dataclass(frozen=True)
class BatteryTarget:
    """Battery charging strategy for the planning horizon."""
    target_soc: float
    target_time: datetime
    strategy: BatteryStrategy


@dataclass(frozen=True)
class BatteryConfig:
    """Battery system configuration."""
    capacity_kwh: float
    max_discharge_entity: str | None
    max_discharge_default: float | None
    target_soc: float
    target_time: time
    strategy: BatteryStrategy
    allow_grid_charging: bool


@dataclass
class TimeSlot:
    """A planning time slot with expected conditions."""
    start: datetime
    end: datetime
    expected_solar_watts: float  # Expected PV production
    expected_excess_watts: float  # Expected excess after household load
    price: float  # Energy price during this slot
    is_cheap: bool  # Below cheap_price_threshold


@dataclass
class BatteryAllocation:
    """How battery charging should be allocated across the timeline."""
    charging_needed_kwh: float  # Total energy needed to reach target
    slots_reserved: list[TimeSlot]  # Slots where excess is reserved for battery
    excess_after_battery: dict[int, float]  # slot_index -> remaining excess after battery allocation


@dataclass(frozen=True)
class PlanEntry:
    """A planned action for an appliance."""
    appliance_id: str
    action: Action
    target_current: float | None
    window: TariffWindow | None
    reason: PlanReason
    priority: int


@dataclass
class Plan:
    """Output of the planner - schedule for the next hours."""
    created_at: datetime
    horizon: timedelta
    entries: list[PlanEntry]
    battery_target: BatteryTarget
    confidence: float  # 0.0-1.0
    grid_charge_recommended: bool = False  # True if solar forecast alone is insufficient to reach battery target


@dataclass(frozen=True)
class ControlDecision:
    """Output of the optimizer for a single appliance."""
    appliance_id: str
    action: Action
    target_current: float | None
    reason: str
    overrides_plan: bool
    bypasses_cooldown: bool = False


@dataclass(frozen=True)
class BatteryDischargeAction:
    """Battery discharge limit decision."""
    should_limit: bool
    max_discharge_watts: float | None = None


@dataclass
class OptimizerResult:
    """Complete output of the optimizer for a single cycle."""
    decisions: list[ControlDecision]
    battery_discharge_action: BatteryDischargeAction


@dataclass(frozen=True)
class InverterGridChargeConfig:
    """Maps generic engage/disengage to inverter-specific entity writes.

    Patterns supported:
    - Single switch / input_boolean (only enable_entity_id; engage/disengage values "on"/"off").
    - Two-step: select-mode + select-command (mode_entity_id + enable_entity_id, no power).
    - Three-step (e.g. Sungrow mkaiser): select-mode + select-command + number-power.
    """
    enable_entity_id: str
    enable_engage_value: str
    enable_disengage_value: str

    mode_entity_id: str | None = None
    mode_engage_value: str | None = None
    mode_disengage_value: str | None = None

    power_entity_id: str | None = None
