from __future__ import annotations

import asyncio
import logging
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
    BAT_IN_HINTS,
    BAT_OUT_HINTS,
    BRAND_KEYWORDS,
    CONF_ADD_BATTERY_ENTITY,
    CONF_API_KEY,
    CONF_BATTERY_CHARGE_POSITIVE,
    CONF_BATTERY_ENTITY,
    CONF_BRANDS,
    CONF_CONSUMERS,
    CONF_CONSUMPTION_ENTITY,
    CONF_GRID_ENTITY,
    CONF_GRID_IMPORT_POSITIVE,
    CONF_METER_BATTERY_CHARGE,
    CONF_METER_BATTERY_DISCHARGE,
    CONF_METER_EXPORT,
    CONF_METER_IMPORT,
    CONF_METER_PRODUCTION,
    CONF_OUT_BATTERY_ENTITY,
    CONF_PRODUCTION_ENTITY,
    CONF_PROFILE,
    CONF_SOC_ENTITY,
    CONF_UPDATE_INTERVAL,
    CONSO_HINTS,
    COVERAGE_API_URL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    GRID_HINTS,
    PROD_HINTS,
    PROFILE_CONSUMPTION,
    PROFILE_SOLAR,
    PROFILE_SOLAR_BATTERY,
    SLOT_FORMAT,
    SOC_HINTS,
)

_LOGGER = logging.getLogger(__name__)

_ENERGY_DEVICE_CLASSES = ["power", "energy"]

