"""Constants for the pid_climate integration."""

DOMAIN = "pid_climate"

# Platform-level configuration
CONF_TARGET_ENTITY = "target_entity"
CONF_ROOM_SENSOR = "room_sensor"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_HIGH_POWER_SWITCH = "high_power_switch"
CONF_EIGHT_DEG_SWITCH = "eight_deg_switch"
CONF_EIGHT_DEG_THRESHOLD = "eight_deg_threshold"
CONF_EIGHT_DEG_SETTLE = "eight_deg_settle"
CONF_FAN_MODES = "fan_modes"
CONF_FAN_ONLY = "fan_only"

CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_TARGET_TEMP = "target_temp"
CONF_TARGET_TEMP_STEP = "target_temp_step"

CONF_COMMAND_MIN = "command_min"
CONF_COMMAND_MAX = "command_max"
CONF_COMMAND_STEP = "command_step"

CONF_SAMPLING_PERIOD = "sampling_period"
CONF_MIN_SETPOINT_INTERVAL = "min_setpoint_interval"
CONF_RESYNC_INTERVAL = "resync_interval"
CONF_MAX_SAMPLE_GAP = "max_sample_gap"

CONF_HEAT = "heat"
CONF_COOL = "cool"

# Per-mode configuration
CONF_KP = "kp"
CONF_KI = "ki"
CONF_KE = "ke"
CONF_OFFSET = "offset"
CONF_INTEGRAL_MIN = "integral_min"
CONF_INTEGRAL_MAX = "integral_max"
CONF_AC_MOD_LOWER = "ac_modulation_lower_margin"
CONF_AC_MOD_UPPER = "ac_modulation_upper_margin"
CONF_OVERHEAT_PROTECTION = "overheat_protection"
CONF_INTEGRAL_BAND = "integral_band"
CONF_HOLD_BASE = "integral_hold_base"
CONF_HOLD_PER_DEGREE = "integral_hold_per_degree"
CONF_HOLD_MAX = "integral_hold_max"
CONF_HOLD_RELEASE_BAND = "integral_hold_release_band"
CONF_AUTO_OFF = "auto_off"
CONF_AUTO_OFF_MARGIN = "auto_off_margin"

# Services
SERVICE_SET_INTEGRAL = "set_integral"
SERVICE_RESET_INTEGRAL = "reset_integral"
SERVICE_CLEAR_HOLD = "clear_hold"
ATTR_MODE = "mode"
ATTR_VALUE = "value"

# Attribute names on the underlying climate entity
ATTR_CURRENT_TEMPERATURE = "current_temperature"
ATTR_FAN_MODE = "fan_mode"
ATTR_FAN_MODES = "fan_modes"
ATTR_PRESET_MODE = "preset_mode"

PRESET_NONE = "none"
PRESET_BOOST = "boost"

DEFAULT_FAN_MODES = ["Auto", "Low", "Medium", "High"]

SIGNAL_UPDATE = f"{DOMAIN}_update"
