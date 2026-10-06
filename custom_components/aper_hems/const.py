DOMAIN = "aper_hems"

CONF_PRODUCTION_ENTITY = "production_entity"
CONF_GRID_ENTITY = "grid_entity"
CONF_BATTERY_ENTITY = "battery_entity"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_API_KEY = "api_key"
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_GRID_IMPORT_POSITIVE = "grid_import_positive"
CONF_METER_PRODUCTION = "meter_production"
CONF_METER_IMPORT = "meter_import"
CONF_METER_EXPORT = "meter_export"
CONF_METER_BATTERY_CHARGE = "meter_battery_charge"
CONF_METER_BATTERY_DISCHARGE = "meter_battery_discharge"
CONF_BATTERY_CHARGE_POSITIVE = "battery_charge_positive"
CONF_CONSUMERS = "consumers"
CONF_SUBTRACT_ENTITIES = "subtract_entities"
CONF_CONSUMER_POWER_ENTITY = "consumer_power_entity"

DEFAULT_UPDATE_INTERVAL = 15

# APER site: the key is sent in the "Authorization: Bearer <key>" header
API_BASE_URL = "https://aper-association.ch"
API_URL = f"{API_BASE_URL}/api/energydata"
COVERAGE_API_URL = f"{API_BASE_URL}/api/energydata/coverage"
# Live values for the cockpit: a WebSocket kept open to the site, which says when a cockpit is watching
# ("watch" message). Values are then sent at each change (at most once a second) and every 10 s anyway.
LIVE_WS_URL = "wss://aper-association.ch/api/energydata/live"
LIVE_HEARTBEAT_SECONDS = 10
LIVE_RECONNECT_MIN_SECONDS = 5
LIVE_RECONNECT_MAX_SECONDS = 300

# Quarters of an hour exchanged with the server: UTC, "2026-10-06T10:15:00Z"
SLOT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CONSUMER_NAME_LENGTH = 100

# Weather of each quarter of an hour, fetched from Home Assistant so that each home has its own Open-Meteo quota.
# Free for non-commercial use, data under CC BY 4.0 (Open-Meteo.com).
WEATHER_API_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_VARIABLES = (
    "temperature_2m,apparent_temperature,wind_speed_10m,uv_index,relative_humidity_2m,"
    "surface_pressure,precipitation,cloud_cover,weather_code"
)
# The forecast API keeps about three months of past data
WEATHER_MAX_PAST_DAYS = 90

BACKFILL_DAYS = 45
BACKFILL_MAX_RETRIES = 2
BACKFILL_INTERVAL_HOURS = 6
