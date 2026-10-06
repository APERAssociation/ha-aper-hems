"""APER HEMS: sends the member's measurements to aper-association.ch, per quarter of an hour.

The server stores them in the "Datas" table (production, grid, battery, consumption)
and in the "CourbesCharge" table (consumers chosen in the options).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    BACKFILL_DAYS,
    BACKFILL_INTERVAL_HOURS,
    BACKFILL_MAX_RETRIES,
    CONF_ADD_BATTERY_ENTITY,
    CONF_API_KEY,
    CONF_BATTERY_CHARGE_POSITIVE,
    CONF_BATTERY_ENTITY,
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
    CONF_SOC_ENTITY,
    CONF_UPDATE_INTERVAL,
    CONSUMER_NAME_LENGTH,
    COVERAGE_API_URL,
    DATA_API_URL,
    DOMAIN,
    SLOT_FORMAT,
)

_LOGGER = logging.getLogger(__name__)

BUCKET_MINUTES = 15

AperHemsConfigEntry = ConfigEntry


def _bucket_start(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // BUCKET_MINUTES) * BUCKET_MINUTES, second=0, microsecond=0)


def _aggregate_15min(states: list, period_start: datetime, period_end: datetime) -> list[dict]:
    """Time-weighted average of a power sensor per 15-min bucket."""
    samples = []
    for state in states:
        if state.state in ("unavailable", "unknown"):
            continue
        try:
            value = float(state.state)
        except ValueError:
            continue
        samples.append((state.last_updated, value))

    if not samples:
        return []

    samples.sort(key=lambda s: s[0])

    buckets = {}
    bucket_dt = _bucket_start(period_start)
    while bucket_dt < period_end:
        buckets[bucket_dt] = {"weighted_sum": 0.0, "total_seconds": 0.0}
        bucket_dt += timedelta(minutes=BUCKET_MINUTES)

    for bucket_dt in sorted(buckets.keys()):
        bucket_end = bucket_dt + timedelta(minutes=BUCKET_MINUTES)

        relevant = []
        last_before = None
        for ts, val in samples:
            if ts < bucket_dt:
                last_before = val
            elif ts < bucket_end:
                relevant.append((ts, val))

        if not relevant and last_before is None:
            del buckets[bucket_dt]
            continue

        current_val = last_before if last_before is not None else relevant[0][1]
        cursor = bucket_dt

        for ts, val in relevant:
            dt_seconds = (ts - cursor).total_seconds()
            if dt_seconds > 0:
                buckets[bucket_dt]["weighted_sum"] += current_val * dt_seconds
                buckets[bucket_dt]["total_seconds"] += dt_seconds
            current_val = val
            cursor = ts

        dt_seconds = (bucket_end - cursor).total_seconds()
        if dt_seconds > 0:
            buckets[bucket_dt]["weighted_sum"] += current_val * dt_seconds
            buckets[bucket_dt]["total_seconds"] += dt_seconds

    result = []
    for bucket_dt in sorted(buckets.keys()):
        b = buckets[bucket_dt]
        if b["total_seconds"] > 0:
            result.append({
                "timestamp": bucket_dt.isoformat(),
                "value": round(b["weighted_sum"] / b["total_seconds"], 1),
            })

    return result


def _delta_15min(states: list, period_start: datetime, period_end: datetime) -> list[dict]:
    """Energy deltas per 15-min bucket for cumulative counters (total_increasing)."""
    samples = []
    for state in states:
        if state.state in ("unavailable", "unknown"):
            continue
        try:
            value = float(state.state)
        except ValueError:
            continue
        if value < 0.001:
            continue
        samples.append((state.last_updated, value))

    if not samples:
        return []

    samples.sort(key=lambda s: s[0])

    def value_at(t):
        v = None
        for ts, val in samples:
            if ts <= t:
                v = val
            else:
                break
        return v

    result = []
    bucket_dt = _bucket_start(period_start)
    while bucket_dt < period_end:
        bucket_end = bucket_dt + timedelta(minutes=BUCKET_MINUTES)
        v_start = value_at(bucket_dt)
        v_end = value_at(bucket_end)
        if v_start is not None and v_end is not None:
            result.append({
                "timestamp": bucket_dt.isoformat(),
                "value": round(max(v_end - v_start, 0), 5),
            })
        bucket_dt += timedelta(minutes=BUCKET_MINUTES)

    return result


def _split_signed(history: list[dict]) -> tuple[list[dict], list[dict]]:
    """Signed power (+ import / - export) into two positive series."""
    positive = []
    negative = []
    for point in history:
        ts = point["timestamp"]
        val = point["value"]
        positive.append({"timestamp": ts, "value": max(val, 0.0)})
        negative.append({"timestamp": ts, "value": abs(min(val, 0.0))})
    return positive, negative


async def _get_history(hass, start, end, entity_id, use_delta=False):
    query_start = start - timedelta(minutes=BUCKET_MINUTES) if use_delta else start
    states = await get_instance(hass).async_add_executor_job(
        state_changes_during_period,
        hass,
        query_start,
        end,
        entity_id,
    )
    raw = states.get(entity_id, [])
    if use_delta:
        return _delta_15min(raw, start, end)
    return _aggregate_15min(raw, start, end)


def _unit(hass, entity_id, default=""):
    state = hass.states.get(entity_id) if entity_id else None
    if state is None:
        return default
    return state.attributes.get("unit_of_measurement", "") or default


def _consumer_name(name: str) -> str:
    """Same rule as the server: trimmed, 100 characters at most."""
    return (name or "").strip()[:CONSUMER_NAME_LENGTH]


def _detect_counter(hass, entity_id, name):
    """Detect if entity is a cumulative counter. Result is cached in hass.data."""
    cache = hass.data.setdefault(DOMAIN, {}).setdefault("counter_cache", {})

    c_state = hass.states.get(entity_id)
    if not c_state:
        if cache.get(entity_id):
            _LOGGER.warning("Consumer %s: entity unavailable, using cached counter=True", name)
            return True, "kWh"
        return None, ""

    sc = c_state.attributes.get("state_class", "")
    dc = c_state.attributes.get("device_class", "")
    unit = c_state.attributes.get("unit_of_measurement", "") or ""

    if not unit and not sc and not dc:
        if cache.get(entity_id):
            _LOGGER.warning("Consumer %s: attributes empty, using cached counter=True", name)
            return True, "kWh"
        _LOGGER.warning("Consumer %s: attributes not loaded, skipping", name)
        return None, unit

    is_counter = (
        sc in ("total_increasing", "total")
        or dc == "energy"
        or unit.lower().strip() in ("kwh", "wh")
    )

    if not is_counter and cache.get(entity_id):
        _LOGGER.warning(
            "Consumer %s: attributes say not counter (sc=%r dc=%r unit=%r) but cache says counter, forcing delta",
            name, sc, dc, unit,
        )
        is_counter = True

    if is_counter:
        cache[entity_id] = True

    return is_counter, unit


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: AperHemsConfigEntry) -> bool:
    data = entry.data
    api_key = data[CONF_API_KEY]
    grid_entity = data[CONF_GRID_ENTITY]
    production_entity = data.get(CONF_PRODUCTION_ENTITY, "")
    consumption_entity = data.get(CONF_CONSUMPTION_ENTITY, "")
    battery_entity = data.get(CONF_BATTERY_ENTITY, "")
    add_battery_entity = data.get(CONF_ADD_BATTERY_ENTITY, "")
    out_battery_entity = data.get(CONF_OUT_BATTERY_ENTITY, "")
    soc_entity = data.get(CONF_SOC_ENTITY, "")
    grid_import_positive = data.get(CONF_GRID_IMPORT_POSITIVE, True)
    battery_charge_positive = data.get(CONF_BATTERY_CHARGE_POSITIVE, True)
    meter_production = data.get(CONF_METER_PRODUCTION, "")
    meter_import = data.get(CONF_METER_IMPORT, "")
    meter_export = data.get(CONF_METER_EXPORT, "")
    meter_battery_charge = data.get(CONF_METER_BATTERY_CHARGE, "")
    meter_battery_discharge = data.get(CONF_METER_BATTERY_DISCHARGE, "")
    interval_td = timedelta(minutes=int(data[CONF_UPDATE_INTERVAL]))

    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {api_key}"}

    async def _collect(start: datetime, end: datetime) -> dict:
        """Measurements between two UTC dates, per quarter of an hour, in the format of /api/energydata."""
        # Grid: energy counters if configured, otherwise the signed power sensor
        import_history, export_history, grid_unit = [], [], ""
        if meter_import and meter_export:
            import_history = await _get_history(hass, start, end, meter_import, use_delta=True)
            export_history = await _get_history(hass, start, end, meter_export, use_delta=True)
            grid_unit = _unit(hass, meter_import, "kWh")
        if not import_history and not export_history:
            grid_history = await _get_history(hass, start, end, grid_entity)
            if not grid_import_positive:
                for pt in grid_history:
                    pt["value"] = -pt["value"]
            import_history, export_history = _split_signed(grid_history)
            grid_unit = _unit(hass, grid_entity)

        prod_history, prod_unit = [], ""
        if meter_production:
            prod_history = await _get_history(hass, start, end, meter_production, use_delta=True)
            prod_unit = _unit(hass, meter_production, "kWh")
        if not prod_history and production_entity:
            prod_history = await _get_history(hass, start, end, production_entity)
            prod_unit = _unit(hass, production_entity)

        # Battery: counters, then separate charge/discharge sensors, then a single signed sensor
        add_history, out_history, bat_unit = [], [], ""
        if meter_battery_charge and meter_battery_discharge:
            charge = await _get_history(hass, start, end, meter_battery_charge, use_delta=True)
            discharge = await _get_history(hass, start, end, meter_battery_discharge, use_delta=True)
            add_history, out_history = (charge, discharge) if battery_charge_positive else (discharge, charge)
            bat_unit = _unit(hass, meter_battery_charge, "kWh")
        if not add_history and not out_history:
            if add_battery_entity:
                add_history = await _get_history(hass, start, end, add_battery_entity)
                bat_unit = _unit(hass, add_battery_entity)
            if out_battery_entity:
                out_history = await _get_history(hass, start, end, out_battery_entity)
                bat_unit = bat_unit or _unit(hass, out_battery_entity)
        if not add_history and not out_history and battery_entity:
            bat_history = await _get_history(hass, start, end, battery_entity)
            if not battery_charge_positive:
                for pt in bat_history:
                    pt["value"] = -pt["value"]
            add_history, out_history = _split_signed(bat_history)
            bat_unit = _unit(hass, battery_entity)

        soc_history = await _get_history(hass, start, end, soc_entity) if soc_entity else []

        conso_history, conso_unit = [], ""
        if consumption_entity:
            conso_history = await _get_history(hass, start, end, consumption_entity)
            conso_unit = _unit(hass, consumption_entity)

        payload = {
            "productionUnit": prod_unit,
            "productionHistory": prod_history,
            "consumptionUnit": conso_unit,
            "consumptionHistory": conso_history,
            "importUnit": grid_unit,
            "importHistory": import_history,
            "exportUnit": grid_unit,
            "exportHistory": export_history,
            "addBatteryUnit": bat_unit,
            "addBatteryHistory": add_history,
            "outBatteryUnit": bat_unit,
            "outBatteryHistory": out_history,
            "socHistory": soc_history,
            "consumers": [],
        }

        for consumer in entry.options.get(CONF_CONSUMERS, []):
            entity_id = consumer["entity"]
            name = consumer["name"]
            is_counter, unit = _detect_counter(hass, entity_id, name)
            if is_counter is None:
                continue
            history = await _get_history(hass, start, end, entity_id, use_delta=is_counter)
            _LOGGER.debug("Consumer %s: unit=%r counter=%s points=%d", name, unit, is_counter, len(history))
            payload["consumers"].append({
                "name": name,
                "unit": unit,
                "category": consumer.get("category", "other"),
                "history": history,
            })

        return payload

    async def _post(payload: dict) -> bool:
        try:
            async with session.post(
                DATA_API_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status == 200:
                    return True
                if resp.status == 401:
                    _LOGGER.error("APER: clé API refusée, créez-en une nouvelle depuis l'administration du site")
                else:
                    _LOGGER.error("APER API error %s: %s", resp.status, await resp.text())
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("APER: envoi impossible: %s", err)
        return False

    async def _send_data(_now=None):
        if not hass.states.get(grid_entity):
            _LOGGER.warning("Grid entity not available")
            return

        now = dt_util.utcnow()
        # The last quarter of an hour is still running: it is sent again, complete, next time
        payload = await _collect(_bucket_start(now - interval_td), now)
        if await _post(payload):
            _LOGGER.debug(
                "History sent: %d prod, %d imp, %d exp, %d consumers",
                len(payload["productionHistory"]), len(payload["importHistory"]),
                len(payload["exportHistory"]), len(payload["consumers"]),
            )

    async def _backfill(_now=None):
        """Fill the gaps of the last days from the Home Assistant recorder."""
        failures = hass.data.setdefault(DOMAIN, {}).setdefault("backfill_failures", {})

        now = dt_util.utcnow()
        start = _bucket_start(now - timedelta(days=BACKFILL_DAYS))
        current = _bucket_start(now)

        try:
            async with session.get(
                COVERAGE_API_URL,
                params={"from": start.strftime(SLOT_FORMAT), "to": current.strftime(SLOT_FORMAT)},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Coverage API error: %s", resp.status)
                    return
                coverage = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Coverage API unreachable: %s", err)
            return

        expected = set()
        bucket = start
        while bucket < current:
            expected.add(bucket.strftime(SLOT_FORMAT))
            bucket += timedelta(minutes=BUCKET_MINUTES)

        missing = expected - set(coverage.get("slots", []))

        # The server compares consumer names without case
        consumer_existing = {
            name.lower(): set(slots) for name, slots in coverage.get("consumerSlots", {}).items()
        }
        for consumer in entry.options.get(CONF_CONSUMERS, []):
            c_name = _consumer_name(consumer["name"])
            c_missing = expected - consumer_existing.get(c_name.lower(), set())
            if c_missing:
                _LOGGER.info("Backfill: consumer '%s' misses %d slots", c_name, len(c_missing))
            missing |= c_missing

        # A quarter of an hour that the recorder cannot fill is only tried a few times
        actionable = sorted(s for s in missing if failures.get(s, 0) < BACKFILL_MAX_RETRIES)
        if not actionable:
            _LOGGER.debug("Backfill: nothing to send")
            return

        _LOGGER.info("Backfill: %d gaps, %d to send", len(missing), len(actionable))

        # Consecutive quarters of an hour grouped into ranges of 24 h at most
        ranges = []
        for slot in actionable:
            slot_dt = datetime.strptime(slot, SLOT_FORMAT).replace(tzinfo=dt_util.UTC)
            slot_end = slot_dt + timedelta(minutes=BUCKET_MINUTES)
            if ranges and ranges[-1][1] == slot_dt and slot_end - ranges[-1][0] <= timedelta(hours=24):
                ranges[-1][1] = slot_end
            else:
                ranges.append([slot_dt, slot_end])

        for idx, (r_start, r_end) in enumerate(ranges):
            if idx > 0:
                await asyncio.sleep(5)

            slot = r_start
            while slot < r_end:
                key = slot.strftime(SLOT_FORMAT)
                failures[key] = failures.get(key, 0) + 1
                slot += timedelta(minutes=BUCKET_MINUTES)

            try:
                payload = await _collect(r_start, r_end)
                if not payload["importHistory"] and not payload["exportHistory"]:
                    _LOGGER.info("Backfill: no recorder data for %s -> %s", r_start, r_end)
                    continue
                if await _post(payload):
                    _LOGGER.info("Backfill OK: %s -> %s", r_start, r_end)
            except Exception as err:  # noqa: BLE001 - one bad range must not stop the others
                _LOGGER.error("Backfill error %s -> %s: %s", r_start, r_end, err)

    entry.async_on_unload(async_track_time_interval(hass, _send_data, interval_td))
    entry.async_on_unload(async_track_time_interval(hass, _backfill, timedelta(hours=BACKFILL_INTERVAL_HOURS)))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async def _delayed_start():
        await asyncio.sleep(60)
        await _send_data()
        await _backfill()

    entry.async_create_background_task(hass, _delayed_start(), f"{DOMAIN}_start")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: AperHemsConfigEntry) -> bool:
    return True
