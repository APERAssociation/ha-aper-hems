"""APER HEMS: sends the member's measurements to aper-association.ch, per quarter of an hour.

Adapted from ha-pilote-com 1.28.0. The server stores the measurements in the "Datas" table
(production, grid, battery) and the consumers in the "CourbesCharge" table.
Live sending and charging station control are not part of it: the APER site does not handle them.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

import aiohttp

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    API_URL,
    BACKFILL_DAYS,
    BACKFILL_INTERVAL_HOURS,
    BACKFILL_MAX_RETRIES,
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
    CONSUMER_NAME_LENGTH,
    COVERAGE_API_URL,
    DOMAIN,
    LIVE_HEARTBEAT_SECONDS,
    LIVE_RECONNECT_MAX_SECONDS,
    LIVE_RECONNECT_MIN_SECONDS,
    LIVE_WS_URL,
    SLOT_FORMAT,
    WEATHER_API_URL,
    WEATHER_MAX_PAST_DAYS,
    WEATHER_VARIABLES,
)

_LOGGER = logging.getLogger(__name__)

BUCKET_MINUTES = 15

AperHemsConfigEntry = ConfigEntry


def _bucket_start(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // BUCKET_MINUTES) * BUCKET_MINUTES, second=0, microsecond=0)


def _subtract_histories(main_history: list[dict], sub_histories: list[list[dict]]) -> list[dict]:
    """Subtract values of sub_histories from main_history, matching by timestamp."""
    if not sub_histories:
        return main_history
    sub_maps = [{pt["timestamp"]: pt["value"] for pt in sh} for sh in sub_histories]
    result = []
    for pt in main_history:
        val = pt["value"]
        for sm in sub_maps:
            val -= sm.get(pt["timestamp"], 0)
        result.append({"timestamp": pt["timestamp"], "value": round(max(0, val), 4)})
    return result


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
    """Calculate energy deltas per 15-min bucket for cumulative counters (total_increasing)."""
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
    """Signed power into two positive series: (positive part, negative part)."""
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


def _weather_15min(data: dict, period_start: datetime, period_end: datetime) -> list[dict]:
    """Open-Meteo "minutely_15" answer (GMT) -> one point per 15-min bucket.

    Values at the start of the bucket; rain over the hour that ends with the bucket
    (Open-Meteo gives the rain of the 15 minutes BEFORE each time).
    """
    series = data.get("minutely_15") or {}
    index = {t: i for i, t in enumerate(series.get("time") or [])}

    def value(name, i):
        values = series.get(name) or []
        return values[i] if i is not None and i < len(values) else None

    result = []
    bucket_dt = _bucket_start(period_start)
    while bucket_dt < period_end:
        i = index.get(bucket_dt.strftime("%Y-%m-%dT%H:%M"))
        if i is not None:
            rain = None
            for minutes in (-30, -15, 0, 15):
                j = index.get((bucket_dt + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M"))
                mm = value("precipitation", j)
                if mm is not None:
                    rain = (rain or 0) + mm
            result.append({
                "timestamp": bucket_dt.isoformat(),
                "temperature": value("temperature_2m", i),
                "feelsLike": value("apparent_temperature", i),
                "windSpeed": value("wind_speed_10m", i),
                "uvIndex": value("uv_index", i),
                "humidity": value("relative_humidity_2m", i),
                "pressure": value("surface_pressure", i),
                "precipitationLastHour": round(rain, 2) if rain is not None else None,
                "cloudCover": value("cloud_cover", i),
                "weatherCode": int(value("weather_code", i)) if value("weather_code", i) is not None else None,
            })
        bucket_dt += timedelta(minutes=BUCKET_MINUTES)
    return result


def _unit(hass, entity_id, default=""):
    state = hass.states.get(entity_id) if entity_id else None
    if state is None:
        return default
    return state.attributes.get("unit_of_measurement", "") or default


def _consumer_name(name: str) -> str:
    """Same rule as the server: trimmed, 100 characters at most."""
    return (name or "").strip()[:CONSUMER_NAME_LENGTH]


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _detect_counter(hass, entity_id, name):
    """Detect if entity is a cumulative counter. Result is cached in hass.data."""
    cache = hass.data.setdefault(DOMAIN, {}).setdefault("counter_cache", {})

    c_state = hass.states.get(entity_id)
    if not c_state:
        if cache.get(entity_id):
            _LOGGER.debug("Consumer %s: entity unavailable, using cached counter=True", name)
            return True, "", "", ""
        return None, "", "", ""

    sc = c_state.attributes.get("state_class", "")
    dc = c_state.attributes.get("device_class", "")
    unit = c_state.attributes.get("unit_of_measurement", "")

    if not unit and not sc and not dc:
        if cache.get(entity_id):
            _LOGGER.debug("Consumer %s: attributes empty, using cached counter=True", name)
            return True, sc, dc, unit
        _LOGGER.debug("Consumer %s: attributes not loaded, skipping", name)
        return None, sc, dc, unit

    is_counter = (
        sc in ("total_increasing", "total")
        or dc == "energy"
        or unit.lower().strip() in ("kwh", "wh")
    )

    if not is_counter and cache.get(entity_id):
        _LOGGER.warning(
            "Consumer %s: attributes say not counter (sc=%r dc=%r unit=%r) but cache says counter — forcing delta",
            name, sc, dc, unit,
        )
        is_counter = True

    if is_counter:
        cache[entity_id] = True

    return is_counter, sc, dc, unit


async def async_setup_entry(hass: HomeAssistant, entry: AperHemsConfigEntry) -> bool:
    production_entity = entry.data.get(CONF_PRODUCTION_ENTITY, "")
    grid_entity = entry.data[CONF_GRID_ENTITY]
    grid_import_positive = entry.data.get(CONF_GRID_IMPORT_POSITIVE, True)
    battery_entity = entry.data.get(CONF_BATTERY_ENTITY, "")
    battery_soc_entity = entry.data.get(CONF_BATTERY_SOC_ENTITY, "")
    meter_production = entry.data.get(CONF_METER_PRODUCTION, "")
    meter_import = entry.data.get(CONF_METER_IMPORT, "")
    meter_export = entry.data.get(CONF_METER_EXPORT, "")
    meter_battery_charge = entry.data.get(CONF_METER_BATTERY_CHARGE, "")
    meter_battery_discharge = entry.data.get(CONF_METER_BATTERY_DISCHARGE, "")
    battery_charge_positive = entry.data.get(CONF_BATTERY_CHARGE_POSITIVE, True)
    interval_td = timedelta(minutes=int(entry.data[CONF_UPDATE_INTERVAL]))
    api_key = entry.data[CONF_API_KEY]

    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {api_key}"}

    async def _get_weather(start: datetime, end: datetime) -> list[dict]:
        """Weather at the home from Open-Meteo, per quarter of an hour. Without it the measurements are still sent.

        Only the location rounded to about 1 km leaves Home Assistant, and only towards Open-Meteo.
        """
        latitude, longitude = hass.config.latitude, hass.config.longitude
        if latitude is None or longitude is None:
            return []
        start = max(start, dt_util.utcnow() - timedelta(days=WEATHER_MAX_PAST_DAYS))
        if start >= end:
            return []
        params = {
            "latitude": f"{latitude:.2f}",
            "longitude": f"{longitude:.2f}",
            "minutely_15": WEATHER_VARIABLES,
            "timezone": "GMT",
            # One hour earlier for the rain of the last hour
            "start_date": (start - timedelta(hours=1)).strftime("%Y-%m-%d"),
            "end_date": (end + timedelta(minutes=BUCKET_MINUTES)).strftime("%Y-%m-%d"),
        }
        try:
            async with session.get(
                WEATHER_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Open-Meteo error %s", resp.status)
                    return []
                data = await resp.json()
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as err:
            _LOGGER.warning("Open-Meteo unreachable: %s", type(err).__name__)
            return []
        return _weather_15min(data, start, end)

    async def _collect(start: datetime, end: datetime) -> dict:
        """Measurements between two UTC dates, per quarter of an hour, in the format of /api/energydata.

        The server computes the house consumption: production + import - export + battery balance.
        """
        import_history = []
        export_history = []
        grid_unit = ""
        if meter_import and meter_export:
            import_history = await _get_history(hass, start, end, meter_import, use_delta=True)
            export_history = await _get_history(hass, start, end, meter_export, use_delta=True)
            if import_history or export_history:
                grid_unit = _unit(hass, meter_import, "kWh")
        if not import_history and not export_history:
            grid_history = await _get_history(hass, start, end, grid_entity)
            if not grid_import_positive:
                for pt in grid_history:
                    pt["value"] = -pt["value"]
            import_history, export_history = _split_signed(grid_history)
            grid_unit = _unit(hass, grid_entity)

        prod_history = []
        prod_unit = ""
        if meter_production:
            prod_history = await _get_history(hass, start, end, meter_production, use_delta=True)
            if prod_history:
                prod_unit = _unit(hass, meter_production, "kWh")
        if not prod_history and production_entity:
            prod_history = await _get_history(hass, start, end, production_entity)
            prod_unit = _unit(hass, production_entity)

        add_bat_history = []
        out_bat_history = []
        bat_unit = ""
        if meter_battery_charge and meter_battery_discharge:
            ch_history = await _get_history(hass, start, end, meter_battery_charge, use_delta=True)
            dis_history = await _get_history(hass, start, end, meter_battery_discharge, use_delta=True)
            if ch_history or dis_history:
                if battery_charge_positive:
                    add_bat_history, out_bat_history = ch_history, dis_history
                else:
                    add_bat_history, out_bat_history = dis_history, ch_history
                bat_unit = _unit(hass, meter_battery_charge, "kWh")
        if not add_bat_history and not out_bat_history and battery_entity:
            # Signed battery power: positive = charge, negative = discharge
            bat_history = await _get_history(hass, start, end, battery_entity)
            add_bat_history, out_bat_history = _split_signed(bat_history)
            bat_unit = _unit(hass, battery_entity)

        soc_history = []
        if battery_soc_entity:
            soc_history = await _get_history(hass, start, end, battery_soc_entity)

        payload = {
            "weatherHistory": await _get_weather(start, end),
            "productionUnit": prod_unit,
            "productionHistory": prod_history,
            "importUnit": grid_unit,
            "importHistory": import_history,
            "exportUnit": grid_unit,
            "exportHistory": export_history,
            "addBatteryUnit": bat_unit,
            "addBatteryHistory": add_bat_history,
            "outBatteryUnit": bat_unit,
            "outBatteryHistory": out_bat_history,
            "socHistory": soc_history,
            "consumers": [],
        }

        for consumer in entry.options.get(CONF_CONSUMERS, []):
            entity_id = consumer["entity"]
            name = consumer["name"]
            is_counter, sc, dc, unit = _detect_counter(hass, entity_id, name)
            if is_counter is None:
                continue
            c_history = await _get_history(hass, start, end, entity_id, use_delta=is_counter)
            subtract_ids = consumer.get(CONF_SUBTRACT_ENTITIES, [])
            if subtract_ids:
                sub_histories = []
                for sub_eid in subtract_ids:
                    sub_counter, _sc, _dc, _u = _detect_counter(hass, sub_eid, f"sub:{sub_eid}")
                    if sub_counter is None:
                        continue
                    if sub_counter != is_counter:
                        _LOGGER.warning(
                            "Consumer %s: subtract entity %s has different type (counter=%s vs parent counter=%s), forcing parent mode",
                            name, sub_eid, sub_counter, is_counter,
                        )
                    sub_histories.append(await _get_history(hass, start, end, sub_eid, use_delta=is_counter))
                c_history = _subtract_histories(c_history, sub_histories)
            _LOGGER.debug(
                "Consumer %s: sc=%r dc=%r unit=%r counter=%s pts=%d subs=%d",
                name, sc, dc, unit, is_counter, len(c_history), len(subtract_ids),
            )
            payload["consumers"].append({
                "name": name,
                # A counter whose unit is not loaded yet (cache): kWh, as for the meters
                "unit": unit or ("kWh" if is_counter else ""),
                "category": consumer.get("category", "other"),
                "history": c_history,
            })

        return payload

    async def _post(payload: dict) -> bool:
        try:
            async with session.post(
                API_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status == 200:
                    return True
                if resp.status == 401:
                    _LOGGER.error("APER : clé API refusée, demandez-en une nouvelle à l'APER")
                else:
                    _LOGGER.error("API error %s: %s", resp.status, await resp.text())
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            _LOGGER.error("Failed to send data: %s", type(err).__name__)
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
                "History sent: %d prod, %d imp, %d exp, %d bat, %d consumers",
                len(payload["productionHistory"]),
                len(payload["importHistory"]),
                len(payload["exportHistory"]),
                len(payload["addBatteryHistory"]) + len(payload["outBatteryHistory"]),
                len(payload["consumers"]),
            )

    async def _backfill(_now=None):
        """Combler les trous des derniers jours depuis le recorder HA."""
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
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            _LOGGER.warning("Coverage API unreachable: %s", type(err).__name__)
            return

        existing = set(coverage.get("slots", []))
        # The server compares consumer names without case
        consumer_existing = {
            name.lower(): set(slots) for name, slots in coverage.get("consumerSlots", {}).items()
        }

        expected = set()
        bucket = start
        while bucket < current:
            expected.add(bucket.strftime(SLOT_FORMAT))
            bucket += timedelta(minutes=BUCKET_MINUTES)

        missing = expected - existing

        for consumer in entry.options.get(CONF_CONSUMERS, []):
            c_name = _consumer_name(consumer["name"])
            c_missing = expected - consumer_existing.get(c_name.lower(), set())
            if c_missing:
                _LOGGER.info("Backfill: consommateur '%s' manque %d slots", c_name, len(c_missing))
            missing |= c_missing

        if not missing:
            _LOGGER.debug("Backfill: aucun trou détecté")
            return

        actionable = sorted(s for s in missing if failures.get(s, 0) < BACKFILL_MAX_RETRIES)
        if not actionable:
            _LOGGER.debug("Backfill: tous les trous ont atteint le max de tentatives")
            return

        _LOGGER.info("Backfill: %d trous, %d à traiter", len(missing), len(actionable))

        # Consecutive quarters of an hour grouped into ranges of 24 h at most
        ranges = []
        for slot_str in actionable:
            slot_dt = datetime.strptime(slot_str, SLOT_FORMAT).replace(tzinfo=dt_util.UTC)
            slot_end = slot_dt + timedelta(minutes=BUCKET_MINUTES)
            if ranges and ranges[-1][1] == slot_dt and slot_end - ranges[-1][0] <= timedelta(hours=24):
                ranges[-1][1] = slot_end
            else:
                ranges.append([slot_dt, slot_end])

        _LOGGER.info("Backfill: %d tranches à envoyer", len(ranges))

        for idx, (r_start, r_end) in enumerate(ranges):
            if idx > 0:
                await asyncio.sleep(5)

            range_slots = []
            s = r_start
            while s < r_end:
                range_slots.append(s.strftime(SLOT_FORMAT))
                s += timedelta(minutes=BUCKET_MINUTES)

            try:
                payload = await _collect(r_start, r_end)

                has_main = bool(payload["importHistory"] or payload["exportHistory"])
                has_consumers = any(c["history"] for c in payload["consumers"])
                if not has_main and not has_consumers:
                    for sl in range_slots:
                        failures[sl] = failures.get(sl, 0) + 1
                    _LOGGER.info("Backfill: pas de données recorder pour %s→%s", r_start, r_end)
                    continue

                # Sent again until it is complete, but only a few times: without weather (Open-Meteo
                # unavailable, no location in Home Assistant) the server keeps asking for these slots
                if not await _post(payload) or not payload["weatherHistory"]:
                    for sl in range_slots:
                        failures[sl] = failures.get(sl, 0) + 1
                else:
                    _LOGGER.info("Backfill OK: %s → %s (%d points)", r_start, r_end,
                                 len(payload["productionHistory"]))

            except Exception as err:  # noqa: BLE001 - one bad range must not stop the others
                _LOGGER.error("Backfill error %s→%s: %s", r_start, r_end, err)

    def _read_number(entity_id):
        """Current value of an entity, or None if unknown."""
        state = hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _read_power_w(entity_id):
        """Current power of an entity in watts, or None."""
        value = _read_number(entity_id)
        if value is None:
            return None
        unit = (hass.states.get(entity_id).attributes.get("unit_of_measurement") or "").lower().strip()
        return value * 1000 if unit == "kw" else value

    def _live_values() -> dict:
        """Instantaneous values, in W: grid positive = import, battery positive = charge."""
        grid = _read_power_w(grid_entity)
        if grid is not None and not grid_import_positive:
            grid = -grid

        consumers_live = []
        for consumer in entry.options.get(CONF_CONSUMERS, []):
            power_eid = consumer.get(CONF_CONSUMER_POWER_ENTITY)
            if not power_eid:
                # The main entity is used when it is a power sensor, not an energy counter
                if _detect_counter(hass, consumer["entity"], consumer["name"])[0] is False:
                    power_eid = consumer["entity"]
            if not power_eid:
                continue
            power_w = _read_power_w(power_eid)
            if power_w is None:
                continue
            for sub_eid in consumer.get(CONF_SUBTRACT_ENTITIES, []):
                sub_w = _read_power_w(sub_eid)
                if sub_w is not None:
                    power_w -= sub_w
            consumers_live.append({
                "name": consumer["name"],
                "category": consumer.get("category", "other"),
                "power": round(max(0, power_w), 1),
            })

        return {
            "grid": grid,
            "production": _read_power_w(production_entity),
            "battery": _read_power_w(battery_entity),
            "batterySoc": _read_number(battery_soc_entity),
            "consumers": consumers_live,
        }

    # Live: WebSocket kept open to the site. The site says when one of the member's cockpits is open;
    # values are then sent at each change of a sensor (at most once a second) and every 10 s anyway.
    live = {"ws": None, "watching": False, "dirty": False, "busy": False, "last_sent": 0.0}

    async def _live_connection():
        delay = LIVE_RECONNECT_MIN_SECONDS
        interrupted = False
        while True:
            try:
                async with session.ws_connect(LIVE_WS_URL, headers=headers, heartbeat=30) as ws:
                    live["ws"] = ws
                    delay = LIVE_RECONNECT_MIN_SECONDS
                    if interrupted:
                        _LOGGER.info("Live : connexion rétablie")
                        interrupted = False
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            message = json.loads(msg.data)
                        except ValueError:
                            continue
                        if message.get("type") == "watch":
                            live["watching"] = bool(message.get("on"))
                            live["dirty"] = live["watching"]
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                if err.status == 401:
                    _LOGGER.error("APER : clé API refusée pour le live")
                    delay = LIVE_RECONNECT_MAX_SECONDS
                elif not interrupted:
                    _LOGGER.info("Live interrompu (HTTP %s), reprise automatique", err.status)
                interrupted = True
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
                # Said once: during an Internet outage this would otherwise fill the log
                if not interrupted:
                    _LOGGER.info("Live interrompu (%s), reprise automatique", type(err).__name__)
                interrupted = True
            finally:
                live["ws"] = None
                live["watching"] = False
            if not interrupted:
                # Closed by the site (restart, update): it is usually back within seconds
                interrupted = True
            await asyncio.sleep(delay)
            delay = min(delay * 2, LIVE_RECONNECT_MAX_SECONDS)

    @callback
    def _live_changed(_event: Event) -> None:
        live["dirty"] = True

    async def _live_tick(_now=None):
        ws = live["ws"]
        if ws is None or ws.closed or not live["watching"] or live["busy"]:
            return
        now = hass.loop.time()
        if not live["dirty"] and now - live["last_sent"] < LIVE_HEARTBEAT_SECONDS:
            return
        live["busy"] = True
        live["dirty"] = False
        live["last_sent"] = now
        try:
            await ws.send_json(_live_values())
        except (aiohttp.ClientError, ConnectionResetError, RuntimeError):
            # The connection loop notices the closing and reconnects
            pass
        finally:
            live["busy"] = False

    live_entities = [eid for eid in (grid_entity, production_entity, battery_entity, battery_soc_entity) if eid]
    for consumer in entry.options.get(CONF_CONSUMERS, []):
        live_entities.append(consumer.get(CONF_CONSUMER_POWER_ENTITY) or consumer["entity"])
        live_entities.extend(consumer.get(CONF_SUBTRACT_ENTITIES, []))
    entry.async_on_unload(async_track_state_change_event(hass, sorted(set(live_entities)), _live_changed))
    entry.async_on_unload(async_track_time_interval(hass, _live_tick, timedelta(seconds=1)))
    entry.async_create_background_task(hass, _live_connection(), f"{DOMAIN}_live")
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
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
