"""The decisions of the control of the charging stations, every minute, from the planning of the HEMS.

The station of the car the planning charges (its name in the planning) is driven through the driver of its brand: started
when the quarter of an hour in progress says "charge", stopped otherwise, only when the planning changes its mind (a manual
change holds until then); a status that does not allow a start, or a car that wants no more energy, stops everything until
the car is unplugged. Its current: on the solar surplus in "Solaire d'abord" (quarters planned on the sun), down to its
lowest current; at its highest otherwise, or without sun for a while.

Kept apart from Home Assistant (everything it reads comes in) so that it can be tested.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from ..const import (
    CONF_CHARGER_BRAND,
    CONF_CHARGER_MAX_CURRENT,
    CONF_CHARGER_MIN_CURRENT,
    CONF_CHARGER_PHASES,
    CONF_CHARGER_POWER_ENTITY,
    CONF_NAME,
    CONTROL_MAX_PLANNING_AGE_MINUTES,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MIN_CURRENT,
    NO_SUN_MINUTES,
    NO_SUN_W,
    PHASE_VOLTS,
    PHASES_MONO,
    PHASES_TRI,
)
from . import ChargerDriver, driver_for


def parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


class ChargerControl:
    """hass: to drive the stations; chargers: the stations of the options; read_power_w(entity) in W; live_values(): the
    grid (+ import) and the battery (+ charge) in W; production_w(): the sun now in W; log(charger, command, reason, test,
    **extra): what was sent (or would have been, in test mode); warn(text): a command refused."""

    def __init__(self, hass, chargers: list[dict], read_power_w: Callable, live_values: Callable, production_w: Callable,
                 log: Callable, warn: Callable = lambda text: None,
                 driver_factory: Callable[..., ChargerDriver | None] = driver_for) -> None:
        self.hass = hass
        self.chargers = chargers
        self.read_power_w = read_power_w
        self.live_values = live_values
        self.production_w = production_w
        self.log = log
        self.warn = warn
        self.driver_factory = driver_factory
        # By station: what the planning wanted last (to act only when it changes), a start waiting for the car to be plugged
        # in, the current set, and whether nothing more is done until the car is unplugged
        self.stations: dict[str, dict] = {}
        self.no_sun_since: datetime | None = None

    def forget(self, name: str) -> None:
        """Starts again from what the planning says now (after a test of the station)."""
        self.stations.pop(name, None)

    async def tick(self, data: dict | None, now: datetime, test: bool) -> None:
        data = data or {}
        generated = parse_time(data.get("generatedAt"))
        # No recent planning: nothing is changed (a charge in progress goes on, as the member left it)
        if generated is None or now - generated > timedelta(minutes=CONTROL_MAX_PLANNING_AGE_MINUTES):
            return
        name = data.get("chargerName")
        charger = next((c for c in self.chargers if c.get(CONF_NAME) == name), None)
        driver = self.driver_factory(self.hass, charger, charger.get(CONF_CHARGER_BRAND)) if charger else None
        if driver is None:
            return
        quarter = None
        for q in data.get("quarters", []):
            start = parse_time(q.get("start"))
            if start is not None and start <= now < start + timedelta(minutes=15):
                quarter = q
        if quarter is None:
            return
        state = self.stations.setdefault(name, {"want": None, "pending_start": False, "current": None, "blocked": False})
        want = quarter["car"] == "charge"
        status = driver.status()

        async def send(command: str, action, reason: str, **extra) -> bool:
            if not test:
                try:
                    await action()
                except Exception as err:  # noqa: BLE001 - a missing entity or a refused command must not stop the plugin
                    self.warn(f"Borne {name} : {command} refusé : {err}")
                    return False
            self.log(name, command, reason, test, status=status, **extra)
            return True

        def block(reason: str) -> None:
            state.update(blocked=True, pending_start=False, current=None)
            self.log(name, "attente débranchement", reason, test, status=status)

        # The car unplugged: a new cycle, from what the planning says now (a charge planned waits for the car)
        if driver.is_unplugged() and state["blocked"]:
            state.update(blocked=False, want=None, pending_start=False)
            self.log(name, "nouveau cycle", "Voiture débranchée", test, status=status)
        # The car wants no more energy: nothing more until it is unplugged
        if driver.is_done() and not state["blocked"]:
            block(f"Statut {status} : la voiture ne demande plus d'énergie, rien jusqu'au débranchement")
        if state["blocked"]:
            state["want"] = want
            return

        # On / off: only when the planning changes its mind (a manual change holds until then)
        if want != state["want"]:
            state["want"] = want
            state["pending_start"] = want
            if not want and driver.is_charging():
                await send("stop", driver.stop, quarter.get("carLabel") or "Pas de recharge prévue")
        if state["pending_start"]:
            if driver.is_running():
                state["pending_start"] = False
            elif driver.can_start():
                if await send("start", driver.start, "Charger VE"):
                    state["pending_start"] = False
            elif not driver.is_unplugged():
                # A status that does not allow a start: nothing more is tried until the car is unplugged (not plugged in yet:
                # the start waits for it)
                block(f"Statut {status} : pas de démarrage, rien jusqu'au débranchement")
                return
        if not want:
            state["current"] = None
            return

        # The current: on the solar surplus in "Solaire d'abord" (quarters planned on the sun), at the highest otherwise
        low = int(charger.get(CONF_CHARGER_MIN_CURRENT, DEFAULT_MIN_CURRENT))
        high = int(charger.get(CONF_CHARGER_MAX_CURRENT, DEFAULT_MAX_CURRENT))
        # Its phases: three for a three-phase station (6 A = 4.1 kW), one for a single-phase one, as planned for a switchable one
        kind = charger.get(CONF_CHARGER_PHASES)
        phases = 3 if kind == PHASES_TRI else 1 if kind == PHASES_MONO else int(quarter.get("carPhases") or 3)
        if (self.production_w() or 0) < NO_SUN_W:
            self.no_sun_since = self.no_sun_since or now
        else:
            self.no_sun_since = None
        no_sun = self.no_sun_since is not None and now - self.no_sun_since >= timedelta(minutes=NO_SUN_MINUTES)
        solar_first = data.get("carMode") == "solar_first"
        amperes, reason = high, "Pleine puissance"
        if solar_first and quarter.get("carSource") == "solar" and not no_sun:
            # What the car could take without buying: what it takes now, plus what goes to the grid, plus what charges the
            # battery (the car first in "Solaire d'abord"), minus what the battery gives (it must not feed the car)
            values = self.live_values()
            car_w = self.read_power_w(charger.get(CONF_CHARGER_POWER_ENTITY))
            if car_w is None:
                car_w = (driver.current_a() or 0) * PHASE_VOLTS * phases if driver.is_charging() else 0
            available = car_w - (values.get("grid") or 0) + (values.get("battery") or 0)
            amperes = max(low, min(high, int(available // (PHASE_VOLTS * phases))))
            reason = f"Surplus solaire {round(available)} W"
        elif no_sun and solar_first:
            reason = "Pas de soleil : pleine puissance"
        known = driver.current_a() if not test else state["current"]
        if known is None or abs(known - amperes) >= 1:
            if await send(f"{amperes} A", lambda: driver.set_current(amperes), reason, current_a=amperes):
                state["current"] = amperes
