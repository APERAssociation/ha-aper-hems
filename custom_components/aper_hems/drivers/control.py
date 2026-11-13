"""The decisions of the control of the charging stations, every minute, from the planning of the HEMS.

The station of the car the planning charges (its name in the planning) is driven through the driver of its brand: started
when the quarter of an hour in progress says "charge", stopped otherwise when the planning changes its mind, and a charge
going on that the settings do not want (started by the station, the car or by hand) stopped at the next minute, again
every minute while it goes on; a status that does not allow a start, or a car that wants no more energy, stops everything until
the car is unplugged. Its current: on the solar surplus in "Solaire d'abord" (quarters planned on the sun), down to its
lowest current (with the house's base load, kept at its lowest current while the sun makes more than the base load, the
battery of the house giving the difference only when it is well ahead of the planning: battery_source); at its highest
otherwise, or without sun for a while. Never more than the home may take from the grid
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
    START_CHECK_MINUTES,
    START_TRIES,
    SUN_BATTERY_START_KWH,
    SUN_BATTERY_STOP_KWH,
    SUN_PHASE_DOWN_MINUTES,
    SUN_PHASE_MARGIN_W,
    SUN_PHASE_UP_HOLD_MINUTES,
    PLUG_START_WINDOW_MINUTES,
    SUN_RESTART_MINUTES,
    SUN_START_MINUTES,
    SUN_STOP_MINUTES,
    UNWANTED_REPEAT_MINUTES,
    UNWANTED_STOP_MINUTES,
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
        # By station, whether the car was unplugged at the last look, and when it was plugged in (whatever the planning names)
        self.plugs: dict[str, dict] = {}
        # Whether the battery of the house is far enough ahead of the planning to give to a car charging on the sun
        self.battery_ahead = False

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
                                                "sun_low_since": None, "sun_phases": None, "sun_phases_since": None,
                                                "sun_floor": False,
                                                "started_at": None, "start_tries": 0, "ran": False, "restarts": 0})
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
                # In the commands sensor too: the member sees why nothing happens
                self.log(name, f"{command} refusé", str(err) or type(err).__name__, test, status=driver.status(), **extra)
                return False
        self.log(name, command, reason, test, status=driver.status(), **extra)
        return True

    async def tick(self, data: dict | None, now: datetime, test: bool) -> bool:
        """Each minute: the station of the planning driven. True when a car has just been plugged in: the planning in hand does
        not know it yet, it must be asked again."""
        plugged = self._watch_plugs(now)
        await self._tick(data or {}, now, test)
        return plugged

    @staticmethod
    def _grid_charge(quarter: dict | None) -> bool:
        """A quarter of an hour that charges the car from the grid (the planning decides those; the sun is followed live)."""
        return quarter is not None and quarter.get("car") == "charge" and quarter.get("carSource") != "solar"

    @staticmethod
    def _next_quarter(data: dict, quarter: dict) -> dict | None:
        start = parse_time(quarter.get("start"))
        if start is None:
            return None
        return next((q for q in data.get("quarters", []) if parse_time(q.get("start")) == start + timedelta(minutes=15)), None)

    def _watch_plugs(self, now: datetime) -> bool:
        """Every station, planned or not: a car plugged in or unplugged starts a new cycle (what the HEMS did before is
        forgotten, what the planning says now is followed, a charge the station starts by itself is stopped)."""
        plugged = False
        for charger in self.chargers:
            name = charger.get(CONF_NAME)
            driver = self.driver_factory(self.hass, charger, charger.get(CONF_CHARGER_BRAND))
            if driver is None or driver.status() is None:
                continue
            unplugged = driver.is_unplugged()
            plug = self.plugs.setdefault(name, {"unplugged": None, "plugged_at": None})
            if plug["unplugged"] is not None and plug["unplugged"] != unplugged:
                state = self.stations.get(name)
                if state is not None:
                    if unplugged and state["blocked"]:
                        self.log(name, "nouveau cycle", "Voiture débranchée", False, status=driver.status())
                    state.update(want=None, pending_start=False, blocked=False, manual=None, started_at=None, start_tries=0,
                                 ran=False, restarts=0)
                if not unplugged:
                    plug["plugged_at"] = now
                    plugged = True
                    self.log(name, "branchée", "Voiture branchée : planning redemandé", False, status=driver.status())
                else:
                    plug["plugged_at"] = None
            plug["unplugged"] = unplugged
        return plugged

    async def _tick(self, data: dict, now: datetime, test: bool) -> None:
        found = self._station(data, now)
        if found is None:
            return
        name, charger, driver, quarter, state, phases = found
        # The planning decides the charges from the grid; the sun is followed on the live values alone (a quarter planned on the
        # sun charges only if the live surplus is there)
        planned = self._grid_charge(quarter)
        # A charge from the grid going on is not stopped for a single quarter of an hour when the planning charges again the next
        # one (the battery taking the connection for a while, the house a little higher): stopping and starting again wears the
        # station; meanwhile the connection's limit lowers its current
        if (not planned and state["want"] is True and not state["sun"] and driver.is_running()
                and self._grid_charge(self._next_quarter(data, quarter))):
            planned = True
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

        # Just plugged in, no charge wanted, and the station charging by itself: stopped once (a charge started later by hand
        # from its app holds)
        plug = self.plugs.get(name) or {}
        if (not want and plug.get("plugged_at") is not None and driver.is_charging()
                and now - plug["plugged_at"] <= timedelta(minutes=PLUG_START_WINDOW_MINUTES)):
            plug["plugged_at"] = None
            state["want"] = False
            await send("stop", driver.stop, "La borne a démarré seule au branchement : "
                       + (quarter.get("carLabel") or "pas de recharge prévue maintenant"))
            await self._free_phases(name, driver, state, test, "Fin de la recharge pilotée")
            return
        # On / off: when the planning changes its mind; a charge going on that it does not want is stopped further down
        if want != state["want"]:
            state["want"] = want
            state["pending_start"] = want
            if want:
                state["manual"] = None
            if not want and driver.is_charging():
                reason = (f"Surplus solaire sous le courant minimal depuis {SUN_STOP_MINUTES} min" if was_sun
                          else quarter.get("carLabel") or "Pas de recharge prévue")
                if was_sun and state.get("sun_floor"):
                    reason = (f"Soleil sous le talon ({round(float(data.get('houseBaseKw') or 0) * 1000)} W) "
                              f"depuis {SUN_STOP_MINUTES} min")
                # Sent again later only if the charge goes on (UNWANTED_REPEAT_MINUTES)
                state["unwanted_stop_at"] = now
                if await send("stop", driver.stop, reason):
                    state["stopped_at"] = now
            if not want:
                await self._free_phases(name, driver, state, test, "Fin de la recharge pilotée")
        # A station that switches between one and three phases: the phases the planning wants, set before a start and when
        # they change; during a charge the driver pauses it around the switch (the car keeps the phases of its session)
        if (want and self._switches(charger, driver) and not driver.is_unplugged() and state["phases"] != phases):
            words = "monophasé" if phases == 1 else "triphasé"
            if await send(f"{phases} phase" + ("s" if phases > 1 else ""), lambda: driver.set_phases(phases),
                          f"Planning en {words}", phases=phases):
                state["phases"] = phases
        low = int(charger.get(CONF_CHARGER_MIN_CURRENT, DEFAULT_MIN_CURRENT))
        high = int(charger.get(CONF_CHARGER_MAX_CURRENT, DEFAULT_MAX_CURRENT))
        # A start the station did not follow: still waiting to start a while after it (a car waking up shows another status)
        retry = relaunch = False
        if not want or driver.is_running():
            state.update(started_at=None, start_tries=0)
        # A charge wanted that ran and ended by itself (the transaction over: Preparing or Finishing for a Wallbox, its switch
        # still on, so not stopped by hand from its app or Home Assistant, which holds): started again, RESTARTS_MAX times at
        # most until the car is unplugged (a station that keeps ending it is left alone)
        if not want:
            state["ran"] = False
        elif driver.is_running():
            state["ran"] = True
        elif (state["ran"] and not state["pending_start"] and state["started_at"] is None
                and driver.status() in driver.RESTART_STATUSES and driver.switch_on() is True):
            state["ran"] = False
            if state["restarts"] >= START_TRIES:
                block(f"La recharge s'est arrêtée seule {START_TRIES} fois (statut {status}), rien jusqu'au débranchement")
                return
            state.update(pending_start=True, restarts=state["restarts"] + 1)
            relaunch = True
        elif (not relaunch and not state["pending_start"] and state["started_at"] is not None and driver.can_start()
                and now - state["started_at"] >= timedelta(minutes=START_CHECK_MINUTES)):
            if state["start_tries"] >= START_TRIES:
                block(f"La borne n'a pas démarré après {START_TRIES} essais (statut {status}), rien jusqu'au débranchement")
                return
            state["pending_start"] = retry = True
        if state["pending_start"]:
            if driver.is_running():
                state["pending_start"] = False
            elif driver.can_start():
                # Its current first: a station starts at the current it had, the whole surplus (or more) bought for a minute
                await self._set_current(data, quarter, driver, state, phases, low, high, now, test, send)
                reason = "Charger VE" if planned else "Surplus solaire imprévu"
                if planned and quarter.get("carLabel"):
                    reason = f"Planning : {quarter['carLabel']} ({quarter.get('carCurrentA') or high} A, réseau)"
                elif not planned:
                    production = self.production_w()
                    surplus = self._surplus(driver, phases, high)
                    reason = (f"Surplus solaire imprévu : soleil {round(production or 0)} W, surplus {round(surplus)} W"
                              if not state.get("sun_floor") else
                              f"Soleil {round(production or 0)} W au-dessus du talon "
                              f"{round(float(data.get('houseBaseKw') or 0) * 1000)} W (surplus {round(surplus)} W)")
                if retry:
                    reason = f"Nouvel essai : la borne n'a pas démarré (statut {status})"
                elif relaunch:
                    reason = f"Relance : la recharge s'est arrêtée seule (statut {status})"
                if await send("start", driver.start, reason):
                    state.update(pending_start=False, started_at=now, start_tries=state["start_tries"] + 1)
                    # The grid does not show the car yet: its current looked at again at the next minute
                    return
            elif not driver.is_unplugged():
                # A status that does not allow a start: nothing more is tried until the car is unplugged (not plugged in yet:
                # the start waits for it)
                block(f"Statut {status} : pas de démarrage, rien jusqu'au débranchement")
                return

        if not want:
            state["current"] = None
            state["manual"] = None
            # A charge the settings do not want (no grid charge planned now: over the highest price, the level reached…, no
            # sun): stopped, whoever started it, after UNWANTED_STOP_MINUTES, again every UNWANTED_REPEAT_MINUTES while it goes on
            if not driver.is_running():
                state["unwanted_since"] = None
                return
            state["unwanted_since"] = state.get("unwanted_since") or now
            last = state.get("unwanted_stop_at")
            if (now - state["unwanted_since"] >= timedelta(minutes=UNWANTED_STOP_MINUTES)
                    and (last is None or now - last >= timedelta(minutes=UNWANTED_REPEAT_MINUTES))):
                label = quarter.get("carLabel") or "Pas de recharge prévue"
                price = quarter.get("price")
                reason = (f"Recharge non voulue par les réglages ({label}"
                          + (f", réseau à {float(price) * 100:.2f} ct" if price is not None else "") + ")")
                state["unwanted_stop_at"] = now
                if await send("stop", driver.stop, reason):
                    state["stopped_at"] = now
                    await self._free_phases(name, driver, state, test, "Fin de la recharge non voulue")
            return
        state["unwanted_since"] = None
        # Only a charge actually running is regulated (its live status): a car not plugged in, or plugged in and not charging,
        # gets its current when it starts (set before the start), not every minute from the planning and the import
        if not driver.is_running():
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
            # Down to one phase soon (three phases at their lowest current buy from the grid), up to three only after a while
            wait = SUN_PHASE_DOWN_MINUTES if target == 1 else SUN_STOP_MINUTES
            # Never back up to three soon after going down to one (no switch, so no pause, at each passing cloud)
            down_at = state.get("sun_phases_down_at")
            held = target == 3 and down_at is not None and now - down_at < timedelta(minutes=SUN_PHASE_UP_HOLD_MINUTES)
            if now - state["sun_phases_since"] >= timedelta(minutes=wait) and not held:
                state.update(sun_phases=target, sun_phases_since=None)
                if target == 1:
                    state["sun_phases_down_at"] = now
        return state["sun_phases"]

    def _sun(self, data: dict, quarter: dict, charger: dict, driver: ChargerDriver, state: dict, phases: int, now: datetime,
             planned: bool) -> bool:
        """A charge on the solar surplus seen live (the export, what the car takes, the battery), whatever the planning says,
        when no charge from the grid is planned, the car plugged in, in "Solaire d'abord" or "Bon tarif et soleil" (the sun is
        free). Started once the surplus has reached the lowest current for SUN_START_MINUTES (at once for a charge already
        going on: started by the planning, or on the sun before), stopped once it has been under it for SUN_STOP_MINUTES. In
        "Solaire d'abord" with the house's base load (solarMinimum: the car under its level wanted), the sun above the base load
        instead of the surplus (its lowest current kept, the rest bought)."""
        if planned or data.get("carMode") not in ("solar_first", "best_price") or driver.is_unplugged():
            state.update(sun=False, sun_since=None, sun_low_since=None, sun_phases=None, sun_phases_since=None, sun_floor=False)
            return False
        low = int(charger.get(CONF_CHARGER_MIN_CURRENT, DEFAULT_MIN_CURRENT))
        # Enough sun: the lowest current on one phase for a station that switches
        sun_phases = 1 if self._switches(charger, driver) or charger.get(CONF_CHARGER_PHASES) == PHASES_MONO else 3
        running = driver.is_running()
        available = self._surplus(driver, state["phases"] or phases,
                                  int(charger.get(CONF_CHARGER_MAX_CURRENT, DEFAULT_MAX_CURRENT)))
        # "Solaire d'abord" with the house's base load, the car under its level wanted: the sun above the base load is enough (its
        # lowest current kept, what the sun does not give bought); otherwise the surplus must reach the lowest current
        base_kw = data.get("houseBaseKw")
        floor = data.get("carMode") == "solar_first" and base_kw is not None and bool(quarter.get("solarMinimum"))
        if floor:
            production = self.production_w()
            enough = production is not None and production > float(base_kw) * 1000
        else:
            enough = available >= low * PHASE_VOLTS * sun_phases
        state["sun_floor"] = floor
        if enough:
            state["sun_low_since"] = None
            state["sun_since"] = state.get("sun_since") or now
            # A charge going on (started by the planning, or found charging when Home Assistant starts): on the sun at once;
            # otherwise once the surplus has lasted, and never less than SUN_RESTART_MINUTES after the last stop
            stopped_at = state.get("stopped_at")
            rested = stopped_at is None or now - stopped_at >= timedelta(minutes=SUN_RESTART_MINUTES)
            if not state["sun"] and ((running and state["want"] is not False)
                                     or (rested and now - state["sun_since"] >= timedelta(minutes=SUN_START_MINUTES))):
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

    def _surplus(self, driver: ChargerDriver, phases: int, high: int) -> float:
        """What the car could take without buying: what it takes now, plus what goes to the grid, plus what charges the battery
        (the car first in "Solaire d'abord"), minus what the battery gives (it must not feed the car). What it takes now from its
        current setting (a power sensor read from the cloud is a minute or two late, and the current would go up and down); never
        more than the panels make now: a car taking less than its setting (its own charger is smaller, it is nearly full) would
        otherwise look like a surplus, even at night). Its setting never counted above the highest current of the options: an
        Easee's dynamic limit can be at 32 A by itself."""
        values = self.live_values()
        car_w = min(driver.current_a() or 0, high) * PHASE_VOLTS * phases if driver.is_running() else 0
        available = car_w - (values.get("grid") or 0) + (values.get("battery") or 0)
        production = self.production_w()
        return available if production is None else min(available, max(0.0, production))

    async def _set_current(self, data: dict, quarter: dict, driver: ChargerDriver, state: dict, phases: int, low: int, high: int,
                           now: datetime, test: bool, send) -> None:
        """The current: on the solar surplus seen live for a charge on the sun, at the highest for a charge planned on the grid;
        never above the connection."""
        known = driver.current_a() if not test else state["current"]
        amperes, reason = high, "Pleine puissance (charge sur le réseau planifiée)"
        surplus_w = None
        if state["sun"]:
            available = self._surplus(driver, phases, high)
            surplus_w = round(available)
            amperes = max(low, min(high, int(available // (PHASE_VOLTS * phases))))
            reason = f"Surplus solaire {round(available)} W"
            if state.get("sun_floor") and available < low * PHASE_VOLTS * phases:
                reason = f"Soleil au-dessus du talon : courant minimal (surplus {round(max(0.0, available))} W)"
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
            extra = {"current_a": amperes, "phases": phases}
            if surplus_w is not None:
                extra["surplus_w"] = surplus_w
            if await send(f"{amperes} A", lambda: driver.set_current(amperes), reason, **extra):
                state["current"] = amperes

    def battery_advance_kwh(self, data: dict | None, now: datetime) -> float | None:
        """How far the battery of the house is ahead of the planning now (kWh, negative: behind): its live level against the
        one the planning expected at this minute (between the start and the end of the quarter of an hour), × its capacity."""
        data = data or {}
        capacity = data.get("batteryCapacityKwh")
        soc = self.live_values().get("batterySoc")
        if not capacity or soc is None:
            return None
        quarters = data.get("quarters", [])
        for i, q in enumerate(quarters):
            start = parse_time(q.get("start"))
            if start is None or not start <= now < start + timedelta(minutes=15) or q.get("plannedSoc") is None:
                continue
            before = quarters[i - 1].get("plannedSoc") if i > 0 else None
            before = q["plannedSoc"] if before is None else before
            expected = before + (q["plannedSoc"] - before) * ((now - start) / timedelta(minutes=15))
            return (float(soc) - expected) * float(capacity) / 100
        return None

    def battery_source(self, data: dict | None, now: datetime) -> tuple[str, str] | None:
        """While the station of the planning charges on the sun above the house's base load (its lowest current kept): what the
        battery of the house does, as a source of the planning, with the reason; None otherwise (the planning decides). It gives
        the difference ("solar": self-consumption) once it is SUN_BATTERY_START_KWH ahead of the planning, until it is only
        SUN_BATTERY_STOP_KWH ahead; otherwise it keeps its energy ("grid_only"), the grid gives what the sun does not."""
        state = self.stations.get((data or {}).get("chargerName"))
        if not state or not state.get("sun") or not state.get("sun_floor") or state.get("blocked"):
            return None
        advance = self.battery_advance_kwh(data, now)
        if advance is None:
            self.battery_ahead = False
        elif advance >= SUN_BATTERY_START_KWH:
            self.battery_ahead = True
        elif advance < SUN_BATTERY_STOP_KWH:
            self.battery_ahead = False
        if self.battery_ahead:
            return "solar", f"Voiture au soleil : batterie en avance de {advance:.1f} kWh, elle donne la différence"
        ahead = "" if advance is None else f" (avance {advance:.1f} kWh)"
        return "grid_only", f"Voiture au soleil au courant minimal : la batterie garde son énergie{ahead}"
