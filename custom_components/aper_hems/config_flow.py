from __future__ import annotations

import asyncio
import re
from datetime import timedelta

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_API_KEY,
    CONF_GRID_MAX_KW,
    CONF_PLANNING_ENTITY,
    DEFAULT_PLANNING_ENTITY,
    CONF_CONTROL_MODE_ENTITY,
    CONF_CONTROL_CHARGE_GRID,
    CONF_CONTROL_GRID_ONLY,
    CONF_CONTROL_AUTO,
    CONF_CONTROL_TEST,
    CONTROL_DEFAULTS,
    CHARGERS_CONTROL_DEFAULTS,
    CONF_CHARGERS_CONTROL,
    CONF_CHARGERS_CONTROL_TEST,
    CONF_BATTERIES,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_LEVEL_ENTITY,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_EFFICIENCY,
    CONF_BATTERY_WEAR,
    CONF_BATTERY_END_SOC,
    CONF_BATTERY_CHARGE_KW,
    CONF_BATTERY_DISCHARGE_KW,
    BATTERY_DEFAULTS,
    CONF_FORECAST_TODAY_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_SOLAR_FIELDS,
    CONF_BATTERY_CHARGE_POSITIVE,
    CONF_BATTERY_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CAR_BRAND,
    CONF_CAR_CAPACITY,
    CONF_CAR_CHARGER,
    CONF_CAR_SOC_ENTITY,
    CONF_CAR_STATUS_ENTITY,
    CONF_CARS,
    CONF_CHARGER_CURRENT_ENTITY,
    CONF_CHARGER_BRAND,
    CONF_CHARGER_ENERGY_ENTITY,
    CONF_CHARGER_MAX_CURRENT,
    CONF_CHARGER_MIN_CURRENT,
    CONF_CHARGER_PHASES,
    CONF_CHARGER_PLUGGED_ENTITY,
    CONF_CHARGER_POWER_ENTITY,
    CONF_CHARGER_SWITCH_ENTITY,
    CONF_CHARGERS,
    CONF_CONSUMER_POWER_ENTITY,
    CONF_CONSUMERS,
    CONF_GRID_ENTITY,
    CONF_GRID_IMPORT_POSITIVE,
    CONF_METER_BATTERY_CHARGE,
    CONF_METER_BATTERY_DISCHARGE,
    CONF_METER_EXPORT,
    CONF_METER_IMPORT,
    CONF_METER_PRODUCTION,
    CONF_NAME,
    CONF_PRODUCTION_ENTITY,
    CONF_SUBTRACT_ENTITIES,
    CONF_UPDATE_INTERVAL,
    CHARGER_BRANDS,
    COVERAGE_API_URL,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MIN_CURRENT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    PHASES_MONO,
    PHASES_SWITCHABLE,
    PHASES_TRI,
    SLOT_FORMAT,
)

# Optional entities, saved empty when left blank
_OPTIONAL_KEYS = (
    CONF_PRODUCTION_ENTITY, CONF_BATTERY_ENTITY, CONF_BATTERY_SOC_ENTITY, CONF_METER_PRODUCTION,
    CONF_METER_IMPORT, CONF_METER_EXPORT, CONF_METER_BATTERY_CHARGE, CONF_METER_BATTERY_DISCHARGE,
)