# Optional entities, saved empty when left blank
_OPTIONAL_KEYS = (
    CONF_PRODUCTION_ENTITY, CONF_CONSUMPTION_ENTITY, CONF_ADD_BATTERY_ENTITY, CONF_OUT_BATTERY_ENTITY,
    CONF_SOC_ENTITY, CONF_BATTERY_ENTITY, CONF_METER_PRODUCTION, CONF_METER_IMPORT, CONF_METER_EXPORT,
    CONF_METER_BATTERY_CHARGE, CONF_METER_BATTERY_DISCHARGE,
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
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return "cannot_connect"
    return None


def _match_hints(entity_id: str, hints: list[str]) -> bool:
    eid = entity_id.lower()
    return any(h in eid for h in hints)


def _detect_entities(hass, brands: list[str]) -> dict[str, str]:
    """Scan all HA sensors and find the best matches."""
    brand_kws = []
    for brand in brands:
        brand_kws.extend(BRAND_KEYWORDS.get(brand, [brand]))

    candidates: dict[str, list[str]] = {}

    for state in hass.states.async_all("sensor"):
        eid = state.entity_id.lower()
        attrs = state.attributes
        dc = attrs.get("device_class", "") or ""
        unit = (attrs.get("unit_of_measurement") or "").lower()

        if not any(kw in eid for kw in brand_kws):
            continue

        is_power = dc in ("power", "energy") or unit in ("w", "kw", "wh", "kwh")

        if is_power and _match_hints(eid, GRID_HINTS):
            candidates.setdefault("grid", []).append(state.entity_id)
        if is_power and _match_hints(eid, PROD_HINTS):
            candidates.setdefault("production", []).append(state.entity_id)
        if is_power and _match_hints(eid, CONSO_HINTS):
            candidates.setdefault("consumption", []).append(state.entity_id)
        if is_power and _match_hints(eid, BAT_IN_HINTS):
            candidates.setdefault("battery_in", []).append(state.entity_id)
        if is_power and _match_hints(eid, BAT_OUT_HINTS):
            candidates.setdefault("battery_out", []).append(state.entity_id)
        if dc == "battery" or _match_hints(eid, SOC_HINTS):
            candidates.setdefault("soc", []).append(state.entity_id)

    return {role: eids[0] for role, eids in candidates.items() if eids}


def _entity_selector(device_classes=None):
    return EntitySelector(EntitySelectorConfig(
        domain="sensor",
        device_class=device_classes or _ENERGY_DEVICE_CLASSES,
    ))


def _interval_selector():
    return NumberSelector(NumberSelectorConfig(
        min=5, max=1440, step=5,
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="min",
    ))


def _entity_schema(profile: str, defaults: dict | None = None) -> vol.Schema:
    """Entities to choose, depending on the kind of installation."""
    d = defaults or {}
    schema: dict = {}

    schema[vol.Required(CONF_GRID_ENTITY, default=d.get("grid"))] = _entity_selector()

    if profile in (PROFILE_SOLAR_BATTERY, PROFILE_SOLAR):
        schema[vol.Required(CONF_PRODUCTION_ENTITY, default=d.get("production"))] = _entity_selector()
        schema[vol.Optional(
            CONF_CONSUMPTION_ENTITY,
            description={"suggested_value": d.get("consumption", "")},
        )] = _entity_selector()

    if profile == PROFILE_SOLAR_BATTERY:
        schema[vol.Optional(
            CONF_ADD_BATTERY_ENTITY,
            description={"suggested_value": d.get("battery_in", "")},
        )] = _entity_selector()
        schema[vol.Optional(
            CONF_OUT_BATTERY_ENTITY,
            description={"suggested_value": d.get("battery_out", "")},
        )] = _entity_selector()
        schema[vol.Optional(
            CONF_SOC_ENTITY,
            description={"suggested_value": d.get("soc", "")},
        )] = EntitySelector(EntitySelectorConfig(domain="sensor"))

    return vol.Schema(schema)


class AperHemsOptionsFlow(OptionsFlow):
    """Consumers sent to the "CourbesCharge" table."""

    async def async_step_init(self, user_input=None):
        consumers = list(self.config_entry.options.get(CONF_CONSUMERS, []))
        if user_input is not None:
            action = user_input.get("action")
            if action == "add":
                return await self.async_step_add_consumer()
            if action and action.startswith("remove:"):
                idx = int(action.split(":")[1])
                if 0 <= idx < len(consumers):
                    consumers.pop(idx)
            return self.async_create_entry(data={CONF_CONSUMERS: consumers})

        options = [SelectOptionDict(value="add", label="Ajouter un consommateur")]
        for i, c in enumerate(consumers):
            options.append(SelectOptionDict(value=f"remove:{i}", label=f"Supprimer : {c['name']}"))
        options.append(SelectOptionDict(value="done", label="Terminé"))

        cat_labels = {"ev_charger": "Borne VE", "heat_pump": "PAC", "pool": "Piscine", "hot_water": "Chauffe-eau",
                      "appliance": "Électroménager", "other": "Autres"}
        desc = "Aucun consommateur configuré."
        if consumers:
            lines = [f"• {c['name']} [{cat_labels.get(c.get('category', 'other'), 'Autres')}] ({c['entity']})"
                     for c in consumers]
            desc = "Consommateurs actuels :\n" + "\n".join(lines)

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
            consumers.append({
                "entity": user_input["consumer_entity"],
                "name": user_input["consumer_name"].strip(),
                "category": user_input.get("consumer_category", "other"),
            })
            return self.async_create_entry(data={CONF_CONSUMERS: consumers})

        return self.async_show_form(
            step_id="add_consumer",
            data_schema=vol.Schema({
                vol.Required("consumer_entity"): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Required("consumer_name"): TextSelector(TextSelectorConfig(type="text")),
                vol.Required("consumer_category", default="other"): SelectSelector(
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
                ),
            }),
        )


class AperHemsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for APER HEMS."""

    VERSION = 1

    def __init__(self):
        self._data: dict = {}

    @staticmethod
    def async_get_options_flow(config_entry):
        return AperHemsOptionsFlow()

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Step 1: kind of installation."""
        if user_input is not None:
            self._data[CONF_PROFILE] = user_input[CONF_PROFILE]
            return await self.async_step_equipment()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_PROFILE): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=PROFILE_SOLAR_BATTERY, label="Solaire + Batterie"),
                            SelectOptionDict(value=PROFILE_SOLAR, label="Solaire uniquement"),
                            SelectOptionDict(value=PROFILE_CONSUMPTION,
                                             label="Consommation uniquement (pas de panneaux)"),
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    async def async_step_equipment(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Step 2: equipment, used to find the sensors."""
        if user_input is not None:
            self._data[CONF_BRANDS] = user_input[CONF_BRANDS]
            return await self.async_step_entities()

        profile = self._data[CONF_PROFILE]

        options = [
            SelectOptionDict(value="whatwatt", label="WhatWatt Go (compteur GRD)"),
            SelectOptionDict(value="shelly", label="Shelly EM / Pro"),
        ]
        if profile in (PROFILE_SOLAR_BATTERY, PROFILE_SOLAR):
            options.extend([
                SelectOptionDict(value="fronius", label="Fronius"),
                SelectOptionDict(value="solaredge", label="SolarEdge"),
                SelectOptionDict(value="huawei", label="Huawei FusionSolar"),
                SelectOptionDict(value="enphase", label="Enphase"),
                SelectOptionDict(value="sma", label="SMA"),
                SelectOptionDict(value="kostal", label="Kostal"),
                SelectOptionDict(value="senec", label="SENEC"),
            ])
        if profile == PROFILE_SOLAR_BATTERY:
            options.append(SelectOptionDict(value="mystrom", label="myStrom (prises)"))
        options.append(SelectOptionDict(value="other", label="Autre matériel"))

        return self.async_show_form(
            step_id="equipment",
            data_schema=vol.Schema({
                vol.Required(CONF_BRANDS): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST, multiple=True)
                ),
            }),
        )

    async def async_step_entities(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Step 3: sensors, detected automatically."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_api()

        detected = _detect_entities(self.hass, self._data.get(CONF_BRANDS, []))
        if detected:
            _LOGGER.info("Auto-detection: %s", detected)

        return self.async_show_form(
            step_id="entities",
            data_schema=_entity_schema(self._data[CONF_PROFILE], detected),
        )

    async def async_step_api(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Step 4: API key created on aper-association.ch, and sending interval."""
        errors = {}
        if user_input is not None:
            user_input[CONF_API_KEY] = user_input[CONF_API_KEY].strip()
            error = await _check_api_key(self.hass, user_input[CONF_API_KEY])
            if error:
                errors["base"] = error
            else:
                self._data.update(user_input)
                for key in _OPTIONAL_KEYS:
                    self._data.setdefault(key, "")
                self._data.setdefault(CONF_GRID_IMPORT_POSITIVE, True)
                self._data.setdefault(CONF_BATTERY_CHARGE_POSITIVE, True)

                await self.async_set_unique_id(self._data[CONF_API_KEY])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(title="APER HEMS", data=self._data)

        return self.async_show_form(
            step_id="api",
            errors=errors,
            data_schema=vol.Schema({
                vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type="password")),
                vol.Required(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): _interval_selector(),
            }),
        )

    async def async_step_reconfigure(self, user_input: dict | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        existing = dict(entry.data)
        errors = {}

        if user_input is not None:
            for key in _OPTIONAL_KEYS:
                user_input.setdefault(key, "")
            user_input[CONF_API_KEY] = user_input[CONF_API_KEY].strip()
            error = None
            if user_input[CONF_API_KEY] != existing.get(CONF_API_KEY):
                error = await _check_api_key(self.hass, user_input[CONF_API_KEY])
            if error:
                errors["base"] = error
            else:
                existing.update(user_input)
                return self.async_update_reload_and_abort(entry, data=existing)

        def suggested(key):
            return {"suggested_value": existing.get(key, "")}

        meter_selector = EntitySelector(EntitySelectorConfig(domain="sensor", unit_of_measurement=["Wh", "kWh"]))

        schema = {
            vol.Required(CONF_GRID_ENTITY, default=existing.get(CONF_GRID_ENTITY)): _entity_selector(),
            vol.Required(CONF_GRID_IMPORT_POSITIVE,
                         default=existing.get(CONF_GRID_IMPORT_POSITIVE, True)): BooleanSelector(),
            vol.Optional(CONF_PRODUCTION_ENTITY, description=suggested(CONF_PRODUCTION_ENTITY)): _entity_selector(),
            vol.Optional(CONF_CONSUMPTION_ENTITY, description=suggested(CONF_CONSUMPTION_ENTITY)): _entity_selector(),
            vol.Optional(CONF_BATTERY_ENTITY, description=suggested(CONF_BATTERY_ENTITY)): _entity_selector(["power"]),
            vol.Optional(CONF_ADD_BATTERY_ENTITY, description=suggested(CONF_ADD_BATTERY_ENTITY)): _entity_selector(),
            vol.Optional(CONF_OUT_BATTERY_ENTITY, description=suggested(CONF_OUT_BATTERY_ENTITY)): _entity_selector(),
            vol.Optional(CONF_SOC_ENTITY, description=suggested(CONF_SOC_ENTITY)):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Required(CONF_BATTERY_CHARGE_POSITIVE,
                         default=existing.get(CONF_BATTERY_CHARGE_POSITIVE, True)): BooleanSelector(),
            vol.Optional(CONF_METER_PRODUCTION, description=suggested(CONF_METER_PRODUCTION)): meter_selector,
            vol.Optional(CONF_METER_IMPORT, description=suggested(CONF_METER_IMPORT)): meter_selector,
            vol.Optional(CONF_METER_EXPORT, description=suggested(CONF_METER_EXPORT)): meter_selector,
            vol.Optional(CONF_METER_BATTERY_CHARGE, description=suggested(CONF_METER_BATTERY_CHARGE)): meter_selector,
            vol.Optional(CONF_METER_BATTERY_DISCHARGE,
                         description=suggested(CONF_METER_BATTERY_DISCHARGE)): meter_selector,
            vol.Required(CONF_UPDATE_INTERVAL,
                         default=existing.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)): _interval_selector(),
            vol.Required(CONF_API_KEY, default=existing.get(CONF_API_KEY)):
                TextSelector(TextSelectorConfig(type="password")),
        }

        return self.async_show_form(step_id="reconfigure", errors=errors, data_schema=vol.Schema(schema))
