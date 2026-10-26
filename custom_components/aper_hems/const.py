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

# Solar production forecast: the solar fields (one per roof side), each with the sensors of Open-Meteo Solar Forecast (or
# Forecast.Solar) for today and tomorrow, whose "watts" attribute gives the power expected at each time; all of them are added
# up and sent with the measurements of each quarter of an hour
CONF_SOLAR_FIELDS = "solar_fields"
CONF_FORECAST_TODAY_ENTITY = "today_entity"
CONF_FORECAST_TOMORROW_ENTITY = "tomorrow_entity"
# The first version: a plain list of sensors, still read
CONF_SOLAR_FORECAST = "solar_forecast_entities"
SOLAR_FORECAST_ATTRIBUTE = "watts"
# How long the forecasts read are kept, for the quarters sent later (the night, the backfill of the last hours)
SOLAR_FORECAST_KEEP_HOURS = 72
# Points further apart are not interpolated
SOLAR_FORECAST_MAX_GAP_MINUTES = 120

# Charging stations and cars, kept in the options for the piloting to come
CONF_CHARGERS = "chargers"
CONF_CARS = "cars"
CONF_NAME = "name"
CONF_CHARGER_SWITCH_ENTITY = "switch_entity"
CONF_CHARGER_CURRENT_ENTITY = "current_entity"
# The status of the station (charging, plugged in, ready…), or just "cable plugged in": the site reads its words.
# Its key keeps its first name, so that the stations already configured keep it
CONF_CHARGER_PLUGGED_ENTITY = "plugged_entity"
CONF_CHARGER_POWER_ENTITY = "power_entity"
CONF_CHARGER_ENERGY_ENTITY = "energy_entity"
# The range the station can be controlled in: its phases (switchable between mono and tri, mono or tri) and its current
CONF_CHARGER_PHASES = "phases"
# Its brand: the site reads its statuses with the words of the brand, and the control will use its own way
CONF_CHARGER_BRAND = "brand"
CHARGER_BRANDS = {
    "wallbox": "Wallbox",
    "easee": "Easee",
    "tesla": "Tesla Wall Connector",
    "zaptec": "Zaptec",
    "goe": "go-e Charger",
    "keba": "KEBA",
    "other": "Autre",
}
CONF_CHARGER_MIN_CURRENT = "min_current"
CONF_CHARGER_MAX_CURRENT = "max_current"
PHASES_SWITCHABLE = "mono_tri"
PHASES_MONO = "mono"
PHASES_TRI = "tri"
DEFAULT_MIN_CURRENT = 6
DEFAULT_MAX_CURRENT = 16
CONF_CAR_BRAND = "brand"
CONF_CAR_SOC_ENTITY = "soc_entity"
CONF_CAR_STATUS_ENTITY = "status_entity"
CONF_CAR_CAPACITY = "capacity_kwh"
CONF_CAR_CHARGER = "charger"

# The highest power the home can take from the grid (kW: its subscription, its main breaker): the HEMS never plans a charge
# above it
CONF_GRID_MAX_KW = "grid_max_kw"

# The solar batteries of the home: their name, capacity and level (%), sent to the site with the cars (the simulator starts
# from them)
CONF_BATTERIES = "batteries"
CONF_BATTERY_CAPACITY = "capacity_kwh"
CONF_BATTERY_LEVEL_ENTITY = "soc_entity"
# How the HEMS may use it: its lowest and highest level (%), what it gives back of what it takes (%), what a kWh through it
# costs in wear (ct) and the level wanted at the end of the day (%), for the next night until the prices of the next day
CONF_BATTERY_MIN_SOC = "min_soc"
CONF_BATTERY_MAX_SOC = "max_soc"
CONF_BATTERY_EFFICIENCY = "efficiency"
CONF_BATTERY_WEAR = "wear_ct"
CONF_BATTERY_END_SOC = "end_soc"
# The highest power it charges and gives at (kW)
CONF_BATTERY_CHARGE_KW = "charge_kw"
CONF_BATTERY_DISCHARGE_KW = "discharge_kw"
BATTERY_DEFAULTS = {
    CONF_BATTERY_CHARGE_KW: 5,
    CONF_BATTERY_DISCHARGE_KW: 5,
    CONF_BATTERY_MIN_SOC: 10,
    CONF_BATTERY_MAX_SOC: 100,
    CONF_BATTERY_EFFICIENCY: 90,
    CONF_BATTERY_WEAR: 3,
    CONF_BATTERY_END_SOC: 30,
}

DEFAULT_UPDATE_INTERVAL = 15

# APER site: the key is sent in the "Authorization: Bearer <key>" header
API_BASE_URL = "https://aper-association.ch"
API_URL = f"{API_BASE_URL}/api/energydata"
COVERAGE_API_URL = f"{API_BASE_URL}/api/energydata/coverage"
# Cars and charging stations with the state of their entities, for the cockpit's "Véhicule électrique" page: sent at each
# change (every 10 s at most) and every 5 minutes anyway
DEVICES_API_URL = f"{API_BASE_URL}/api/energydata/devices"
DEVICES_MIN_SECONDS = 10
DEVICES_HEARTBEAT_SECONDS = 300
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