async def _check_api_key(hass, api_key: str) -> str | None:
    """Asks the APER site whether the key is valid. Returns an error code, or None."""
    now = dt_util.utcnow()
    try:
        async with async_get_clientsession(hass).get(
            COVERAGE_API_URL,
            params={"from": (now - timedelta(hours=1)).strftime(SLOT_FORMAT), "to": now.strftime(SLOT_FORMAT)},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 401:
                return "invalid_auth"
            if resp.status != 200:
                return "cannot_connect"
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return "cannot_connect"
    return None


def _optional_entity(schema: dict, key: str, defaults: dict, selector: EntitySelector) -> None:
    value = defaults.get(key)
    if value:
        schema[vol.Optional(key, default=value)] = selector
    else:
        schema[vol.Optional(key)] = selector


def _user_schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    schema = {}
    power = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power"))
    sensor = EntitySelector(EntitySelectorConfig(domain="sensor"))

    _optional_entity(schema, CONF_PRODUCTION_ENTITY, d, power)
    schema[vol.Required(CONF_GRID_ENTITY, default=d.get(CONF_GRID_ENTITY))] = power
    schema[vol.Required(CONF_GRID_IMPORT_POSITIVE, default=d.get(CONF_GRID_IMPORT_POSITIVE, True))] = BooleanSelector()
    _optional_entity(schema, CONF_BATTERY_ENTITY, d, power)
    _optional_entity(schema, CONF_BATTERY_SOC_ENTITY, d,
                     EntitySelector(EntitySelectorConfig(domain="sensor", device_class="battery")))
    _optional_entity(schema, CONF_METER_PRODUCTION, d, sensor)
    _optional_entity(schema, CONF_METER_IMPORT, d, sensor)
    _optional_entity(schema, CONF_METER_EXPORT, d, sensor)
    _optional_entity(schema, CONF_METER_BATTERY_CHARGE, d, sensor)
    _optional_entity(schema, CONF_METER_BATTERY_DISCHARGE, d, sensor)
    schema[vol.Required(CONF_BATTERY_CHARGE_POSITIVE, default=d.get(CONF_BATTERY_CHARGE_POSITIVE, True))] = BooleanSelector()

    schema[vol.Required(CONF_UPDATE_INTERVAL, default=d.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))] = NumberSelector(
        NumberSelectorConfig(min=5, max=1440, step=5, mode=NumberSelectorMode.BOX, unit_of_measurement="min")
    )
    schema[vol.Required(CONF_API_KEY, default=d.get(CONF_API_KEY))] = TextSelector(
        TextSelectorConfig(type="password")
    )

    return vol.Schema(schema)


