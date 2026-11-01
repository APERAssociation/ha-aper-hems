"""The decisions of the control of the charging stations, every minute, from the planning of the HEMS.

The station of the car the planning charges (its name in the planning) is driven through the driver of its brand: started
when the quarter of an hour in progress says "charge", stopped otherwise, only when the planning changes its mind (a manual
change holds until then); a status that does not allow a start, or a car that wants no more energy, stops everything until
the car is unplugged. Its current: on the solar surplus in "Solaire d'abord" (quarters planned on the sun), down to its
lowest current; at its highest otherwise, or without sun for a while. Never more than the home may take from the grid
(its connection): lowered as soon as the import goes above it (protect), raised again every minute when it comes down.

Kept apart from Home Assistant (everything it reads comes in) so that it can be tested.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta

from ..const import (
    CONF_CHARGER_BRAND,
    CONF_CHARGER_MAX_CURRENT,
    CONF_CHARGER_MIN_CURRENT,
    CONF_CHARGER_PHASES,
    CONF_NAME,
    CONTROL_MAX_PLANNING_AGE_MINUTES,
    DONE_AFTER_MINUTES,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MIN_CURRENT,
    GRID_RAISE_MARGIN_W,
    PHASE_VOLTS,
    PHASES_MONO,
    PHASES_SWITCHABLE,
    PHASES_TRI,
    SUN_PHASE_MARGIN_W,
    SUN_START_MINUTES,
    SUN_STOP_MINUTES,
)
from . import ChargerDriver, driver_for


def parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


class ChargerControl:
    """hass: to drive the stations; chargers: the stations of the options; live_values(): the grid (+ import) and the
    battery (+ charge) in W; production_w(): the sun now in W; grid_max_w: what the home may take from the grid (W, None:
    no limit); log(charger, command, reason, test, **extra): what was sent (or would have been, in test mode); warn(text):
    a command refused."""

    def __init__(self, hass, chargers: list[dict], live_values: Callable, production_w: Callable,
                 log: Callable, warn: Callable = lambda text: None, grid_max_w: float | None = None,
                 driver_factory: Callable[..., ChargerDriver | None] = driver_for) -> None:
        self.hass = hass
        self.chargers = chargers
        self.live_values = live_values
        self.production_w = production_w
        self.log = log
        self.warn = warn
        self.grid_max_w = grid_max_w if grid_max_w and grid_max_w > 0 else None
        self.driver_factory = driver_factory
        # By station: what the planning wanted last (to act only when it changes), a start waiting for the car to be plugged
        # in, the current set, whether nothing more is done until the car is unplugged, and the current of a charge
        # started by hand before the connection lowered it
        self.stations: dict[str, dict] = {}

    def forget(self, name: str) -> None:
        """Starts again from what the planning says now (after a test of the station)."""
        self.stations.pop(name, None)

    def _station(self, data: dict, now: datetime):
        """The station of the planning, its driver and the quarter of an hour in progress; None when nothing is to be done."""
        generated = parse_time(data.get("generatedAt"))
        # No recent planning: nothing is changed (a charge in progress goes on, as the member left it)
        if generated is None or now - generated > timedelta(minutes=CONTROL_MAX_PLANNING_AGE_MINUTES):
            return None
        name = data.get("chargerName")
        charger = next((c for c in self.chargers if c.get(CONF_NAME) == name), None)
        driver = self.driver_factory(self.hass, charger, charger.get(CONF_CHARGER_BRAND)) if charger else None
        if driver is None:
            return None
        quarter = None
        for q in data.get("quarters", []):
            start = parse_time(q.get("start"))
            if start is not None and start <= now < start + timedelta(minutes=15):
                quarter = q
        if quarter is None:
            return None
        state = self.stations.setdefault(name, {"want": None, "pending_start": False, "current": None, "blocked": False,
                                                "done_since": None, "manual": None, "phases": None, "sun": False, "sun_since": None,
                                                "sun_low_since": None, "sun_phases": None, "sun_phases_since": None})
        kind = charger.get(CONF_CHARGER_PHASES)
        # Its phases: three for a three-phase station (6 A = 4.2 kW), one for a single-phase one, as planned for a switchable one
        phases = 3 if kind == PHASES_TRI else 1 if kind == PHASES_MONO else int(quarter.get("carPhases") or 3)
        return name, charger, driver, quarter, state, phases

    def _grid_cap(self, driver: ChargerDriver, phases: int, margin_w: float) -> int | None:
        """The highest current that keeps the import under the connection's limit (None: no limit): what the car takes now,
        plus what the home may still take from the grid."""
        if self.grid_max_w is None:
            return None
        grid = self.live_values().get("grid")
        if grid is None:
            return None
        car_w = (driver.current_a() or 0) * PHASE_VOLTS * phases if driver.is_running() else 0
        return math.floor((car_w + self.grid_max_w - grid - margin_w) / (PHASE_VOLTS * phases))

    def _limit_reason(self) -> str:
        grid = self.live_values().get("grid") or 0
        return f"Raccordement {self.grid_max_w / 1000:g} kW : import {round(grid)} W"

    async def protect(self, data: dict | None, now: datetime, test: bool) -> None:
        """At each change of the grid: the current lowered at once when the import goes above the connection's limit."""
        if self.grid_max_w is None:
            return
        found = self._station(data or {}, now)
        if found is None:
            return
        name, charger, driver, quarter, state, phases = found
        if state["blocked"] or not driver.is_running():
            return
        low = int(charger.get(CONF_CHARGER_MIN_CURRENT, DEFAULT_MIN_CURRENT))
        cap = self._grid_cap(driver, phases, 0)
        known = state["current"] if test and state["current"] is not None else driver.current_a()
        if cap is None or known is None or known <= max(low, cap):
            return
        amperes = max(low, cap)
        # A charge started by hand: the current it had, to give it back
        if not state["want"] and state["manual"] is None:
            state["manual"] = int(known)
        if await self._send(name, driver, test, f"{amperes} A", lambda: driver.set_current(amperes), self._limit_reason(),
                            current_a=amperes):
            state["current"] = amperes

    async def _send(self, name: str, driver: ChargerDriver, test: bool, command: str, action, reason: str, **extra) -> bool:
        if not test:
            try:
                await action()
            except Exception as err:  # noqa: BLE001 - a missing entity or a refused command must not stop the plugin
                self.warn(f"Borne {name} : {command} refusé : {err}")
                return False
        self.log(name, command, reason, test, status=driver.status(), **extra)
        return True

    async def tick(self, data: dict | None, now: datetime, test: bool) -> None:
        data = data or {}
        found = self._station(data, now)
        if found is None:
            return
        name, charger, driver, quarter, state, phases = found
        # The planning decides the charges from the grid; the sun is followed on the live values alone (a quarter planned on the
        # sun charges only if the live surplus is there)
        planned = quarter["car"] == "charge" and quarter.get("carSource") != "solar"
        was_sun = state["sun"]
        sun = self._sun(data, quarter, charger, driver, state, phases, now, planned)
        want = planned or sun
        if sun and not planned:
            phases = state["sun_phases"] or phases
        status = driver.status()

        async def send(command: str, action, reason: str, **extra) -> bool:
            return await self._send(name, driver, test, command, action, reason, **extra)

        def block(reason: str) -> None:
            state.update(blocked=True, pending_start=False, current=None)
            self.log(name, "attente débranchement", reason, test, status=status)

        # The car unplugged: a new cycle, from what the planning says now (a charge planned waits for the car)
        if driver.is_unplugged() and state["blocked"]:
            state.update(blocked=False, want=None, pending_start=False)
            self.log(name, "nouveau cycle", "Voiture débranchée", test, status=status)
        if driver.is_unplugged():
            state["manual"] = None
            await self._free_phases(name, driver, state, test, "Voiture débranchée")
        # The car wants no more energy for a while (not only waking up after a start): nothing more until it is unplugged
        if driver.is_done():
            state["done_since"] = state["done_since"] or now
        else:
            state["done_since"] = None
        if (state["done_since"] is not None and now - state["done_since"] >= timedelta(minutes=DONE_AFTER_MINUTES)
                and not state["blocked"]):
            block(f"Statut {status} depuis {DONE_AFTER_MINUTES} min : la voiture ne demande plus d'énergie, "
                  "rien jusqu'au débranchement")
        if state["blocked"]:
            state["want"] = want
            return

        # On / off: only when the planning changes its mind (a manual change holds until then)
        if want != state["want"]:
            state["want"] = want
            state["pending_start"] = want
            if want:
                state["manual"] = None
            if not want and driver.is_charging():
                reason = (f"Surplus solaire sous le courant minimal depuis {SUN_STOP_MINUTES} min" if was_sun
                          else quarter.get("carLabel") or "Pas de recharge prévue")
                await send("stop", driver.stop, reason)
            if not want:
                await self._free_phases(name, driver, state, test, "Fin de la recharge pilotée")
        # A station that switches between one and three phases: the phases the planning wants, set before a start and when
        # they change; a charge in progress is paused to take them, and started again at the next minute
        if (want and self._switches(charger, driver) and not driver.is_unplugged() and state["phases"] != phases):
            words = "monophasé" if phases == 1 else "triphasé"
            if await send(f"{phases} phase" + ("s" if phases > 1 else ""), lambda: driver.set_phases(phases),
                          f"Planning en {words}", phases=phases):
                state["phases"] = phases
                if driver.is_running():
                    if await send("stop", driver.stop, f"Passage en {words} : pause, reprise à la minute suivante"):
                        state["pending_start"] = True
                    return
        low = int(charger.get(CONF_CHARGER_MIN_CURRENT, DEFAULT_MIN_CURRENT))
        high = int(charger.get(CONF_CHARGER_MAX_CURRENT, DEFAULT_MAX_CURRENT))
        if state["pending_start"]:
            if driver.is_running():
                state["pending_start"] = False
            elif driver.can_start():
                # Its current first: a station starts at the current it had, the whole surplus (or more) bought for a minute
                await self._set_current(data, quarter, driver, state, phases, low, high, now, test, send)
                if await send("start", driver.start, "Charger VE" if planned else "Surplus solaire imprévu"):
                    state["pending_start"] = False
                    # The grid does not show the car yet: its current looked at again at the next minute
                    return
            elif not driver.is_unplugged():
                # A status that does not allow a start: nothing more is tried until the car is unplugged (not plugged in yet:
                # the start waits for it)
                block(f"Statut {status} : pas de démarrage, rien jusqu'au débranchement")
                return

        known = driver.current_a() if not test else state["current"]
        if not want:
            state["current"] = None
            # A charge started by hand, lowered for the connection: its current given back as the import comes down
            if state["manual"] is not None and driver.is_running():
                cap = self._grid_cap(driver, phases, GRID_RAISE_MARGIN_W)
                amperes = state["manual"] if cap is None else max(low, min(state["manual"], cap))
                if known is not None and amperes > known:
                    if await send(f"{amperes} A", lambda: driver.set_current(amperes), "Import redescendu", current_a=amperes):
                        state["current"] = amperes
                if amperes >= state["manual"]:
                    state["manual"] = None
            return
        await self._set_current(data, quarter, driver, state, phases, low, high, now, test, send)

    async def _free_phases(self, name: str, driver: ChargerDriver, state: dict, test: bool, reason: str) -> None:
        """The phases locked for a charge it drove, left again to the station once that charge is over (a charge started by
        hand, from its app, takes its phases as before: not one only)."""
        if state["phases"] is None or not driver.SWITCHES_PHASES:
            return
        if await self._send(name, driver, test, "phases auto", lambda: driver.set_phases(0), reason, phases=0):
            state["phases"] = None

    @staticmethod
    def _switches(charger: dict, driver: ChargerDriver) -> bool:
        return charger.get(CONF_CHARGER_PHASES) == PHASES_SWITCHABLE and driver.SWITCHES_PHASES

    def _sun_phases(self, charger: dict, driver: ChargerDriver, state: dict, available: float, low: int, now: datetime) -> int:
        """The phases of a charge on an unplanned surplus: one for a single-phase station, three for a three-phase one; for a
        station that switches, three once the surplus reaches the lowest three-phase current (one under it), a change being
        made only past a margin and after SUN_STOP_MINUTES (each one pauses and starts the charge again)."""
        kind = charger.get(CONF_CHARGER_PHASES)
        if not self._switches(charger, driver):
            return 1 if kind == PHASES_MONO else 3
        tri_w = low * PHASE_VOLTS * 3
        chosen = state.get("sun_phases")
        if chosen is None:
            state.update(sun_phases=3 if available >= tri_w else 1, sun_phases_since=None)
            return state["sun_phases"]
        target = 3 if available >= tri_w + SUN_PHASE_MARGIN_W else 1 if available < tri_w - SUN_PHASE_MARGIN_W else chosen
        if target == chosen:
            state["sun_phases_since"] = None
        else:
            state["sun_phases_since"] = state.get("sun_phases_since") or now
            if now - state["sun_phases_since"] >= timedelta(minutes=SUN_STOP_MINUTES):
                state.update(sun_phases=target, sun_phases_since=None)
        return state["sun_phases"]

    def _sun(self, data: dict, quarter: dict, charger: dict, driver: ChargerDriver, state: dict, phases: int, now: datetime,
             planned: bool) -> bool:
        """A charge on the solar surplus seen live (the export, what the car takes, the battery), whatever the planning says,
        when no charge from the grid is planned, the car plugged in, in "Solaire d'abord" or "Plein au bon prix" (the sun is
        free). Started once the surplus has reached the lowest current for SUN_START_MINUTES (at once for a charge already
        going on: started by the planning, or on the sun before), stopped once it has been under it for SUN_STOP_MINUTES."""
        if planned or data.get("carMode") not in ("solar_first", "best_price") or driver.is_unplugged():
            state.update(sun=False, sun_since=None, sun_low_since=None, sun_phases=None, sun_phases_since=None)
            return False
        low = int(charger.get(CONF_CHARGER_MIN_CURRENT, DEFAULT_MIN_CURRENT))
        # Enough sun: the lowest current on one phase for a station that switches
        sun_phases = 1 if self._switches(charger, driver) or charger.get(CONF_CHARGER_PHASES) == PHASES_MONO else 3
        values = self.live_values()
        running = driver.is_running()
        # What the car could take: what it takes now (from its setting, on the phases it charges on), what goes to the grid,
        # what charges the battery, minus what the battery gives
        car_w = (driver.current_a() or 0) * PHASE_VOLTS * (state["phases"] or phases) if running else 0
        available = car_w - (values.get("grid") or 0) + (values.get("battery") or 0)
        if available >= low * PHASE_VOLTS * sun_phases:
            state["sun_low_since"] = None
            state["sun_since"] = state.get("sun_since") or now
            # A charge going on (started by the planning, or found charging when Home Assistant starts): on the sun at once
            if not state["sun"] and ((running and state["want"] is not False)
                                     or now - state["sun_since"] >= timedelta(minutes=SUN_START_MINUTES)):
                state["sun"] = True
        else:
            state["sun_since"] = None
            if state["sun"]:
                state["sun_low_since"] = state.get("sun_low_since") or now
                if now - state["sun_low_since"] >= timedelta(minutes=SUN_STOP_MINUTES):
                    state["sun"] = False
        if state["sun"]:
            self._sun_phases(charger, driver, state, available, low, now)
        else:
            state.update(sun_phases=None, sun_phases_since=None)
        return state["sun"]

    async def _set_current(self, data: dict, quarter: dict, driver: ChargerDriver, state: dict, phases: int, low: int, high: int,
                           now: datetime, test: bool, send) -> None:
        """The current: on the solar surplus seen live for a charge on the sun, at the highest for a charge planned on the grid;
        never above the connection."""
        known = driver.current_a() if not test else state["current"]
        amperes, reason = high, "Pleine puissance"
        if state["sun"]:
            # What the car could take without buying: what it takes now, plus what goes to the grid, plus what charges the
            # battery (the car first in "Solaire d'abord"), minus what the battery gives (it must not feed the car). What it
            # takes now from its current setting: a power sensor read from the cloud is a minute or two late, and the
            # current would go up and down
            values = self.live_values()
            car_w = (driver.current_a() or 0) * PHASE_VOLTS * phases if driver.is_running() else 0
            available = car_w - (values.get("grid") or 0) + (values.get("battery") or 0)
            amperes = max(low, min(high, int(available // (PHASE_VOLTS * phases))))
            reason = f"Surplus solaire {round(available)} W"
        # Never above the connection: lowered at once, raised only with a margin left (no going up and down)
        cap = self._grid_cap(driver, phases, 0)
        if cap is not None and (known is None or amperes > known):
            cap = max(self._grid_cap(driver, phases, GRID_RAISE_MARGIN_W), min(known or 0, cap))
        if cap is not None and amperes > cap:
            amperes = max(low, cap)
            if known is not None and amperes == known:
                return
            reason = self._limit_reason()
        if known is None or abs(known - amperes) >= 1:
            if await send(f"{amperes} A", lambda: driver.set_current(amperes), reason, current_a=amperes):
                state["current"] = amperes
