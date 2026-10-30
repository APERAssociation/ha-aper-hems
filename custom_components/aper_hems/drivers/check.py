"""A test of a charging station, started by the member (action "aper_hems.test_charger"): the real station is driven step by
step with the driver of its brand, each step checked on its entities, and the report shown in a notification.

The car must be plugged in and not charging. Steps: the entities, the status, 6 A, start, a higher current, back to the
lowest, stop, and its current put back as it was. Every step waits for the station to follow (its cloud or OCPP can take
a while), and the test stops at the first step that fails, the charge stopped.
"""

from __future__ import annotations

import asyncio
import time

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from . import ChargerDriver

# How long a step waits for the station to follow, and how often it looks
STEP_TIMEOUT_SECONDS = 90
POLL_SECONDS = 5
# How much the power must rise between the lowest current and the higher one (W): the car may take less than asked
POWER_RISE_W = 1000


async def run_check(hass: HomeAssistant, driver: ChargerDriver, name: str, low: int, high: int, phases: int,
                    power_w) -> list[tuple[bool | None, str]]:
    """Runs the test; returns its steps: (True passed, False failed, None skipped or for information, text)."""
    steps: list[tuple[bool | None, str]] = []
    started = time.monotonic()

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

    # 1. The entities
    missing = [label for label, entity in (("interrupteur marche/arrêt", driver.switch), ("consigne d'ampérage", driver.current),
                                           ("statut", driver.status_entity)) if not entity]
    if missing:
        say(False, "Entités manquantes dans le plugin : " + ", ".join(missing))
        return steps
    say(True, f"Entités : interrupteur {driver.switch}, consigne {driver.current}, statut {driver.status_entity}")
    if power() is None:
        say(None, "Pas de capteur de puissance : la puissance ne sera pas vérifiée")
    before = driver.current_a()
    say(None, f"Statut {driver.status()}, interrupteur {'marche' if driver.switch_on() else 'arrêt'}, consigne {before} A")

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
            if driver.is_charging() or driver.switch_on():
                await driver.stop()
            if before is not None:
                await driver.set_current(int(before))
        except Exception:  # noqa: BLE001 - the report says what happened, the test must end
            pass

    try:
        # 3. The lowest current
        await driver.set_current(low)
        if not await wait(lambda: driver.current_a() == low):
            say(False, f"Consigne à {low} A pas suivie : elle reste à {driver.current_a()} A")
            return steps
        say(True, f"Consigne à {low} A suivie")

        # 4. Start: the car may go through SuspendedEV / SuspendedEVSE while it wakes up, it must reach Charging
        await driver.start()
        if not await wait(lambda: driver.is_running()):
            if driver.is_done():
                say(False, f"Statut {driver.status()} depuis {STEP_TIMEOUT_SECONDS} s : la voiture ne demande pas d'énergie "
                           "(pleine, ou son propre horaire) ; le reste du test ne peut pas être vérifié")
            else:
                say(False, f"Démarrage pas suivi : statut {driver.status()}, "
                           f"interrupteur {'marche' if driver.switch_on() else 'arrêt'}")
            return steps
        say(True, f"Charge démarrée : statut {driver.status()}")
        expected_low = low * 230 * phases
        if power() is not None:
            if await wait(lambda: (power() or 0) > expected_low * 0.5):
                say(True, f"Puissance à {low} A : {round(power() or 0)} W (≈ {expected_low} W attendus)")
            else:
                say(False, f"Pas de puissance après le démarrage : {round(power() or 0)} W")
                return steps
        low_power = power()

        # 5. A higher current
        await driver.set_current(high)
        if not await wait(lambda: driver.current_a() == high):
            say(False, f"Consigne à {high} A pas suivie : elle reste à {driver.current_a()} A")
            return steps
        say(True, f"Consigne à {high} A suivie")
        if low_power is not None:
            if await wait(lambda: (power() or 0) > low_power + POWER_RISE_W):
                say(True, f"Puissance à {high} A : {round(power() or 0)} W (≈ {high * 230 * phases} W attendus)")
            else:
                say(None, f"Puissance à {high} A : {round(power() or 0)} W, presque comme à {low} A "
                          "(la voiture limite peut-être sa charge)")

        # 6. Back to the lowest
        await driver.set_current(low)
        if not await wait(lambda: driver.current_a() == low):
            say(False, f"Retour à {low} A pas suivi : la consigne reste à {driver.current_a()} A")
            return steps
        say(True, f"Retour à {low} A suivi")

        # 7. Stop
        await driver.stop()
        if not await wait(lambda: not driver.is_charging()):
            say(False, f"Arrêt pas suivi : statut {driver.status()}")
            return steps
        say(True, f"Charge arrêtée : statut {driver.status()}"
                  + (" (un nouveau démarrage est possible)" if driver.can_start() else ""))
        if power() is not None:
            if await wait(lambda: (power() or 0) < 300, timeout=60):
                say(True, f"Puissance après l'arrêt : {round(power() or 0)} W")
            else:
                say(False, f"La borne prend encore {round(power() or 0)} W après l'arrêt")
    except Exception as err:  # noqa: BLE001 - a refused command ends the test, said in the report
        say(False, f"Commande refusée : {err}")
    finally:
        await stop_and_restore()
        if before is not None:
            say(None, f"Consigne remise à {int(before)} A")
    return steps