class AperHemsOptionsFlow(OptionsFlow):
    """Three lists: the consumers sent to the "CourbesCharge" table, the charging stations and the cars (kept for the
    piloting to come)."""

    def __init__(self):
        self._edit_index: int | None = None

    def _items(self, key: str) -> list:
        return list(self.config_entry.options.get(key, []))

    def _save(self, key: str, items: list):
        # The other lists are kept: the options are replaced as a whole
        return self.async_create_entry(data={**self.config_entry.options, key: items})

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["grid", "consumers", "chargers", "cars", "batteries", "solar_fields", "planning", "control", "chargers_control"],
            description_placeholders={
                "consumers": str(len(self._items(CONF_CONSUMERS))),
                "chargers": str(len(self._items(CONF_CHARGERS))),
                "cars": str(len(self._items(CONF_CARS))),
                "batteries": str(len(self._items(CONF_BATTERIES))),
                "forecasts": str(len(self._items(CONF_SOLAR_FIELDS))),
            },
        )

    # ---------- the connection to the grid ----------

    async def async_step_grid(self, user_input=None):
        if user_input is not None:
            options = {k: v for k, v in self.config_entry.options.items() if k != CONF_GRID_MAX_KW}
            if user_input.get(CONF_GRID_MAX_KW):
                options[CONF_GRID_MAX_KW] = float(user_input[CONF_GRID_MAX_KW])
            return self.async_create_entry(data=options)
        power = NumberSelector(NumberSelectorConfig(min=1, max=500, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="kW"))
        current = self.config_entry.options.get(CONF_GRID_MAX_KW)
        key = vol.Optional(CONF_GRID_MAX_KW, default=current) if current else vol.Optional(CONF_GRID_MAX_KW)
        return self.async_show_form(step_id="grid", data_schema=vol.Schema({key: power}))

    # ---------- the planning of the HEMS ----------

    async def async_step_planning(self, user_input=None):
        errors = {}
        if user_input is not None:
            entity_id = (user_input.get(CONF_PLANNING_ENTITY) or "").strip().lower()
            if entity_id and not re.fullmatch(r"sensor\.[a-z0-9_]+", entity_id):
                errors[CONF_PLANNING_ENTITY] = "planning_entity"
            else:
                options = {k: v for k, v in self.config_entry.options.items() if k != CONF_PLANNING_ENTITY}
                options[CONF_PLANNING_ENTITY] = entity_id
                return self.async_create_entry(data=options)
        current = self.config_entry.options.get(CONF_PLANNING_ENTITY, DEFAULT_PLANNING_ENTITY)
        return self.async_show_form(
            step_id="planning",
            data_schema=vol.Schema({vol.Optional(CONF_PLANNING_ENTITY, default=current): TextSelector(TextSelectorConfig())}),
            errors=errors,
        )

    # ---------- the control of the battery ----------

    async def async_step_control(self, user_input=None):
        keys = (CONF_CONTROL_MODE_ENTITY, CONF_CONTROL_CHARGE_GRID, CONF_CONTROL_GRID_ONLY, CONF_CONTROL_AUTO, CONF_CONTROL_TEST)
        if user_input is not None:
            options = {k: v for k, v in self.config_entry.options.items() if k not in keys}
            if user_input.get(CONF_CONTROL_MODE_ENTITY):
                options.update({k: user_input[k] for k in keys if k in user_input})
            return self.async_create_entry(data=options)
        current = {**CONTROL_DEFAULTS, **{k: v for k, v in self.config_entry.options.items() if k in keys}}
        mode = current.get(CONF_CONTROL_MODE_ENTITY)
        schema = {
            (vol.Optional(CONF_CONTROL_MODE_ENTITY, default=mode) if mode else vol.Optional(CONF_CONTROL_MODE_ENTITY)):
                EntitySelector(EntitySelectorConfig(domain=["input_select", "select"])),
            vol.Required(CONF_CONTROL_CHARGE_GRID, default=current[CONF_CONTROL_CHARGE_GRID]): TextSelector(TextSelectorConfig()),
            vol.Required(CONF_CONTROL_GRID_ONLY, default=current[CONF_CONTROL_GRID_ONLY]): TextSelector(TextSelectorConfig()),
            vol.Required(CONF_CONTROL_AUTO, default=current[CONF_CONTROL_AUTO]): TextSelector(TextSelectorConfig()),
            vol.Required(CONF_CONTROL_TEST, default=current[CONF_CONTROL_TEST]): BooleanSelector(),
        }
        return self.async_show_form(step_id="control", data_schema=vol.Schema(schema))

    # ---------- the control of the charging stations ----------

    async def async_step_chargers_control(self, user_input=None):
        keys = tuple(CHARGERS_CONTROL_DEFAULTS)
        if user_input is not None:
            options = {k: v for k, v in self.config_entry.options.items() if k not in keys}
            options.update({k: user_input[k] for k in keys if k in user_input})
            return self.async_create_entry(data=options)
        current = {**CHARGERS_CONTROL_DEFAULTS, **{k: v for k, v in self.config_entry.options.items() if k in keys}}
        schema = {
            vol.Required(CONF_CHARGERS_CONTROL, default=current[CONF_CHARGERS_CONTROL]): BooleanSelector(),
            vol.Required(CONF_CHARGERS_CONTROL_TEST, default=current[CONF_CHARGERS_CONTROL_TEST]): BooleanSelector(),
        }
        return self.async_show_form(step_id="chargers_control", data_schema=vol.Schema(schema))

    # ---------- solar batteries ----------

    async def async_step_batteries(self, user_input=None):
        def describe(b):
            parts = [b[CONF_NAME], f"{b[CONF_BATTERY_CAPACITY]:g} kWh"]
            if b.get(CONF_BATTERY_CHARGE_KW) is not None:
                parts.append(f"charge {b[CONF_BATTERY_CHARGE_KW]:g} kW, décharge {b[CONF_BATTERY_DISCHARGE_KW]:g} kW")
            if b.get(CONF_BATTERY_MIN_SOC) is not None:
                parts.append(f"{b[CONF_BATTERY_MIN_SOC]:g}–{b[CONF_BATTERY_MAX_SOC]:g} %, fin de journée {b[CONF_BATTERY_END_SOC]:g} %")
            if b.get(CONF_BATTERY_LEVEL_ENTITY):
                parts.append(f"niveau : {b[CONF_BATTERY_LEVEL_ENTITY]}")
            return "• " + ", ".join(parts)

        return await self._list_step("batteries", CONF_BATTERIES, "une batterie", lambda b: b[CONF_NAME], describe, user_input)

    async def async_step_add_batterie(self, user_input=None):
        return await self._edit_step("add_batterie", CONF_BATTERIES, self._battery_schema, self._battery_from_input,
                                     user_input, adding=True, validate=self._battery_errors)

    async def async_step_edit_batterie(self, user_input=None):
        return await self._edit_step("edit_batterie", CONF_BATTERIES, self._battery_schema, self._battery_from_input,
                                     user_input, adding=False, validate=self._battery_errors)

    @staticmethod
    def _battery_errors(user_input: dict) -> dict:
        if user_input[CONF_BATTERY_MIN_SOC] >= user_input[CONF_BATTERY_MAX_SOC]:
            return {CONF_BATTERY_MAX_SOC: "soc_range"}
        return {}

    @staticmethod
    def _battery_from_input(user_input: dict) -> dict:
        battery = {CONF_NAME: user_input[CONF_NAME].strip(), CONF_BATTERY_CAPACITY: float(user_input[CONF_BATTERY_CAPACITY])}
        for key in BATTERY_DEFAULTS:
            battery[key] = float(user_input[key])
        if user_input.get(CONF_BATTERY_LEVEL_ENTITY):
            battery[CONF_BATTERY_LEVEL_ENTITY] = user_input[CONF_BATTERY_LEVEL_ENTITY]
        return battery

    @staticmethod
    def _battery_schema(defaults=None):
        d = defaults or {}
        capacity = NumberSelector(NumberSelectorConfig(min=0.5, max=500, step=0.1, mode=NumberSelectorMode.BOX,
                                                       unit_of_measurement="kWh"))
        schema = {vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): TextSelector(TextSelectorConfig(type="text"))}
        if d.get(CONF_BATTERY_CAPACITY):
            schema[vol.Required(CONF_BATTERY_CAPACITY, default=d[CONF_BATTERY_CAPACITY])] = capacity
        else:
            schema[vol.Required(CONF_BATTERY_CAPACITY)] = capacity
        _optional_entity(schema, CONF_BATTERY_LEVEL_ENTITY, d, EntitySelector(EntitySelectorConfig(domain=["sensor", "number"])))
        power = NumberSelector(NumberSelectorConfig(min=0.1, max=100, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="kW"))
        for key in (CONF_BATTERY_CHARGE_KW, CONF_BATTERY_DISCHARGE_KW):
            schema[vol.Required(key, default=d.get(key, BATTERY_DEFAULTS[key]))] = power
        percent = NumberSelector(NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="%"))
        for key in (CONF_BATTERY_MIN_SOC, CONF_BATTERY_MAX_SOC, CONF_BATTERY_END_SOC):
            schema[vol.Required(key, default=d.get(key, BATTERY_DEFAULTS[key]))] = percent
        schema[vol.Required(CONF_BATTERY_EFFICIENCY, default=d.get(CONF_BATTERY_EFFICIENCY, BATTERY_DEFAULTS[CONF_BATTERY_EFFICIENCY]))] = \
            NumberSelector(NumberSelectorConfig(min=50, max=100, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="%"))
        schema[vol.Required(CONF_BATTERY_WEAR, default=d.get(CONF_BATTERY_WEAR, BATTERY_DEFAULTS[CONF_BATTERY_WEAR]))] = \
            NumberSelector(NumberSelectorConfig(min=0, max=50, step=0.5, mode=NumberSelectorMode.BOX, unit_of_measurement="ct/kWh"))
        return vol.Schema(schema)

    # ---------- solar fields (forecast) ----------

    async def async_step_solar_fields(self, user_input=None):
        def describe(f):
            sensors = [f[CONF_FORECAST_TODAY_ENTITY]] + ([f[CONF_FORECAST_TOMORROW_ENTITY]] if f.get(CONF_FORECAST_TOMORROW_ENTITY) else [])
            return f"• {f[CONF_NAME]} ({', '.join(sensors)})"

        return await self._list_step("solar_fields", CONF_SOLAR_FIELDS, "un champ solaire", lambda f: f[CONF_NAME], describe,
                                     user_input)

    async def async_step_add_solar_field(self, user_input=None):
        return await self._edit_step("add_solar_field", CONF_SOLAR_FIELDS, self._solar_field_schema, self._solar_field_from_input,
                                     user_input, adding=True)

    async def async_step_edit_solar_field(self, user_input=None):
        return await self._edit_step("edit_solar_field", CONF_SOLAR_FIELDS, self._solar_field_schema, self._solar_field_from_input,
                                     user_input, adding=False)

    @staticmethod
    def _solar_field_from_input(user_input: dict) -> dict:
        field = {CONF_NAME: user_input[CONF_NAME].strip(), CONF_FORECAST_TODAY_ENTITY: user_input[CONF_FORECAST_TODAY_ENTITY]}
        if user_input.get(CONF_FORECAST_TOMORROW_ENTITY):
            field[CONF_FORECAST_TOMORROW_ENTITY] = user_input[CONF_FORECAST_TOMORROW_ENTITY]
        return field

    @staticmethod
    def _solar_field_schema(defaults=None):
        d = defaults or {}
        sensor = EntitySelector(EntitySelectorConfig(domain="sensor"))
        schema = {vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): TextSelector(TextSelectorConfig(type="text"))}
        if d.get(CONF_FORECAST_TODAY_ENTITY):
            schema[vol.Required(CONF_FORECAST_TODAY_ENTITY, default=d[CONF_FORECAST_TODAY_ENTITY])] = sensor
        else:
            schema[vol.Required(CONF_FORECAST_TODAY_ENTITY)] = sensor
        _optional_entity(schema, CONF_FORECAST_TOMORROW_ENTITY, d, sensor)
        return vol.Schema(schema)

    async def _list_step(self, step_id: str, key: str, noun: str, label, describe, user_input):
        """A list with add, edit and remove: the action chosen leads to add_<step> or edit_<step>."""
        items = self._items(key)
        if user_input is not None:
            action = user_input.get("action")
            if action == "add":
                return await getattr(self, f"async_step_add_{step_id[:-1]}")()
            if action and action.startswith("edit:"):
                idx = int(action.split(":")[1])
                if 0 <= idx < len(items):
                    self._edit_index = idx
                    return await getattr(self, f"async_step_edit_{step_id[:-1]}")()
            if action and action.startswith("remove:"):
                idx = int(action.split(":")[1])
                if 0 <= idx < len(items):
                    items.pop(idx)
            return self._save(key, items)

        options = [SelectOptionDict(value="add", label=f"Ajouter {noun}")]
        for i, item in enumerate(items):
            options.append(SelectOptionDict(value=f"edit:{i}", label=f"Modifier : {label(item)}"))
            options.append(SelectOptionDict(value=f"remove:{i}", label=f"Supprimer : {label(item)}"))
        options.append(SelectOptionDict(value="done", label="Terminé"))

        return self.async_show_form(
            step_id=step_id,
            description_placeholders={"list": "\n".join(describe(item) for item in items) if items else "Rien de configuré pour l'instant."},
            data_schema=vol.Schema({
                vol.Required("action", default="done"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                ),
            }),
        )

    async def _edit_step(self, step_id: str, key: str, schema, from_input, user_input, adding: bool, validate=None):
        """validate(user_input): the errors of the form, shown again with what was entered."""
        items = self._items(key)
        errors = validate(user_input) if validate and user_input is not None else {}
        if errors:
            return self.async_show_form(step_id=step_id, data_schema=schema(user_input), errors=errors)
        if adding:
            if user_input is not None:
                items.append(from_input(user_input))
                return self._save(key, items)
            return self.async_show_form(step_id=step_id, data_schema=schema())

        idx = self._edit_index
        if idx is None or idx >= len(items):
            return await self.async_step_init()
        if user_input is not None:
            items[idx] = from_input(user_input)
            self._edit_index = None
            return self._save(key, items)
        return self.async_show_form(step_id=step_id, data_schema=schema(items[idx]))

    # ---------- consumers ----------

    async def async_step_consumers(self, user_input=None):
        cat_labels = {"ev_charger": "Borne de recharge", "heat_pump": "PAC", "pool": "Piscine", "hot_water": "Chauffe-eau",
                      "appliance": "Électroménager", "other": "Autres"}

        def describe(c):
            base = f"• {c['name']} [{cat_labels.get(c.get('category', 'other'), 'Autres')}] ({c['entity']})"
            if c.get(CONF_CONSUMER_POWER_ENTITY):
                base += " ⚡ live"
            subs = c.get(CONF_SUBTRACT_ENTITIES, [])
            if subs:
                base += f" − {len(subs)} entité(s)"
            return base

        return await self._list_step("consumers", CONF_CONSUMERS, "un consommateur", lambda c: c["name"], describe, user_input)

    async def async_step_add_consumer(self, user_input=None):
        return await self._edit_step("add_consumer", CONF_CONSUMERS, self._consumer_schema, self._consumer_from_input,
                                     user_input, adding=True)

    async def async_step_edit_consumer(self, user_input=None):
        return await self._edit_step("edit_consumer", CONF_CONSUMERS, self._consumer_schema, self._consumer_from_input,
                                     user_input, adding=False)

    # ---------- charging stations ----------

    async def async_step_chargers(self, user_input=None):
        def describe(c):
            brand = CHARGER_BRANDS.get(c.get(CONF_CHARGER_BRAND, "other"))
            base = f"• {c[CONF_NAME]}" + (f" [{brand}]" if brand and c.get(CONF_CHARGER_BRAND, "other") != "other" else "")
            base += f" ({c[CONF_CHARGER_SWITCH_ENTITY]}, {c[CONF_CHARGER_CURRENT_ENTITY]})"
            if c.get(CONF_CHARGER_PLUGGED_ENTITY):
                base += " 🔌"
            if c.get(CONF_CHARGER_POWER_ENTITY):
                base += " ⚡"
            if c.get(CONF_CHARGER_ENERGY_ENTITY):
                base += " kWh"
            if c.get(CONF_CHARGER_PHASES):
                phases = {PHASES_SWITCHABLE: "Mono/Tri", PHASES_MONO: "Mono", PHASES_TRI: "Triphasé"}[c[CONF_CHARGER_PHASES]]
                base += f" · {phases} {c[CONF_CHARGER_MIN_CURRENT]}–{c[CONF_CHARGER_MAX_CURRENT]} A"
            return base

        return await self._list_step("chargers", CONF_CHARGERS, "une borne", lambda c: c[CONF_NAME], describe, user_input)

    async def async_step_add_charger(self, user_input=None):
        return await self._edit_step("add_charger", CONF_CHARGERS, self._charger_schema, self._charger_from_input,
                                     user_input, adding=True, validate=self._charger_errors)

    async def async_step_edit_charger(self, user_input=None):
        return await self._edit_step("edit_charger", CONF_CHARGERS, self._charger_schema, self._charger_from_input,
                                     user_input, adding=False, validate=self._charger_errors)

    @staticmethod
    def _charger_errors(user_input: dict) -> dict:
        if user_input[CONF_CHARGER_MIN_CURRENT] > user_input[CONF_CHARGER_MAX_CURRENT]:
            return {CONF_CHARGER_MAX_CURRENT: "current_range"}
        return {}

    @staticmethod
    def _charger_from_input(user_input: dict) -> dict:
        charger = {
            CONF_NAME: user_input[CONF_NAME].strip(),
            CONF_CHARGER_BRAND: user_input.get(CONF_CHARGER_BRAND, "other"),
            CONF_CHARGER_SWITCH_ENTITY: user_input[CONF_CHARGER_SWITCH_ENTITY],
            CONF_CHARGER_CURRENT_ENTITY: user_input[CONF_CHARGER_CURRENT_ENTITY],
            CONF_CHARGER_PHASES: user_input[CONF_CHARGER_PHASES],
            CONF_CHARGER_MIN_CURRENT: int(user_input[CONF_CHARGER_MIN_CURRENT]),
            CONF_CHARGER_MAX_CURRENT: int(user_input[CONF_CHARGER_MAX_CURRENT]),
        }
        for key in (CONF_CHARGER_PLUGGED_ENTITY, CONF_CHARGER_POWER_ENTITY, CONF_CHARGER_ENERGY_ENTITY):
            if user_input.get(key):
                charger[key] = user_input[key]
        return charger

    @staticmethod
    def _charger_schema(defaults=None):
        d = defaults or {}
        schema = {vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): TextSelector(TextSelectorConfig(type="text"))}
        schema[vol.Required(CONF_CHARGER_BRAND, default=d.get(CONF_CHARGER_BRAND, "other"))] = SelectSelector(
            SelectSelectorConfig(options=[SelectOptionDict(value=k, label=v) for k, v in CHARGER_BRANDS.items()],
                                 mode=SelectSelectorMode.DROPDOWN))
        switch = EntitySelector(EntitySelectorConfig(domain=["switch", "input_boolean"]))
        # Easee: its current is read on a sensor (dynamic_charger_limit) and set through its own action
        current = EntitySelector(EntitySelectorConfig(domain=["number", "input_number", "sensor"]))
        for key, selector in ((CONF_CHARGER_SWITCH_ENTITY, switch), (CONF_CHARGER_CURRENT_ENTITY, current)):
            if d.get(key):
                schema[vol.Required(key, default=d[key])] = selector
            else:
                schema[vol.Required(key)] = selector
        # The range it can be controlled in
        schema[vol.Required(CONF_CHARGER_PHASES, default=d.get(CONF_CHARGER_PHASES, PHASES_TRI))] = SelectSelector(
            SelectSelectorConfig(options=[
                SelectOptionDict(value=PHASES_SWITCHABLE, label="Mono/Tri"),
                SelectOptionDict(value=PHASES_MONO, label="Mono"),
                SelectOptionDict(value=PHASES_TRI, label="Triphasé"),
            ], mode=SelectSelectorMode.DROPDOWN))
        amperes = NumberSelector(NumberSelectorConfig(min=1, max=80, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="A"))
        schema[vol.Required(CONF_CHARGER_MIN_CURRENT, default=d.get(CONF_CHARGER_MIN_CURRENT, DEFAULT_MIN_CURRENT))] = amperes
        schema[vol.Required(CONF_CHARGER_MAX_CURRENT, default=d.get(CONF_CHARGER_MAX_CURRENT, DEFAULT_MAX_CURRENT))] = amperes
        _optional_entity(schema, CONF_CHARGER_PLUGGED_ENTITY, d,
                         EntitySelector(EntitySelectorConfig(domain=["binary_sensor", "sensor"])))
        _optional_entity(schema, CONF_CHARGER_POWER_ENTITY, d,
                         EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power")))
        _optional_entity(schema, CONF_CHARGER_ENERGY_ENTITY, d,
                         EntitySelector(EntitySelectorConfig(domain="sensor", device_class="energy")))
        return vol.Schema(schema)

    # ---------- cars ----------

    async def async_step_cars(self, user_input=None):
        def label(c):
            return f"{c[CONF_NAME]} ({c[CONF_CAR_BRAND]})" if c.get(CONF_CAR_BRAND) else c[CONF_NAME]

        def describe(c):
            parts = [label(c)]
            if c.get(CONF_CAR_CAPACITY):
                parts.append(f"{c[CONF_CAR_CAPACITY]:g} kWh")
            if c.get(CONF_CAR_SOC_ENTITY):
                parts.append(f"niveau : {c[CONF_CAR_SOC_ENTITY]}")
            if c.get(CONF_CAR_STATUS_ENTITY):
                parts.append(f"état : {c[CONF_CAR_STATUS_ENTITY]}")
            return "• " + ", ".join(parts)

        return await self._list_step("cars", CONF_CARS, "une voiture", label, describe, user_input)

    async def async_step_add_car(self, user_input=None):
        return await self._edit_step("add_car", CONF_CARS, self._car_schema, self._car_from_input, user_input, adding=True)

    async def async_step_edit_car(self, user_input=None):
        return await self._edit_step("edit_car", CONF_CARS, self._car_schema, self._car_from_input, user_input, adding=False)

    @staticmethod
    def _car_from_input(user_input: dict) -> dict:
        car = {CONF_NAME: user_input[CONF_NAME].strip()}
        brand = (user_input.get(CONF_CAR_BRAND) or "").strip()
        if brand:
            car[CONF_CAR_BRAND] = brand
        for key in (CONF_CAR_SOC_ENTITY, CONF_CAR_STATUS_ENTITY):
            if user_input.get(key):
                car[key] = user_input[key]
        if user_input.get(CONF_CAR_CAPACITY):
            car[CONF_CAR_CAPACITY] = float(user_input[CONF_CAR_CAPACITY])
        if user_input.get(CONF_CAR_CHARGER):
            car[CONF_CAR_CHARGER] = user_input[CONF_CAR_CHARGER]
        return car

    def _car_schema(self, defaults=None):
        d = defaults or {}
        schema = {
            vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): TextSelector(TextSelectorConfig(type="text")),
            vol.Optional(CONF_CAR_BRAND, default=d.get(CONF_CAR_BRAND, "")): TextSelector(TextSelectorConfig(type="text")),
        }
        _optional_entity(schema, CONF_CAR_SOC_ENTITY, d, EntitySelector(EntitySelectorConfig(domain=["sensor", "number"])))
        _optional_entity(schema, CONF_CAR_STATUS_ENTITY, d,
                         EntitySelector(EntitySelectorConfig(domain=["sensor", "binary_sensor", "device_tracker"])))
        capacity = NumberSelector(NumberSelectorConfig(min=1, max=250, step=0.1, mode=NumberSelectorMode.BOX,
                                                       unit_of_measurement="kWh"))
        if d.get(CONF_CAR_CAPACITY):
            schema[vol.Optional(CONF_CAR_CAPACITY, default=d[CONF_CAR_CAPACITY])] = capacity
        else:
            schema[vol.Optional(CONF_CAR_CAPACITY)] = capacity
        # The station it usually charges at, to know which car is plugged in where
        chargers = [c[CONF_NAME] for c in self._items(CONF_CHARGERS)]
        if chargers:
            usual = SelectSelector(SelectSelectorConfig(
                options=[SelectOptionDict(value=name, label=name) for name in chargers], mode=SelectSelectorMode.DROPDOWN))
            if d.get(CONF_CAR_CHARGER) in chargers:
                schema[vol.Optional(CONF_CAR_CHARGER, default=d[CONF_CAR_CHARGER])] = usual
            else:
                schema[vol.Optional(CONF_CAR_CHARGER)] = usual
        return vol.Schema(schema)

    # ---------- consumer schema and conversion ----------

    @staticmethod
    def _consumer_from_input(user_input: dict) -> dict:
        consumer = {
            "entity": user_input["consumer_entity"],
            "name": user_input["consumer_name"].strip(),
            "category": user_input.get("consumer_category", "other"),
        }
        power_entity = user_input.get(CONF_CONSUMER_POWER_ENTITY)
        if power_entity:
            consumer[CONF_CONSUMER_POWER_ENTITY] = power_entity
        subtract = user_input.get(CONF_SUBTRACT_ENTITIES)
        if subtract:
            consumer[CONF_SUBTRACT_ENTITIES] = subtract if isinstance(subtract, list) else [subtract]
        return consumer

    @staticmethod
    def _consumer_schema(defaults=None):
        d = defaults or {}
        schema = {}

        entity = EntitySelector(EntitySelectorConfig(domain="sensor"))
        if d.get("entity"):
            schema[vol.Required("consumer_entity", default=d["entity"])] = entity
        else:
            schema[vol.Required("consumer_entity")] = entity

        schema[vol.Required("consumer_name", default=d.get("name", ""))] = TextSelector(
            TextSelectorConfig(type="text")
        )
        schema[vol.Required("consumer_category", default=d.get("category", "other"))] = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value="ev_charger", label="Borne de recharge"),
                    SelectOptionDict(value="heat_pump", label="Pompe à chaleur (PAC)"),
                    SelectOptionDict(value="pool", label="Piscine"),
                    SelectOptionDict(value="hot_water", label="Chauffe-eau"),
                    SelectOptionDict(value="appliance", label="Électroménager"),
                    SelectOptionDict(value="other", label="Autres"),
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

        power = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power"))
        if d.get(CONF_CONSUMER_POWER_ENTITY):
            schema[vol.Optional(CONF_CONSUMER_POWER_ENTITY, default=d[CONF_CONSUMER_POWER_ENTITY])] = power
        else:
            schema[vol.Optional(CONF_CONSUMER_POWER_ENTITY)] = power

        subtract = EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True))
        if d.get(CONF_SUBTRACT_ENTITIES):
            schema[vol.Optional(CONF_SUBTRACT_ENTITIES, default=d[CONF_SUBTRACT_ENTITIES])] = subtract
        else:
            schema[vol.Optional(CONF_SUBTRACT_ENTITIES)] = subtract

        return vol.Schema(schema)


class AperHemsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for APER HEMS."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return AperHemsOptionsFlow()

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            for key in _OPTIONAL_KEYS:
                user_input.setdefault(key, "")
            user_input[CONF_API_KEY] = user_input[CONF_API_KEY].strip()
            await self.async_set_unique_id(user_input[CONF_API_KEY])
            self._abort_if_unique_id_configured()
            error = await _check_api_key(self.hass, user_input[CONF_API_KEY])
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(title="APER HEMS", data=user_input)

        return self.async_show_form(step_id="user", data_schema=_user_schema(user_input), errors=errors)

    async def async_step_reconfigure(self, user_input: dict | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            for key in _OPTIONAL_KEYS:
                user_input.setdefault(key, "")
            user_input[CONF_API_KEY] = user_input[CONF_API_KEY].strip()
            error = None
            if user_input[CONF_API_KEY] != entry.data.get(CONF_API_KEY):
                error = await _check_api_key(self.hass, user_input[CONF_API_KEY])
            if error:
                errors["base"] = error
            else:
                data = dict(entry.data)
                data.update(user_input)
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_schema(user_input or dict(entry.data)),
            errors=errors,
        )
