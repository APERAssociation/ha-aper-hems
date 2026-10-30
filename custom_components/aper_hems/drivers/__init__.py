"""Drivers of the charging stations: how to start, stop and set the current of each brand.

The plugin drives the station the planning of the HEMS charges the car on: started when the planning says "charge", stopped
otherwise, its current set every minute. Each brand has its own rules (which statuses allow a start); a brand without a
driver is not driven.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from ..const import (
    CONF_CHARGER_CURRENT_ENTITY,
    CONF_CHARGER_PLUGGED_ENTITY,
    CONF_CHARGER_SWITCH_ENTITY,
)


class ChargerDriver:
    """A station driven through its on/off switch and its current setting (a number entity, in A)."""

    # The statuses (as its status entity gives them, in lower case) in which a charge may be started; empty: any
    START_STATUSES: tuple[str, ...] = ()
    # The statuses of a charge in progress (started, the car taking energy or not), and of one actually charging
    CHARGING_STATUSES: tuple[str, ...] = ()
    RUNNING_STATUSES: tuple[str, ...] = ()
    # The statuses of a station without a car: the car was unplugged, a new cycle starts
    UNPLUGGED_STATUSES: tuple[str, ...] = ()
    # The statuses of a car that wants no more energy (full, or its own schedule): nothing more until it is unplugged
    DONE_STATUSES: tuple[str, ...] = ()

    def __init__(self, hass: HomeAssistant, charger: dict) -> None:
        self.hass = hass
        self.charger = charger
        self.switch = charger.get(CONF_CHARGER_SWITCH_ENTITY)
        self.current = charger.get(CONF_CHARGER_CURRENT_ENTITY)
        self.status_entity = charger.get(CONF_CHARGER_PLUGGED_ENTITY)

    def _state(self, entity_id):
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        return state.state

    def status(self) -> str | None:
        value = self._state(self.status_entity)
        return value.strip().lower() if value else None

    def switch_on(self) -> bool | None:
        value = self._state(self.switch)
        return None if value is None else value == "on"

    def current_a(self) -> float | None:
        value = self._state(self.current)
        try:
            return None if value is None else float(value)
        except ValueError:
            return None

    def can_start(self) -> bool:
        """Whether a charge may be started now (the car plugged in, no charge in progress)."""
        return not self.START_STATUSES or self.status() in self.START_STATUSES

    def is_unplugged(self) -> bool:
        return self.status() in self.UNPLUGGED_STATUSES

    def is_done(self) -> bool:
        return self.status() in self.DONE_STATUSES

    def is_charging(self) -> bool:
        """Whether a charge is in progress (to stop it)."""
        status = self.status()
        if self.CHARGING_STATUSES and status is not None:
            return status in self.CHARGING_STATUSES
        return bool(self.switch_on())

    def is_running(self) -> bool:
        """Whether it charges now (when a start is wanted: nothing to do)."""
        status = self.status()
        if self.RUNNING_STATUSES and status is not None:
            return status in self.RUNNING_STATUSES
        return bool(self.switch_on())

    async def start(self) -> None:
        await self.hass.services.async_call("switch", "turn_on", {"entity_id": self.switch}, blocking=True)

    async def stop(self) -> None:
        await self.hass.services.async_call("switch", "turn_off", {"entity_id": self.switch}, blocking=True)

    async def set_current(self, amperes: int) -> None:
        domain = self.current.split(".")[0]
        await self.hass.services.async_call(domain, "set_value", {"entity_id": self.current, "value": amperes}, blocking=True)


class WallboxDriver(ChargerDriver):
    """Wallbox (Pulsar Plus…) driven through OCPP: a charge may be started only when the connector is Preparing (the car
    plugged in, no transaction) or Finishing (the last transaction ended, the car still plugged in). In any other status, or
    once the car wants no more energy (SuspendedEV), nothing more is tried until the car is unplugged (Available)."""

    START_STATUSES = ("preparing", "finishing")
    CHARGING_STATUSES = ("charging", "suspendedev", "suspendedevse")
    RUNNING_STATUSES = ("charging",)
    UNPLUGGED_STATUSES = ("available",)
    DONE_STATUSES = ("suspendedev",)


DRIVERS: dict[str, type[ChargerDriver]] = {
    "wallbox": WallboxDriver,
}


def driver_for(hass: HomeAssistant, charger: dict, brand: str | None) -> ChargerDriver | None:
    """The driver of the brand of the station, None when there is none (or no switch to drive)."""
    driver = DRIVERS.get(brand or "")
    if driver is None or not charger.get(CONF_CHARGER_SWITCH_ENTITY):
        return None
    return driver(hass, charger)
