from __future__ import annotations

import asyncio
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
    CONF_BATTERY_CHARGE_POSITIVE,
    CONF_BATTERY_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CONSUMER_POWER_ENTITY,
    CONF_CONSUMERS,
    CONF_GRID_ENTITY,
    CONF_GRID_IMPORT_POSITIVE,
    CONF_METER_BATTERY_CHARGE,
    CONF_METER_BATTERY_DISCHARGE,
    CONF_METER_EXPORT,
    CONF_METER_IMPORT,
    CONF_METER_PRODUCTION,
    CONF_PRODUCTION_ENTITY,
    CONF_SUBTRACT_ENTITIES,
    CONF_UPDATE_INTERVAL,
    COVERAGE_API_URL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
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
    """Consumers sent to the "CourbesCharge" table."""

    def __init__(self):
        self._edit_index: int | None = None

    async def async_step_init(self, user_input=None):
        consumers = list(self.config_entry.options.get(CONF_CONSUMERS, []))
        if user_input is not None:
            action = user_input.get("action")
            if action == "add":
                return await self.async_step_add_consumer()
            if action and action.startswith("edit:"):
                idx = int(action.split(":")[1])
                if 0 <= idx < len(consumers):
                    self._edit_index = idx
                    return await self.async_step_edit_consumer()
            if action and action.startswith("remove:"):
                idx = int(action.split(":")[1])
                if 0 <= idx < len(consumers):
                    consumers.pop(idx)
            return self.async_create_entry(data={CONF_CONSUMERS: consumers})

        options = [SelectOptionDict(value="add", label="Ajouter un consommateur")]
        for i, c in enumerate(consumers):
            options.append(SelectOptionDict(value=f"edit:{i}", label=f"Modifier : {c['name']}"))
            options.append(SelectOptionDict(value=f"remove:{i}", label=f"Supprimer : {c['name']}"))
        options.append(SelectOptionDict(value="done", label="Terminé"))

        cat_labels = {"ev_charger": "Borne de recharge", "heat_pump": "PAC", "pool": "Piscine", "hot_water": "Chauffe-eau",
                      "appliance": "Électroménager", "other": "Autres"}
        desc = "Aucun consommateur configuré."
        if consumers:
            def _fmt(c):
                base = f"• {c['name']} [{cat_labels.get(c.get('category', 'other'), 'Autres')}] ({c['entity']})"
                if c.get(CONF_CONSUMER_POWER_ENTITY):
                    base += " ⚡ live"
                subs = c.get(CONF_SUBTRACT_ENTITIES, [])
                if subs:
                    base += f" − {len(subs)} entité(s)"
                return base
            desc = "Consommateurs actuels :\n" + "\n".join(_fmt(c) for c in consumers)

        return self.async_show_form(
            step_id="init",
            description_placeholders={"consumers_list": desc},
            data_schema=vol.Schema({
                vol.Required("action", default="done"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                ),
            }),
        )

    async def async_step_add_consumer(self, user_input=None):
        if user_input is not None:
            consumers = list(self.config_entry.options.get(CONF_CONSUMERS, []))
            consumers.append(self._consumer_from_input(user_input))
            return self.async_create_entry(data={CONF_CONSUMERS: consumers})

        return self.async_show_form(step_id="add_consumer", data_schema=self._consumer_schema())

    async def async_step_edit_consumer(self, user_input=None):
        consumers = list(self.config_entry.options.get(CONF_CONSUMERS, []))
        idx = self._edit_index
        if idx is None or idx >= len(consumers):
            return await self.async_step_init()

        if user_input is not None:
            consumers[idx] = self._consumer_from_input(user_input)
            self._edit_index = None
            return self.async_create_entry(data={CONF_CONSUMERS: consumers})

        return self.async_show_form(step_id="edit_consumer", data_schema=self._consumer_schema(consumers[idx]))

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
