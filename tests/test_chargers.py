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

from aper_hems.drivers import WallboxDriver, driver_for  # noqa: E402
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
            if self.status in ("Preparing", "Finishing"):
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


def planning(car="charge", source="grid", mode="best_price", generated=NOW, charger="Wallbox"):
    """A planning whose quarter of an hour in progress says what the car does."""
    return {
        "generatedAt": generated.isoformat(), "chargerName": charger, "carMode": mode,
        "quarters": [{"start": (NOW - timedelta(minutes=15) + timedelta(minutes=15 * i)).isoformat(), "car": car,
                      "carSource": source, "carPhases": 3, "carLabel": "Charger VE" if car == "charge" else "En attente"}
                     for i in range(8)],
    }


class Harness:
    """The control on a fake Wallbox, with the sun and the grid it reads."""

    def __init__(self, station: FakeWallbox, production=0.0, grid=0.0, battery=0.0):
        self.station = station
        self.production = production
        self.grid = grid
        self.battery = battery
        self.log: list[tuple[str, str]] = []
        self.control = ChargerControl(
            station, [CHARGER],
            read_power_w=lambda entity: float(station.get(entity).state) if entity and station.get(entity) else None,
            live_values=lambda: {"grid": self.grid, "battery": self.battery},
            production_w=lambda: self.production,
            log=lambda name, command, reason, test, **extra: self.log.append((command, reason)))

    async def tick(self, data, minutes=0, test=False):
        await self.control.tick(data, NOW + timedelta(minutes=minutes), test)

    def commands(self) -> list[str]:
        return [command for command, _ in self.log]


class DriverTests(unittest.TestCase):
    def test_wallbox_starts_only_when_preparing_or_finishing(self):
        for status, allowed in (("Preparing", True), ("Finishing", True), ("Available", False), ("Charging", False),
                                ("SuspendedEV", False), ("SuspendedEVSE", False), ("Faulted", False)):
            driver = WallboxDriver(FakeWallbox(status=status), CHARGER)
            self.assertEqual(driver.can_start(), allowed, status)

    def test_a_brand_without_driver_is_not_driven(self):
        self.assertIsNone(driver_for(FakeWallbox(), {**CHARGER, "brand": "easee"}, "easee"))
        self.assertIsNone(driver_for(FakeWallbox(), {**CHARGER, "switch_entity": None}, "wallbox"))


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_starts_at_full_power_when_the_planning_charges(self):
        h = Harness(FakeWallbox(status="Preparing", current=6))
        await h.tick(planning("charge"))
        self.assertEqual(h.station.sent(), ["start", "16 A"])
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
        h = Harness(FakeWallbox(status="SuspendedEVSE"))
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
        # Charging at 6 A (4140 W), 2000 W exported, the battery charging 1000 W: 7140 W → 10 A
        h = Harness(FakeWallbox(status="Charging", current=6), production=9000, grid=-2000, battery=1000)
        await h.tick(planning("charge", source="solar", mode="solar_first"))
        self.assertEqual(h.station.sent(), ["10 A"])

    async def test_solar_first_keeps_the_lowest_current_under_clouds(self):
        # 5520 W taken (8 A), 1500 W bought, the battery giving 500 W: 3520 W → 5 A, kept at 6 A
        h = Harness(FakeWallbox(status="Charging", current=8), production=3000, grid=1500, battery=-500)
        await h.tick(planning("charge", source="solar", mode="solar_first"))
        self.assertEqual(h.station.sent(), ["6 A"])

    async def test_solar_first_never_above_the_highest_current(self):
        h = Harness(FakeWallbox(status="Charging", current=16), production=20000, grid=-9000)
        await h.tick(planning("charge", source="solar", mode="solar_first"))
        self.assertEqual(h.station.sent(), [])

    async def test_without_sun_for_ten_minutes_full_power(self):
        h = Harness(FakeWallbox(status="Charging", current=6), production=0, grid=4140)
        data = planning("charge", source="solar", mode="solar_first")
        await h.tick(data)
        self.assertEqual(h.station.sent(), [])
        await h.tick(data, minutes=10)
        self.assertEqual(h.station.sent(), ["16 A"])
        self.assertEqual(h.log[-1][1], "Pas de soleil : pleine puissance")

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
        self.assertEqual(h.commands(), ["start", "16 A"])

    async def test_a_refused_start_is_tried_again(self):
        station = FakeWallbox(status="Preparing", refuse=True)
        h = Harness(station)
        await h.tick(planning("charge"))
        station.refuse = False
        await h.tick(planning("charge"), minutes=1)
        self.assertEqual(station.sent(), ["start", "start"])
        self.assertEqual(station.status, "Charging")


class CheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        check.POLL_SECONDS = 0
        check.STEP_TIMEOUT_SECONDS = 0.05

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

    async def test_says_when_the_car_wants_no_energy(self):
        station = FakeWallbox(status="Preparing", car_takes=False)
        steps = await self.run_check(station)
        self.assertIn("ne demande pas d'énergie", " ".join(text for _, text in steps))
        # The charge stopped at the end
        self.assertEqual(station.status, "Finishing")


if __name__ == "__main__":
    unittest.main()
