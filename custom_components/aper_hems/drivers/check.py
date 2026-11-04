"""A test of a charging station, started by the member (action "aper_hems.test_charger"): the real station is driven step by
step with the driver of its brand, each step checked on its entities, and the report shown in a notification.

The car must be plugged in and not charging. Steps: the entities, the status, 6 A, start, a higher current, back to the
lowest, stop, and its current put back as it was. A station that switches between one and three phases (Mono/Tri, a driver
that can) starts on one phase, then is switched to three while it charges (the station switches by itself), its power checked on each, and is left
choosing its phases itself. Every step waits for the station to follow (its cloud or OCPP can take a while), and the test
stops at the first step that fails, the charge stopped.
"""

from __future__ import annotations

import asyncio
import time

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from ..const import PHASE_VOLTS
from . import ChargerDriver

# How long a step waits for the station to follow, and how often it looks
STEP_TIMEOUT_SECONDS = 90
POLL_SECONDS = 5
# How long a power is waited for: a sensor read from the cloud (Easee) is a minute or two late
POWER_TIMEOUT_SECONDS = 180
# How much the power must rise between the lowest current and the higher one (W): the car may take less than asked
POWER_RISE_W = 1000
# The power of a current on a number of phases, within this share of what it should be: a late sensor still showing the charge
# before (higher or lower) does not pass
POWER_LOW_SHARE = 0.5
POWER_HIGH_SHARE = 1.5


