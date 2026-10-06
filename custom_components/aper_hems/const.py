DOMAIN = "aper_hems"

CONF_PROFILE = "profile"
CONF_PRODUCTION_ENTITY = "production_entity"
CONF_CONSUMPTION_ENTITY = "consumption_entity"
CONF_GRID_ENTITY = "grid_entity"
CONF_ADD_BATTERY_ENTITY = "add_battery_entity"
CONF_OUT_BATTERY_ENTITY = "out_battery_entity"
CONF_SOC_ENTITY = "soc_entity"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_API_KEY = "api_key"
CONF_CONSUMERS = "consumers"
CONF_BRANDS = "brands"

# Advanced settings (reconfiguration)
CONF_BATTERY_ENTITY = "battery_entity"
CONF_GRID_IMPORT_POSITIVE = "grid_import_positive"
CONF_BATTERY_CHARGE_POSITIVE = "battery_charge_positive"
CONF_METER_PRODUCTION = "meter_production"
CONF_METER_IMPORT = "meter_import"
CONF_METER_EXPORT = "meter_export"
CONF_METER_BATTERY_CHARGE = "meter_battery_charge"
CONF_METER_BATTERY_DISCHARGE = "meter_battery_discharge"

DEFAULT_UPDATE_INTERVAL = 15

PROFILE_SOLAR_BATTERY = "solar_battery"
PROFILE_SOLAR = "solar"
PROFILE_CONSUMPTION = "consumption"

BRAND_KEYWORDS = {
    "whatwatt": ["whatwatt"],
    "fronius": ["fronius"],
    "solaredge": ["solaredge", "solar_edge"],
    "huawei": ["huawei", "fusionsolar"],
    "enphase": ["enphase", "envoy"],
    "shelly": ["shelly"],
    "mystrom": ["mystrom"],
    "senec": ["senec"],
    "sma": ["sma_", "_sma"],
    "kostal": ["kostal", "plenticore"],
}

GRID_HINTS = ["grid", "power_in", "power_out", "net", "meter", "reseau",
              "1_7_0", "2_7_0", "16_7_0", "import", "export"]
PROD_HINTS = ["prod", "pv", "solar", "photovoltaic", "generation",
              "power_ac", "onduleur", "inverter"]
CONSO_HINTS = ["conso", "load", "house", "home", "consumption", "verbrauch"]
BAT_IN_HINTS = ["charge", "bat_in", "battery_charge", "add_bat",
                "battery_input", "bat_power_in"]
BAT_OUT_HINTS = ["discharge", "bat_out", "battery_discharge", "out_bat",
                 "battery_output", "bat_power_out"]
SOC_HINTS = ["soc", "state_of_charge", "battery_level", "bat_percent"]

# APER site: the key is sent in the "Authorization: Bearer <key>" header
API_BASE_URL = "https://aper-association.ch"
DATA_API_URL = f"{API_BASE_URL}/api/energydata"
COVERAGE_API_URL = f"{API_BASE_URL}/api/energydata/coverage"

# Quarters of an hour exchanged with the server: UTC, "2026-10-06T10:15:00Z"
SLOT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CONSUMER_NAME_LENGTH = 100

BACKFILL_DAYS = 45
BACKFILL_MAX_RETRIES = 2
BACKFILL_INTERVAL_HOURS = 6
