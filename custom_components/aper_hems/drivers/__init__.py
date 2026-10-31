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
    # Started and stopped through its on/off switch (without one, it is not driven)
    NEEDS_SWITCH = True
    # Switches between one and three phases when told (set_phases)
    SWITCHES_PHASES = False

    def __init__(self, hass: HomeAssistant, charger: dict) -> None:
        self.hass = hass
        self.charger = charger
        self.switch = charger.get(CONF_CHARGER_SWITCH_ENTITY)
        self.current = charger.get(CONF_CHARGER_CURRENT_ENTITY)
        self.status_entity = charger.get(CONF_CHARGER_PLUGGED_ENTITY)

    def required_entities(self) -> list[tuple[str, str | None]]:
        """The entities it is driven through, with their name in the report of the test."""
        return [("interrupteur marche/arrêt", self.switch), ("consigne d'ampérage", self.current), ("statut", self.status_entity)]

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

    async def set_phases(self, phases: int) -> None:
        raise NotImplementedError


class WallboxDriver(ChargerDriver):
    """Wallbox (Pulsar Plus…) driven through OCPP: a charge may be started only when the connector is Preparing (the car
    plugged in, no transaction) or Finishing (the last transaction ended, the car still plugged in). In any other status, or
    once the car wants no more energy (SuspendedEV), nothing more is tried until the car is unplugged (Available)."""

    START_STATUSES = ("preparing", "finishing")
    CHARGING_STATUSES = ("charging", "suspendedev", "suspendedevse")
    RUNNING_STATUSES = ("charging",)
    UNPLUGGED_STATUSES = ("available",)
    DONE_STATUSES = ("suspendedev",)


class EaseeDriver(ChargerDriver):
    """Easee (Home, Charge…) driven through the actions of its Home Assistant integration (easee), on the device of its
    entities: paused and resumed (action_command), its current as the dynamic limit of the charger (set_charger_dynamic_limit,
    read on the sensor "dynamic_charger_limit"), its phases locked to one or three (set_charger_phase_mode). Its statuses
    (chargerOpMode): awaiting_start (the car plugged in, waiting: a charge may be started, or resumed after a pause),
    charging, ready_to_charge (the car takes no energy: SuspendedEV), completed (the car stopped charging), disconnected.
    Once the car wants no more energy (completed, or ready_to_charge for a while), nothing more is tried until the car is
    unplugged (disconnected)."""

    START_STATUSES = ("awaiting_start", "ready_to_charge", "awaiting_authorization", "awaiting_authentication")
    CHARGING_STATUSES = ("charging", "ready_to_charge")
    RUNNING_STATUSES = ("charging",)
    UNPLUGGED_STATUSES = ("disconnected",)
    DONE_STATUSES = ("completed", "ready_to_charge")
    NEEDS_SWITCH = False
    SWITCHES_PHASES = True
    # Its firmware waits about five minutes before a start at 6 A (as seen by evcc): started at 7 A at least
    START_MIN_CURRENT = 7

    def __init__(self, hass: HomeAssistant, charger: dict) -> None:
        super().__init__(hass, charger)
        self.device_id: str | None = None

    def required_entities(self) -> list[tuple[str, str | None]]:
        return [("consigne d'ampérage (capteur dynamic_charger_limit)", self.current), ("statut", self.status_entity)]

    def status(self) -> str | None:
        value = super().status()
        return value.replace(" ", "_").replace("-", "_") if value else None

    # Its switch (when chosen) only says whether the charger is enabled, not whether it charges: the status alone
    def is_charging(self) -> bool:
        return self.status() in self.CHARGING_STATUSES

    def is_running(self) -> bool:
        return self.status() in self.RUNNING_STATUSES

    def _device(self) -> str:
        """The device of the station in Home Assistant (its actions are called on it), from one of its entities."""
        if self.device_id is None:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(self.hass)
            for entity_id in (self.status_entity, self.current, self.switch):
                entry = registry.async_get(entity_id) if entity_id else None
                if entry is not None and entry.device_id:
                    self.device_id = entry.device_id
                    break
        if self.device_id is None:
            raise RuntimeError("appareil Easee introuvable : choisissez des entités de l'intégration Easee")
        return self.device_id

    async def _call(self, service: str, **data) -> None:
        await self.hass.services.async_call("easee", service, {"device_id": self._device(), **data}, blocking=True)

    async def start(self) -> None:
        # The charger disabled (its switch "is_enabled", when chosen): enabled first
        if self.switch and self.switch_on() is False:
            await super().start()
        known = self.current_a()
        if known is not None and known < self.START_MIN_CURRENT:
            await self.set_current(self.START_MIN_CURRENT)
        command = "start" if self.status() in ("awaiting_authorization", "awaiting_authentication") else "resume"
        await self._call("action_command", action_command=command)

    async def stop(self) -> None:
        await self._call("action_command", action_command="pause")

    async def set_current(self, amperes: int) -> None:
        await self._call("set_charger_dynamic_limit", current=amperes)

    async def set_phases(self, phases: int) -> None:
        await self._call("set_charger_phase_mode", phase_mode="1_phase" if phases == 1 else "3_phase")


DRIVERS: dict[str, type[ChargerDriver]] = {
    "wallbox": WallboxDriver,
    "easee": EaseeDriver,
}


def driver_for(hass: HomeAssistant, charger: dict, brand: str | None) -> ChargerDriver | None:
    """The driver of the brand of the station, None when there is none (or no switch to drive, for a brand that needs one)."""
    driver = DRIVERS.get(brand or "")
    if driver is None or (driver.NEEDS_SWITCH and not charger.get(CONF_CHARGER_SWITCH_ENTITY)):
        return None
    return driver(hass, charger)