async def run_check(hass: HomeAssistant, driver: ChargerDriver, name: str, low: int, high: int, phases: int,
                    power_w, switchable: bool = False) -> list[tuple[bool | None, str]]:
    """Runs the test; returns its steps: (True passed, False failed, None skipped or for information, text). switchable: a
    station that switches between one and three phases, tested on both."""
    steps: list[tuple[bool | None, str]] = []
    started = time.monotonic()
    switchable = switchable and driver.SWITCHES_PHASES

    def say(ok: bool | None, text: str) -> None:
        steps.append((ok, f"{round(time.monotonic() - started):>3} s · {text}"))

    async def wait(test, timeout=STEP_TIMEOUT_SECONDS) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if test():
                return True
            await asyncio.sleep(POLL_SECONDS)
        return test()

    def power() -> float | None:
        return power_w()

    def expected(amperes: int, on: int) -> float:
        return amperes * PHASE_VOLTS * on

    def near(amperes: int, on: int) -> bool:
        value = power()
        return value is not None and POWER_LOW_SHARE * expected(amperes, on) <= value <= POWER_HIGH_SHARE * expected(amperes, on)

    def words(on: int) -> str:
        return "monophasé" if on == 1 else "triphasé"

    # 1. The entities
    required = driver.required_entities()
    missing = [label for label, entity in required if not entity]
    if missing:
        say(False, "Entités manquantes dans le plugin : " + ", ".join(missing))
        return steps
    say(True, "Entités : " + ", ".join(f"{label} {entity}" for label, entity in required))
    if power() is None:
        say(None, "Pas de capteur de puissance : la puissance ne sera pas vérifiée")
    before = driver.current_a()

    def switch_text() -> str:
        return f", interrupteur {'marche' if driver.switch_on() else 'arrêt'}" if driver.switch else ""

    say(None, f"Statut {driver.status()}{switch_text()}, consigne {before} A")

    # 2. A status that allows a start
    if driver.is_charging():
        say(False, f"La borne charge déjà (statut {driver.status()}) : arrêtez la charge, puis relancez le test")
        return steps
    if not driver.can_start():
        say(False, f"Statut {driver.status()} : pas de démarrage possible. Branchez la voiture (statut attendu : "
                   + " ou ".join(driver.START_STATUSES) + ")")
        return steps
    say(True, f"Statut {driver.status()} : démarrage possible")

    async def stop_and_restore() -> None:
        try:
            if driver.is_charging() or (driver.NEEDS_SWITCH and driver.switch_on()):
                await driver.stop()
            if before is not None:
                await driver.set_current(int(before))
            if switchable:
                await driver.set_phases(0)
        except Exception:  # noqa: BLE001 - the report says what happened, the test must end
            pass

    async def check_power(amperes: int, on: int) -> bool:
        """The power of this current on these phases (a late sensor waited for); False when it never comes."""
        if power() is None:
            return True
        if await wait(lambda: near(amperes, on), POWER_TIMEOUT_SECONDS):
            say(True, f"Puissance à {amperes} A en {words(on)} : {round(power() or 0)} W (≈ {round(expected(amperes, on))} W attendus)")
            return True
        value = round(power() or 0)
        other = 3 if on == 1 else 1
        if switchable and near(amperes, other):
            say(False, f"Puissance à {amperes} A : {value} W, celle du {words(other)} (≈ {round(expected(amperes, other))} W) "
                       f"et pas du {words(on)} : la borne n'a pas changé de phases")
        else:
            say(False, f"Puissance à {amperes} A en {words(on)} : {value} W, ≈ {round(expected(amperes, on))} W attendus")
        return False

    first = 1 if switchable else phases
    try:
        # 3. The phases, for a station that switches: one to start with
        if switchable:
            await driver.set_phases(1)
            say(True, "Monophasé demandé")

        # 4. The lowest current
        await driver.set_current(low)
        if not await wait(lambda: driver.current_a() == low):
            say(False, f"Consigne à {low} A pas suivie : elle reste à {driver.current_a()} A")
            return steps
        say(True, f"Consigne à {low} A suivie")

        # 5. Start: the car may go through SuspendedEV / SuspendedEVSE while it wakes up, it must reach Charging
        await driver.start()
        if not await wait(lambda: driver.is_running()):
            if driver.is_done():
                say(False, f"Statut {driver.status()} depuis {STEP_TIMEOUT_SECONDS} s : la voiture ne demande pas d'énergie "
                           "(pleine, ou son propre horaire) ; le reste du test ne peut pas être vérifié")
            else:
                say(False, f"Démarrage pas suivi : statut {driver.status()}{switch_text()}")
            return steps
        say(True, f"Charge démarrée : statut {driver.status()}")
        # The station may start at another current than asked (an Easee at 7 A at least): back to the lowest first
        if driver.current_a() != low:
            await driver.set_current(low)
            await wait(lambda: driver.current_a() == low)
        if not await check_power(low, first):
            return steps
        low_power = power()

        # 6. A higher current
        await driver.set_current(high)
        if not await wait(lambda: driver.current_a() == high):
            say(False, f"Consigne à {high} A pas suivie : elle reste à {driver.current_a()} A")
            return steps
        say(True, f"Consigne à {high} A suivie")
        if low_power is not None:
            if await wait(lambda: (power() or 0) > low_power + POWER_RISE_W * first / 3, POWER_TIMEOUT_SECONDS):
                say(True, f"Puissance à {high} A : {round(power() or 0)} W (≈ {round(expected(high, first))} W attendus)")
            else:
                say(None, f"Puissance à {high} A : {round(power() or 0)} W, presque comme à {low} A "
                          "(la voiture limite peut-être sa charge)")

        # 7. Back to the lowest
        await driver.set_current(low)
        if not await wait(lambda: driver.current_a() == low):
            say(False, f"Retour à {low} A pas suivi : la consigne reste à {driver.current_a()} A")
            return steps
        say(True, f"Retour à {low} A suivi")

        # 8. One phase to three, as the control does it: switched while it charges, the station switching by itself
        if switchable:
            await driver.set_phases(3)
            say(True, "Triphasé demandé pendant la charge")
            if not await wait(lambda: driver.is_running()):
                say(False, f"La charge ne reprend pas en triphasé : statut {driver.status()}")
                return steps
            say(True, f"Charge en triphasé : statut {driver.status()}")
            if driver.current_a() != low:
                await driver.set_current(low)
                await wait(lambda: driver.current_a() == low)
            if not await check_power(low, 3):
                return steps

        # 9. Stop
        await driver.stop()
        if not await wait(lambda: not driver.is_charging()):
            say(False, f"Arrêt pas suivi : statut {driver.status()}")
            return steps
        say(True, f"Charge arrêtée : statut {driver.status()}"
                  + (" (un nouveau démarrage est possible)" if driver.can_start() else ""))
        if power() is not None:
            if await wait(lambda: (power() or 0) < 300):
                say(True, f"Puissance après l'arrêt : {round(power() or 0)} W")
            elif not driver.is_charging():
                # The status says stopped: the power sensor is late (a cloud one is read every minute or two)
                say(None, f"Le capteur de puissance indique encore {round(power() or 0)} W, mais le statut dit "
                          f"{driver.status()} : il est en retard (capteur du cloud ?). Préférez celui d'OCPP")
            else:
                say(False, f"La borne prend encore {round(power() or 0)} W après l'arrêt")
    except Exception as err:  # noqa: BLE001 - a refused command ends the test, said in the report
        say(False, f"Commande refusée : {err}")
    finally:
        await stop_and_restore()
        if before is not None:
            say(None, f"Consigne remise à {int(before)} A" + (", phases laissées au choix de la borne" if switchable else ""))
    return steps
