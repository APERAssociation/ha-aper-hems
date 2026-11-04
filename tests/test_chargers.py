"""Tests of the control of the charging stations (drivers/), without Home Assistant: a fake Wallbox that answers as the real
one (OCPP statuses, switch, current, power) and a fake planning.

    python -m unittest discover -s homeassistant/tests -v
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The plugin's package without its __init__.py (which needs Home Assistant): only the drivers and the constants are loaded
PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "aper_hems"
if "aper_hems" not in sys.modules:
    package = types.ModuleType("aper_hems")
    package.__path__ = [str(PACKAGE)]
    sys.modules["aper_hems"] = package

from aper_hems.drivers import EaseeDriver, WallboxDriver, driver_for  # noqa: E402
from aper_hems.drivers import check  # noqa: E402
from aper_hems.drivers.control import ChargerControl  # noqa: E402

SWITCH = "switch.charger_charge_control"
CURRENT = "number.charger_maximum_current"
STATUS = "sensor.charger_status_connector_2"
POWER = "sensor.charger_power"
NOW = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)


class State:
    def __init__(self, state):
        self.state = state


class FakeWallbox:
    """The station and Home Assistant around it: its entities, and the actions it answers (as the Pulsar Plus over OCPP)."""

    def __init__(self, status="Preparing", current=16, refuse=False, car_takes=True):
        self.status = status
        self.switch = "off"
        self.current = current
        self.refuse = refuse
        # A car that stops taking energy once started (full): SuspendedEV
        self.car_takes = car_takes
        self.calls: list[tuple[str, str, dict]] = []
        self.states = self
        self.services = self

    # hass.states
    def get(self, entity_id):
        value = {SWITCH: self.switch, CURRENT: str(self.current), STATUS: self.status, POWER: str(self.power())}.get(entity_id)
        return None if value is None else State(value)

    def power(self) -> float:
        return self.current * 230 * 3 if self.status == "Charging" else 0

    # hass.services
    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))
        if self.refuse:
            raise RuntimeError("refusé")
        if (domain, service) == ("switch", "turn_on"):
            self.switch = "on"
            if self.status in ("Preparing", "Finishing", "SuspendedEVSE"):
                self.status = "Charging" if self.car_takes else "SuspendedEV"
        elif (domain, service) == ("switch", "turn_off"):
            self.switch = "off"
            if self.status in ("Charging", "SuspendedEV", "SuspendedEVSE"):
                self.status = "Finishing"
        elif service == "set_value":
            self.current = int(data["value"])

    def sent(self) -> list[str]:
        """The commands, in words: start, stop, 9 A…"""
        words = []
        for domain, service, data in self.calls:
            words.append("start" if service == "turn_on" else "stop" if service == "turn_off" else f"{data['value']} A")
        return words


CHARGER = {"name": "Wallbox", "brand": "wallbox", "switch_entity": SWITCH, "current_entity": CURRENT,
           "plugged_entity": STATUS, "power_entity": POWER, "phases": "tri", "min_current": 6, "max_current": 16}


def planning(car="charge", source="grid", mode="immediate", generated=NOW, charger="Wallbox", phases=3, solar_allowed=False):
    """A planning whose quarter of an hour in progress says what the car does."""
    return {
        "generatedAt": generated.isoformat(), "chargerName": charger, "carMode": mode,
        "quarters": [{"start": (NOW - timedelta(minutes=15) + timedelta(minutes=15 * i)).isoformat(), "car": car,
                      "carSource": source, "carPhases": phases, "solarAllowed": solar_allowed,
                      "carLabel": "Charger VE" if car == "charge" else "En attente"}
                     for i in range(8)],
    }


EASEE_STATUS = "sensor.easee_status"
EASEE_CURRENT = "sensor.easee_dynamic_charger_limit"
EASEE_SWITCH = "switch.easee_is_enabled"


class FakeEasee:
    """An Easee and Home Assistant around it: its status (chargerOpMode), its dynamic limit, its "enabled" switch, and the
    actions of the Easee integration on its device."""

    def __init__(self, status="awaiting_start", current=16, car_takes=True, enabled="on", obeys_phases=True):
        self.status = status
        self.current = current
        self.car_takes = car_takes
        self.enabled = enabled
        self.phase_mode = "auto_phase"
        # A station that does not follow the phase mode stays on three phases
        self.obeys_phases = obeys_phases
        self.calls: list[tuple[str, str, dict]] = []
        self.states = self
        self.services = self

    def power(self) -> float:
        phases = 1 if self.phase_mode == "1_phase" and self.obeys_phases else 3
        return self.current * 400 / 3 ** 0.5 * phases if self.status == "charging" else 0

    def get(self, entity_id):
        value = {EASEE_STATUS: self.status, EASEE_CURRENT: str(self.current), EASEE_SWITCH: self.enabled}.get(entity_id)
        return None if value is None else State(value)

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))
        if (domain, service) == ("switch", "turn_on"):
            self.enabled = "on"
            return
        assert domain == "easee" and data["device_id"] == "easee-device"
        if service == "action_command":
            command = data["action_command"]
            if command in ("resume", "start") and self.status in ("awaiting_start", "ready_to_charge", "awaiting_authorization"):
                self.status = "charging" if self.car_takes else "ready_to_charge"
            elif command == "pause" and self.status in ("charging", "ready_to_charge"):
                self.status = "awaiting_start"
        elif service == "set_charger_dynamic_limit":
            self.current = int(data["current"])
        elif service == "set_charger_phase_mode":
            self.phase_mode = data["phase_mode"]

    def sent(self) -> list[str]:
        words = []
        for domain, service, data in self.calls:
            if service == "action_command":
                words.append(data["action_command"])
            elif service == "set_charger_dynamic_limit":
                words.append(f"{data['current']} A")
            elif service == "set_charger_phase_mode":
                words.append(data["phase_mode"])
            else:
                words.append(f"{domain}.{service}")
        return words


EASEE = {"name": "Easee", "brand": "easee", "switch_entity": EASEE_SWITCH, "current_entity": EASEE_CURRENT,
         "plugged_entity": EASEE_STATUS, "phases": "tri", "min_current": 6, "max_current": 16}


def easee_factory(hass, charger, brand):
    """The real driver, on the device of the fake station (Home Assistant's entity registry is not there)."""
    driver = driver_for(hass, charger, brand)
    if driver is not None:
        driver.device_id = "easee-device"
    return driver


class Harness:
    """The control on a fake station (a Wallbox unless told), with the sun and the grid it reads."""

    def __init__(self, station, production=0.0, grid=0.0, battery=0.0, grid_max_w=None, charger=None):
        self.station = station
        self.production = production
        self.grid = grid
        self.battery = battery
        self.log: list[tuple[str, str]] = []
        self.control = ChargerControl(
            station, [charger or CHARGER],
            live_values=lambda: {"grid": self.grid, "battery": self.battery},
            production_w=lambda: self.production,
            log=lambda name, command, reason, test, **extra: self.log.append((command, reason)),
            grid_max_w=grid_max_w, driver_factory=easee_factory)

    async def tick(self, data, minutes=0, test=False):
        await self.control.tick(data, NOW + timedelta(minutes=minutes), test)

    async def protect(self, data, minutes=0, test=False):
        await self.control.protect(data, NOW + timedelta(minutes=minutes), test)

    def commands(self) -> list[str]:
        return [command for command, _ in self.log]


class DriverTests(unittest.TestCase):
    def test_wallbox_starts_only_when_preparing_finishing_or_paused(self):
        for status, allowed in (("Preparing", True), ("Finishing", True), ("Available", False), ("Charging", False),
                                ("SuspendedEV", False), ("SuspendedEVSE", True), ("Faulted", False)):
            driver = WallboxDriver(FakeWallbox(status=status), CHARGER)
            self.assertEqual(driver.can_start(), allowed, status)

    def test_a_brand_without_driver_is_not_driven(self):
        self.assertIsNone(driver_for(FakeWallbox(), {**CHARGER, "brand": "zaptec"}, "zaptec"))
        self.assertIsNone(driver_for(FakeWallbox(), {**CHARGER, "switch_entity": None}, "wallbox"))

    def test_an_easee_needs_no_switch(self):
        self.assertIsInstance(driver_for(FakeEasee(), {**EASEE, "switch_entity": None}, "easee"), EaseeDriver)

    def test_easee_starts_when_awaiting_start(self):
        for status, allowed in (("awaiting_start", True), ("ready_to_charge", True), ("disconnected", False),
                                ("charging", False), ("completed", False), ("error", False)):
            driver = EaseeDriver(FakeEasee(status=status), EASEE)
            self.assertEqual(driver.can_start(), allowed, status)


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_starts_at_full_power_when_the_planning_charges(self):
        h = Harness(FakeWallbox(status="Preparing", current=6))
        await h.tick(planning("charge"))
        # Its current first: it starts at the current it had
        self.assertEqual(h.station.sent(), ["16 A", "start"])
        self.assertEqual(h.station.status, "Charging")

    async def test_stops_when_the_planning_stops(self):
        h = Harness(FakeWallbox(status="Preparing"))
        await h.tick(planning("charge"))
        await h.tick(planning("wait"), minutes=1)
        self.assertEqual(h.station.sent(), ["start", "stop"])
        self.assertEqual(h.station.status, "Finishing")

    async def test_restarts_from_finishing_at_the_next_slot(self):
        h = Harness(FakeWallbox(status="Preparing"))
        await h.tick(planning("charge"))
        await h.tick(planning("wait"), minutes=1)
        await h.tick(planning("charge"), minutes=2)
        self.assertEqual(h.station.sent(), ["start", "stop", "start"])

    async def test_does_nothing_more_while_the_planning_does_not_change(self):
        h = Harness(FakeWallbox(status="Preparing"))
        for minute in range(5):
            await h.tick(planning("charge"), minutes=minute)
        self.assertEqual(h.station.sent(), ["start"])

    async def test_a_manual_start_holds_until_the_planning_changes(self):
        h = Harness(FakeWallbox(status="Preparing"))
        await h.tick(planning("wait"))
        # Started from the app outside the slot
        h.station.status = "Charging"
        await h.tick(planning("wait"), minutes=1)
        self.assertEqual(h.station.sent(), [])

    async def test_waits_for_the_car_to_be_plugged_in(self):
        h = Harness(FakeWallbox(status="Available", current=6))
        await h.tick(planning("charge"))
        self.assertEqual(h.station.sent(), ["16 A"])
        h.station.status = "Preparing"
        await h.tick(planning("charge"), minutes=1)
        self.assertEqual(h.station.sent(), ["16 A", "start"])

    async def test_another_status_blocks_until_unplugged(self):
        h = Harness(FakeWallbox(status="Faulted"))
        await h.tick(planning("charge"))
        self.assertEqual(h.station.sent(), [])
        self.assertIn("attente débranchement", h.commands())
        # Nothing more, whatever the planning says
        await h.tick(planning("wait"), minutes=1)
        await h.tick(planning("charge"), minutes=2)
        self.assertEqual(h.station.sent(), [])
        # Unplugged, then plugged in: a new cycle, started
        h.station.status = "Available"
        await h.tick(planning("charge"), minutes=3)
        self.assertIn("nouveau cycle", h.commands())
        h.station.status = "Preparing"
        await h.tick(planning("charge"), minutes=4)
        self.assertIn("start", h.station.sent())

    async def test_a_car_that_wants_no_more_energy_blocks_until_unplugged(self):
        h = Harness(FakeWallbox(status="Preparing", current=16))
        await h.tick(planning("charge"))
        # The car full: SuspendedEV for 5 minutes
        h.station.status = "SuspendedEV"
        await h.tick(planning("charge"), minutes=1)
        self.assertNotIn("attente débranchement", h.commands())
        await h.tick(planning("charge"), minutes=6)
        self.assertIn("attente débranchement", h.commands())
        await h.tick(planning("wait"), minutes=7)
        await h.tick(planning("charge"), minutes=8)
        self.assertEqual(h.station.sent(), ["start"])
        h.station.status = "Available"
        await h.tick(planning("charge"), minutes=9)
        self.assertIn("nouveau cycle", h.commands())

    async def test_a_status_unknown_does_not_block(self):
        # Its status entity unavailable: the start tried all the same
        h = Harness(FakeWallbox(status="unavailable", current=16))
        await h.tick(planning("charge"))
        self.assertEqual(h.station.sent(), ["start"])
        self.assertNotIn("attente débranchement", h.commands())

    async def test_starts_again_after_a_pause_of_the_planning(self):
        # Stopped for a quarter of an hour by the planning: the Wallbox paused (SuspendedEVSE), started again after it
        h = Harness(FakeWallbox(status="Preparing", current=16))
        await h.tick(planning("charge"))
        await h.tick(planning("none"), minutes=1)
        h.station.status = "SuspendedEVSE"
        await h.tick(planning("charge"), minutes=2)
        self.assertEqual(h.station.sent(), ["start", "stop", "start"])
        self.assertNotIn("attente débranchement", h.commands())

    async def test_a_car_waking_up_after_the_start_is_not_blocked(self):
        h = Harness(FakeWallbox(status="Preparing", current=16))
        await h.tick(planning("charge"))
        # SuspendedEV for two minutes while the car wakes up, then Charging
        h.station.status = "SuspendedEV"
        await h.tick(planning("charge"), minutes=1)
        await h.tick(planning("charge"), minutes=2)
        h.station.status = "Charging"
        await h.tick(planning("charge"), minutes=3)
        await h.tick(planning("wait"), minutes=4)
        self.assertNotIn("attente débranchement", h.commands())
        self.assertEqual(h.station.sent(), ["start", "stop"])

    async def test_solar_first_follows_the_surplus(self):
        # Charging at 6 A (4157 W), 2000 W exported, the battery charging 1000 W: 7157 W → 10 A
        h = Harness(FakeWallbox(status="Charging", current=6), production=9000, grid=-2000, battery=1000)
        await h.tick(planning("charge", source="solar", mode="solar_first"))
        self.assertEqual(h.station.sent(), ["10 A"])

    async def test_solar_first_keeps_the_lowest_current_under_clouds(self):
        # On the sun: 5542 W taken (8 A) and 500 W exported
        h = Harness(FakeWallbox(status="Charging", current=8), production=7000, grid=-500)
        await h.tick(planning("charge", source="solar", mode="solar_first"))
        self.assertEqual(h.station.sent(), [])
        # A cloud: 1500 W bought, the battery giving 500 W: 3542 W → 5 A, kept at 6 A
        h.grid, h.battery = 1500, -500
        await h.tick(planning("charge", source="solar", mode="solar_first"), minutes=1)
        self.assertEqual(h.station.sent(), ["6 A"])

    async def test_solar_first_never_above_the_highest_current(self):
        h = Harness(FakeWallbox(status="Charging", current=16), production=20000, grid=-9000)
        await h.tick(planning("charge", source="solar", mode="solar_first"))
        self.assertEqual(h.station.sent(), [])

    async def test_a_quarter_planned_on_the_sun_without_sun_seen_live_does_not_charge(self):
        # The forecast said sun, the live says 4140 W bought: the planning is not followed, the live is
        h = Harness(FakeWallbox(status="Charging", current=6), production=0, grid=4140)
        await h.tick(planning("charge", source="solar", mode="solar_first"))
        self.assertEqual(h.station.sent(), ["stop"])

    async def test_from_the_grid_full_power(self):
        h = Harness(FakeWallbox(status="Charging", current=6), production=9000, grid=-2000)
        await h.tick(planning("charge", source="grid", mode="solar_first"))
        self.assertEqual(h.station.sent(), ["16 A"])

    async def test_after_a_restart_a_charge_in_progress_goes_on(self):
        # Home Assistant restarted in the middle of a slot: the station already charges
        h = Harness(FakeWallbox(status="Charging", current=16))
        await h.tick(planning("charge"))
        self.assertEqual(h.station.sent(), [])

    async def test_after_a_restart_a_charge_out_of_its_slot_is_stopped(self):
        h = Harness(FakeWallbox(status="Charging", current=16))
        await h.tick(planning("wait"))
        self.assertEqual(h.station.sent(), ["stop"])

    async def test_an_old_planning_changes_nothing(self):
        h = Harness(FakeWallbox(status="Preparing"))
        await h.tick(planning("charge", generated=NOW - timedelta(minutes=31)))
        self.assertEqual(h.station.sent(), [])

    async def test_another_station_is_not_driven(self):
        h = Harness(FakeWallbox(status="Preparing"))
        await h.tick(planning("charge", charger="Borne du voisin"))
        self.assertEqual(h.station.sent(), [])

    async def test_test_mode_sends_nothing_but_says_it(self):
        h = Harness(FakeWallbox(status="Preparing", current=6))
        await h.tick(planning("charge"), test=True)
        await h.tick(planning("charge"), minutes=1, test=True)
        self.assertEqual(h.station.sent(), [])
        self.assertEqual(h.commands(), ["16 A", "start"])

    async def test_a_start_the_station_did_not_follow_is_tried_again(self):
        # The command goes through, the station stays Preparing (its switch back off): started again two minutes later
        h = Harness(FakeWallbox(status="Preparing", current=16))
        await h.tick(planning("charge"))
        h.station.status, h.station.switch = "Preparing", "off"
        await h.tick(planning("charge"), minutes=1)
        self.assertEqual(h.station.sent(), ["start"])
        await h.tick(planning("charge"), minutes=2)
        self.assertEqual(h.station.sent(), ["start", "start"])
        self.assertIn("Nouvel essai", h.log[-1][1])
        self.assertEqual(h.station.status, "Charging")

    async def test_a_station_that_never_starts_is_left_after_five_tries(self):
        h = Harness(FakeWallbox(status="Preparing", current=16))
        for minute in range(0, 14):
            await h.tick(planning("charge"), minutes=minute)
            h.station.status, h.station.switch = "Preparing", "off"
        self.assertEqual(h.station.sent(), ["start"] * 5)
        self.assertIn("attente débranchement", h.commands())

    async def test_a_refused_start_is_tried_again(self):
        station = FakeWallbox(status="Preparing", refuse=True)
        h = Harness(station)
        await h.tick(planning("charge"))
        # Shown in the commands sensor, with why
        self.assertIn(("start refusé", "refusé"), h.log)
        station.refuse = False
        await h.tick(planning("charge"), minutes=1)
        self.assertEqual(station.sent(), ["start", "start"])
        self.assertEqual(station.status, "Charging")


class EaseeTests(unittest.IsolatedAsyncioTestCase):
    """An Easee: resumed and paused through its integration, its dynamic limit, its phases locked as planned."""

    def harness(self, station, **charger):
        return Harness(station, charger={**EASEE, **charger})

    async def test_resumes_at_full_power_when_the_planning_charges(self):
        h = self.harness(FakeEasee(status="awaiting_start", current=10))
        await h.tick(planning("charge", charger="Easee"))
        self.assertEqual(h.station.sent(), ["16 A", "resume"])
        self.assertEqual(h.station.status, "charging")

    async def test_a_start_at_6_a_goes_up_to_7_a_first(self):
        h = self.harness(FakeEasee(status="awaiting_start", current=6), max_current=6)
        await h.tick(planning("charge", charger="Easee"))
        self.assertEqual(h.station.sent(), ["7 A", "resume"])
        # Back to its current at the next minute
        await h.tick(planning("charge", charger="Easee"), minutes=1)
        self.assertEqual(h.station.sent(), ["7 A", "resume", "6 A"])

    async def test_pauses_when_the_planning_stops_and_resumes_at_the_next_slot(self):
        h = self.harness(FakeEasee(status="awaiting_start"))
        await h.tick(planning("charge", charger="Easee"))
        await h.tick(planning("wait", charger="Easee"), minutes=1)
        self.assertEqual(h.station.status, "awaiting_start")
        await h.tick(planning("charge", charger="Easee"), minutes=2)
        self.assertEqual(h.station.sent(), ["resume", "pause", "resume"])

    async def test_a_disabled_charger_is_enabled_first(self):
        h = self.harness(FakeEasee(status="awaiting_start", enabled="off"))
        await h.tick(planning("charge", charger="Easee"))
        self.assertEqual(h.station.sent(), ["switch.turn_on", "resume"])

    async def test_a_completed_charge_waits_until_unplugged(self):
        h = self.harness(FakeEasee(status="completed"))
        await h.tick(planning("charge", charger="Easee"))
        self.assertEqual(h.station.sent(), [])
        self.assertIn("attente débranchement", h.commands())
        await h.tick(planning("charge", charger="Easee"), minutes=1)
        self.assertEqual(h.station.sent(), [])
        h.station.status = "disconnected"
        await h.tick(planning("charge", charger="Easee"), minutes=2)
        self.assertIn("nouveau cycle", h.commands())
        h.station.status = "awaiting_start"
        await h.tick(planning("charge", charger="Easee"), minutes=3)
        self.assertIn("resume", h.station.sent())

    async def test_a_car_that_stays_ready_to_charge_blocks_until_unplugged(self):
        h = self.harness(FakeEasee(status="awaiting_start", car_takes=False))
        await h.tick(planning("charge", charger="Easee"))
        self.assertEqual(h.station.status, "ready_to_charge")
        await h.tick(planning("charge", charger="Easee"), minutes=1)
        self.assertNotIn("attente débranchement", h.commands())
        await h.tick(planning("charge", charger="Easee"), minutes=6)
        self.assertIn("attente débranchement", h.commands())

    async def test_a_switchable_easee_takes_the_phases_of_the_planning(self):
        h = self.harness(FakeEasee(status="awaiting_start"), phases="mono_tri")
        # One phase planned (the sun is weak): locked to one phase before the start
        await h.tick(planning("charge", charger="Easee", phases=1))
        self.assertEqual(h.station.sent(), ["1_phase", "resume"])
        # Then three: the station switches by itself, the charge is not paused
        await h.tick(planning("charge", charger="Easee", phases=3), minutes=1)
        self.assertEqual(h.station.sent()[2:], ["3_phase"])
        self.assertEqual((h.station.phase_mode, h.station.status), ("3_phase", "charging"))
        # Nothing more while they stay the same
        await h.tick(planning("charge", charger="Easee", phases=3), minutes=2)
        self.assertEqual(len(h.station.sent()), 3)

    async def test_a_three_phase_easee_is_not_switched(self):
        h = self.harness(FakeEasee(status="awaiting_start"))
        await h.tick(planning("charge", charger="Easee", phases=1))
        self.assertNotIn("1_phase", h.station.sent())

    async def test_solar_surplus_on_one_phase(self):
        # One phase at 6 A (1386 W), 1000 W exported: 2386 W → 10 A on one phase
        h = Harness(FakeEasee(status="charging", current=6), production=4000, grid=-1000,
                    charger={**EASEE, "phases": "mono_tri"})
        h.control.stations["Easee"] = {"want": True, "pending_start": False, "current": None, "blocked": False,
                                       "done_since": None, "manual": None, "phases": 1, "sun": False, "sun_since": None,
                                       "sun_low_since": None}
        await h.tick(planning("charge", source="solar", mode="solar_first", charger="Easee", phases=1))
        self.assertEqual(h.station.sent(), ["10 A"])


class GapTests(unittest.IsolatedAsyncioTestCase):
    """A planned charge going on, the planning waiting for a single quarter of an hour (the battery taking the connection)."""

    @staticmethod
    def gap(next_car):
        data = planning("charge")
        data["quarters"][1].update(car="wait", carSource="none", carLabel="En attente")
        data["quarters"][2].update(car=next_car, carSource="grid" if next_car == "charge" else "none")
        return data

    async def test_a_single_quarter_waiting_does_not_stop_a_charge(self):
        h = Harness(FakeWallbox("Preparing"))
        await h.tick(planning("charge"))
        self.assertEqual(h.station.status, "Charging")
        await h.tick(self.gap("charge"), minutes=1)
        self.assertNotIn("stop", h.commands())
        self.assertEqual(h.station.status, "Charging")

    async def test_it_stops_when_the_planning_waits_longer(self):
        h = Harness(FakeWallbox("Preparing"))
        await h.tick(planning("charge"))
        await h.tick(self.gap("wait"), minutes=1)
        self.assertIn(("stop", "En attente"), h.log)


class PlugInTests(unittest.IsolatedAsyncioTestCase):
    """A car plugged in outside the planning: the station starting by itself is stopped, the planning asked again."""

    def waiting(self):
        return planning("wait", source="none", mode="best_price", charger="Easee", phases=3)

    async def test_a_station_charging_by_itself_once_plugged_in_is_stopped(self):
        h = Harness(FakeEasee(status="disconnected", current=16), charger=EASEE)
        self.assertFalse(await h.control.tick(self.waiting(), NOW, False))
        # Plugged in: the station charges at once by itself, the planning wants nothing now
        h.station.status = "charging"
        self.assertTrue(await h.control.tick(self.waiting(), NOW + timedelta(minutes=1), False))
        self.assertIn("pause", h.station.sent())
        self.assertEqual(h.station.status, "awaiting_start")

    async def test_also_when_it_starts_a_little_after(self):
        h = Harness(FakeEasee(status="disconnected", current=16), charger=EASEE)
        await h.tick(self.waiting())
        h.station.status = "awaiting_start"
        await h.tick(self.waiting(), minutes=1)
        self.assertNotIn("pause", h.station.sent())
        # Two minutes later it starts by itself
        h.station.status = "charging"
        await h.tick(self.waiting(), minutes=3)
        self.assertIn("pause", h.station.sent())
        self.assertIn(("stop", "La borne a démarré seule au branchement : En attente"), h.log)

    async def test_a_charge_started_by_hand_later_holds(self):
        h = Harness(FakeEasee(status="disconnected", current=16), charger=EASEE)
        await h.tick(self.waiting())
        h.station.status = "awaiting_start"
        await h.tick(self.waiting(), minutes=1)
        # Started from its app half an hour later: not stopped
        h.station.status = "charging"
        await h.tick(self.waiting(), minutes=30)
        await h.tick(self.waiting(), minutes=31)
        self.assertNotIn("pause", h.station.sent())

    async def test_a_charge_planned_is_not_stopped(self):
        h = Harness(FakeEasee(status="disconnected", current=16), charger=EASEE)
        await h.tick(planning("charge", charger="Easee"))
        h.station.status = "charging"
        await h.tick(planning("charge", charger="Easee"), minutes=1)
        self.assertNotIn("pause", h.station.sent())


class UnplannedSunTests(unittest.IsolatedAsyncioTestCase):
    """"Solaire d'abord" or "Plein au bon prix", whatever the planning says: the surplus seen live is charged."""

    def waiting(self):
        return planning("wait", source="none", mode="solar_first", charger="Easee", phases=0, solar_allowed=True)

    async def test_starts_once_the_surplus_has_lasted(self):
        # 2000 W exported: 8 A on one phase (1847 W) for a station that switches
        h = Harness(FakeEasee(status="awaiting_start", current=0), production=3000, grid=-2000, charger={**EASEE, "phases": "mono_tri"})
        for minute in range(3):
            await h.tick(self.waiting(), minutes=minute)
        self.assertEqual(h.station.sent(), [])
        await h.tick(self.waiting(), minutes=3)
        self.assertEqual(h.station.sent(), ["1_phase", "8 A", "resume"])
        self.assertIn(("start", "Surplus solaire imprévu"), h.log)

    async def test_a_big_surplus_is_charged_on_three_phases(self):
        # 6000 W exported: three phases (6 A × 3 = 4157 W fits), 8 A (5543 W)
        h = Harness(FakeEasee(status="awaiting_start", current=0), production=7000, grid=-6000, charger={**EASEE, "phases": "mono_tri"})
        for minute in range(4):
            await h.tick(self.waiting(), minutes=minute)
        self.assertEqual(h.station.sent(), ["3_phase", "8 A", "resume"])

    async def test_from_three_phases_to_one_soon_when_three_buy_from_the_grid(self):
        # Three phases at 6 A (4157 W) on the sun, a cloud: 2000 W bought, 2157 W of sun, enough for one phase only
        h = Harness(FakeEasee(status="awaiting_start", current=0), production=7000, grid=-6000, charger={**EASEE, "phases": "mono_tri"})
        for minute in range(4):
            await h.tick(self.waiting(), minutes=minute)
        self.assertEqual(h.station.phase_mode, "3_phase")
        h.station.calls.clear()
        h.station.current, h.grid = 6, 2000
        await h.tick(self.waiting(), minutes=4)
        self.assertEqual(h.station.phase_mode, "3_phase")
        await h.tick(self.waiting(), minutes=5)
        self.assertEqual(h.station.sent()[:1], ["1_phase"])
        self.assertEqual((h.station.phase_mode, h.station.status), ("1_phase", "charging"))
        self.assertNotIn("pause", h.station.sent())

    async def test_from_one_phase_to_three_once_the_sun_has_grown_for_a_while(self):
        h = Harness(FakeEasee(status="awaiting_start", current=0), production=3000, grid=-2000, charger={**EASEE, "phases": "mono_tri"})
        for minute in range(4):
            await h.tick(self.waiting(), minutes=minute)
        self.assertEqual(h.station.phase_mode, "1_phase")
        # The sun grows: the car (8 A on one phase, 1847 W) and 4000 W exported, 5847 W in all
        h.grid, h.production = -4000, 7000
        for minute in range(4, 9):
            await h.tick(self.waiting(), minutes=minute)
        self.assertEqual(h.station.phase_mode, "1_phase")
        await h.tick(self.waiting(), minutes=9)
        self.assertEqual(h.station.phase_mode, "3_phase")
        # Switched by the station itself, the charge not paused
        self.assertNotIn("pause", h.station.sent())

    async def test_a_charge_on_the_sun_is_not_started_again_soon_after_a_stop(self):
        # On the sun, then a cloud: stopped after 5 minutes; the sun back at once, but no start before 15 minutes after the stop
        h = Harness(FakeEasee(status="awaiting_start", current=0), production=7000, grid=-6000, charger={**EASEE, "phases": "mono_tri"})
        for minute in range(4):
            await h.tick(self.waiting(), minutes=minute)
        self.assertEqual(h.station.status, "charging")
        h.production, h.grid = 500, 3000
        minute = 4
        while h.station.status == "charging" and minute < 20:
            await h.tick(self.waiting(), minutes=minute)
            minute += 1
        stopped = minute - 1
        self.assertEqual(h.station.status, "awaiting_start")
        h.production, h.grid = 7000, -6000
        for later in range(stopped + 1, stopped + 15):
            await h.tick(self.waiting(), minutes=later)
        self.assertEqual(h.station.status, "awaiting_start")
        await h.tick(self.waiting(), minutes=stopped + 15)
        self.assertEqual(h.station.status, "charging")

    async def test_no_surplus_at_night_from_a_car_taking_less_than_its_setting(self):
        # Night, the car still charging (11.5 kW bought): its Easee set at 32 A on three phases (22 kW from its setting) while
        # its own charger takes 11 kW. Without the sun, what it takes is bought, never a surplus of 10 kW
        h = Harness(FakeEasee(status="charging", current=32), production=0, grid=11500, charger={**EASEE, "phases": "mono_tri"})
        night = planning("wait", source="none", mode="solar_first", charger="Easee", phases=3, solar_allowed=True)
        for minute in range(8):
            h.station.status = "charging"
            await h.tick(night, minutes=minute)
        self.assertEqual([reason for _, reason in h.log if reason.startswith("Surplus solaire")], [])

    async def test_also_in_best_price(self):
        h = Harness(FakeEasee(status="awaiting_start", current=0), production=3000, grid=-2000, charger={**EASEE, "phases": "mono_tri"})
        for minute in range(4):
            await h.tick(planning("wait", source="none", mode="best_price", charger="Easee", phases=0), minutes=minute)
        self.assertEqual(h.station.sent(), ["1_phase", "8 A", "resume"])

    async def test_not_in_immediate(self):
        h = Harness(FakeEasee(status="awaiting_start", current=0), production=3000, grid=-2000, charger={**EASEE, "phases": "mono_tri"})
        for minute in range(6):
            await h.tick(planning("wait", source="none", mode="immediate", charger="Easee", phases=0), minutes=minute)
        self.assertEqual(h.station.sent(), [])

    async def test_a_charge_from_the_grid_goes_on_at_once_on_the_sun(self):
        h = Harness(FakeEasee(status="awaiting_start", current=6), production=3000, grid=-2000, charger={**EASEE, "phases": "mono_tri"})
        await h.tick(planning("charge", source="grid", mode="solar_first", charger="Easee", phases=1))
        self.assertEqual(h.station.status, "charging")
        # The next quarter, without a charge from the grid: the surplus there, it goes on without a stop
        h.grid = -300
        await h.tick(self.waiting(), minutes=1)
        self.assertNotIn("pause", h.station.sent())

    async def test_stops_after_five_minutes_without_enough_sun(self):
        h = Harness(FakeEasee(status="awaiting_start", current=0), production=3000, grid=-2000, charger={**EASEE, "phases": "mono_tri"})
        for minute in range(4):
            await h.tick(self.waiting(), minutes=minute)
        self.assertEqual(h.station.status, "charging")
        # A cloud: the car (8 A, 1847 W) now buys 1500 W
        h.grid = 1500
        for minute in range(4, 9):
            await h.tick(self.waiting(), minutes=minute)
        self.assertNotIn("pause", h.station.sent())
        await h.tick(self.waiting(), minutes=9)
        # Paused, then its phases left to it again (a charge by hand from its app on three phases, as before)
        self.assertEqual(h.station.sent()[-2:], ["pause", "auto_phase"])
        self.assertIn(("stop", "Surplus solaire sous le courant minimal depuis 5 min"), h.log)
        self.assertEqual(h.station.phase_mode, "auto_phase")

    async def test_phases_left_to_the_station_when_the_car_is_unplugged(self):
        h = Harness(FakeEasee(status="awaiting_start", current=6), production=3000, grid=-2000, charger={**EASEE, "phases": "mono_tri"})
        for minute in range(4):
            await h.tick(self.waiting(), minutes=minute)
        self.assertEqual(h.station.phase_mode, "1_phase")
        h.station.status = "disconnected"
        await h.tick(self.waiting(), minutes=4)
        self.assertEqual(h.station.phase_mode, "auto_phase")


class GridLimitTests(unittest.IsolatedAsyncioTestCase):
    """The connection's limit: 12 kW here; a three-phase car takes 692.8 W per ampere (16 A ≈ 11.1 kW)."""

    async def test_lowers_the_car_when_the_import_goes_above(self):
        # 16 A (11085 W) and the house: 13000 W bought, 1000 W too many → 14 A
        h = Harness(FakeWallbox(status="Charging", current=16), grid=13000, grid_max_w=12000)
        await h.tick(planning("charge"))
        self.assertEqual(h.station.sent(), ["14 A"])
        self.assertIn("Raccordement 12 kW", h.log[-1][1])

    async def test_raises_it_again_when_the_house_takes_less(self):
        h = Harness(FakeWallbox(status="Charging", current=16), grid=13000, grid_max_w=12000)
        await h.tick(planning("charge"))
        # The oven off: 14 A (9699 W) + 500 W
        h.grid = 10199
        await h.tick(planning("charge"), minutes=1)
        self.assertEqual(h.station.sent(), ["14 A", "16 A"])

    async def test_raises_only_with_a_margin(self):
        h = Harness(FakeWallbox(status="Charging", current=14), grid=11800, grid_max_w=12000)
        # 200 W left: 15 A would take 693 W more, not raised
        await h.tick(planning("charge"))
        self.assertEqual(h.station.sent(), [])

    async def test_never_below_the_lowest_current(self):
        h = Harness(FakeWallbox(status="Charging", current=16), grid=20000, grid_max_w=12000)
        await h.tick(planning("charge"))
        self.assertEqual(h.station.sent(), ["6 A"])

    async def test_a_start_is_set_under_the_limit_before_it_starts(self):
        # The house takes 5000 W: 7000 W left → 10 A (6928 W) before the start, nothing more once the grid shows the car
        h = Harness(FakeWallbox(status="Preparing", current=16), grid=5000, grid_max_w=12000)
        await h.tick(planning("charge"))
        self.assertEqual(h.station.sent(), ["10 A", "start"])
        h.grid = 5000 + 6928
        await h.protect(planning("charge"), minutes=0.1)
        await h.tick(planning("charge"), minutes=1)
        self.assertEqual(h.station.sent(), ["10 A", "start"])

    async def test_a_solar_start_takes_the_surplus_current_first(self):
        # 5000 W exported for 3 minutes, nothing charging yet: 5000 W ÷ 692.8 W per ampere → 7 A, set before the start (not
        # 16 A for a minute)
        h = Harness(FakeWallbox(status="Preparing", current=16), production=6000, grid=-5000)
        for minute in range(4):
            await h.tick(planning("wait", source="none", mode="solar_first"), minutes=minute)
        self.assertEqual(h.station.sent(), ["7 A", "start"])

    async def test_protect_lowers_at_once_between_two_minutes(self):
        h = Harness(FakeWallbox(status="Charging", current=16), grid=11000, grid_max_w=12000)
        await h.tick(planning("charge"))
        self.assertEqual(h.station.sent(), [])
        # The heat pump starts: 13500 W
        h.grid = 13500
        await h.protect(planning("charge"), minutes=0.2)
        self.assertEqual(h.station.sent(), ["13 A"])
        # It never raises
        h.grid = 8000
        await h.protect(planning("charge"), minutes=0.3)
        self.assertEqual(h.station.sent(), ["13 A"])

    async def test_a_charge_started_by_hand_is_protected_and_given_back(self):
        h = Harness(FakeWallbox(status="Charging", current=16), grid=11000, grid_max_w=12000)
        await h.tick(planning("wait"), minutes=0)
        h.station.calls.clear()
        h.station.status, h.station.current = "Charging", 16
        h.grid = 13500
        await h.protect(planning("wait"), minutes=0.5)
        self.assertEqual(h.station.sent(), ["13 A"])
        h.grid = 9500
        await h.tick(planning("wait"), minutes=1)
        self.assertEqual(h.station.sent(), ["13 A", "16 A"])

    async def test_solar_first_stays_on_the_surplus(self):
        h = Harness(FakeWallbox(status="Charging", current=6), production=9000, grid=-2000, battery=1000, grid_max_w=12000)
        await h.tick(planning("charge", source="solar", mode="solar_first"))
        self.assertEqual(h.station.sent(), ["10 A"])


class CheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        check.POLL_SECONDS = 0
        check.STEP_TIMEOUT_SECONDS = 0.05
        check.POWER_TIMEOUT_SECONDS = 0.05

    async def run_check(self, station: FakeWallbox):
        driver = WallboxDriver(station, CHARGER)
        return await check.run_check(station, driver, "Wallbox", 6, 10, 3, lambda: station.power())

    async def test_a_station_that_follows_passes_every_step(self):
        station = FakeWallbox(status="Preparing", current=16)
        steps = await self.run_check(station)
        self.assertNotIn(False, [ok for ok, _ in steps])
        self.assertEqual(station.sent(), ["6 A", "start", "10 A", "6 A", "stop", "16 A"])
        self.assertEqual(station.current, 16)

    async def test_stops_when_the_car_is_not_plugged_in(self):
        station = FakeWallbox(status="Available")
        steps = await self.run_check(station)
        self.assertFalse(steps[-1][0])
        self.assertEqual(station.sent(), [])

    async def test_a_car_waking_up_through_suspendedev_passes(self):
        station = FakeWallbox(status="Preparing", current=16)
        driver = WallboxDriver(station, CHARGER)
        start = driver.start

        async def waking_start():
            await start()
            # SuspendedEV first, Charging a moment later
            station.status = "SuspendedEV"
            asyncio.get_running_loop().call_later(0.01, lambda: setattr(station, "status", "Charging"))

        driver.start = waking_start
        steps = await check.run_check(station, driver, "Wallbox", 6, 10, 3, lambda: station.power())
        self.assertNotIn(False, [ok for ok, _ in steps])

    async def test_a_late_power_sensor_is_said_not_failed(self):
        station = FakeWallbox(status="Preparing", current=16)
        driver = WallboxDriver(station, CHARGER)
        last = {"w": 0.0}

        def late_power():
            # The cloud sensor: keeps the power of the charge after the stop
            if station.status == "Charging":
                last["w"] = station.power()
            return last["w"]

        steps = await check.run_check(station, driver, "Wallbox", 6, 10, 3, late_power)
        self.assertNotIn(False, [ok for ok, _ in steps])
        self.assertIn("en retard", steps[-2][1])

    async def test_an_easee_passes_every_step_without_a_switch(self):
        station = FakeEasee(status="awaiting_start", current=16)
        driver = EaseeDriver(station, {**EASEE, "switch_entity": None})
        driver.device_id = "easee-device"
        steps = await check.run_check(station, driver, "Easee", 6, 10, 3, lambda: None)
        self.assertNotIn(False, [ok for ok, _ in steps])
        # Started at 7 A (its firmware), back to 6 A
        self.assertEqual(station.sent(), ["6 A", "7 A", "resume", "6 A", "10 A", "6 A", "pause", "16 A"])

    async def test_a_switchable_easee_is_tested_on_one_phase_then_three(self):
        station = FakeEasee(status="awaiting_start", current=0)
        driver = EaseeDriver(station, {**EASEE, "phases": "mono_tri"})
        driver.device_id = "easee-device"
        steps = await check.run_check(station, driver, "Easee", 6, 10, 3, station.power, switchable=True)
        self.assertNotIn(False, [ok for ok, _ in steps], steps)
        sent = station.sent()
        self.assertEqual(sent[0], "1_phase")
        self.assertIn("3_phase", sent)
        # Switched while it charges: the only pause is the stop at the end
        self.assertLess(sent.index("3_phase"), sent.index("pause"))
        self.assertEqual(sent.count("pause"), 1)
        # Left choosing its phases, its current as it was
        self.assertEqual(sent[-2:], ["0 A", "auto_phase"])
        self.assertTrue(any("1386 W attendus" in text for _, text in steps))
        self.assertTrue(any("4157 W attendus" in text for _, text in steps))

    async def test_an_easee_that_does_not_switch_phases_fails(self):
        station = FakeEasee(status="awaiting_start", current=0, obeys_phases=False)
        driver = EaseeDriver(station, {**EASEE, "phases": "mono_tri"})
        driver.device_id = "easee-device"
        steps = await check.run_check(station, driver, "Easee", 6, 10, 3, station.power, switchable=True)
        self.assertFalse(steps[-2][0])
        self.assertIn("n'a pas changé de phases", steps[-2][1])
        self.assertEqual(station.status, "awaiting_start")

    async def test_says_when_the_car_wants_no_energy(self):
        station = FakeWallbox(status="Preparing", car_takes=False)
        steps = await self.run_check(station)
        self.assertIn("ne demande pas d'énergie", " ".join(text for _, text in steps))
        # The charge stopped at the end
        self.assertEqual(station.status, "Finishing")


if __name__ == "__main__":
    unittest.main()
