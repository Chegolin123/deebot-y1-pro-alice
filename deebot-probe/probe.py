"""Read-only probe for DEEBOT Y1 Pro (cqyi87) on a European account.

Nothing is sent to the robot except the library's own state requests.
No cleaning commands are issued.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os

import aiohttp

from deebot_client.api_client import ApiClient
from deebot_client.authentication import Authenticator, create_rest_config
from deebot_client.device import Device
from deebot_client.events import BatteryEvent, StateEvent
from deebot_client.exceptions import DeviceVerificationRequiredError
from deebot_client.mqtt_client import MqttClient, create_mqtt_config
from deebot_client.util import md5

COUNTRY = os.environ.get("DEEBOT_COUNTRY", "PL")
WATCH_SECONDS = int(os.environ.get("PROBE_SECONDS", "60"))

_log_buffer: list[str] = []


class _Collector(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            _log_buffer.append(f"{record.levelname} {record.name}: {record.getMessage()}")
        except Exception:  # noqa: BLE001
            pass


def _field(obj, key, default=None):
    """Read a field from either a dict-like or object-like device record."""
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)
    except Exception:  # noqa: BLE001
        return default


def _api_of(device):
    return getattr(device, "api", device)


async def get_devices(auth: Authenticator, api: ApiClient):
    """Fetch the device list, handling e-mail verification of a new device ID."""
    try:
        return await api.get_devices()
    except DeviceVerificationRequiredError:
        print("\n*** Ecovacs требует подтверждение нового устройства ***")
        await auth.request_device_verification_code()
        print("Код отправлен на e-mail аккаунта.")
        code = input("Код из письма: ").strip()
        await auth.verify_device(code)
        return await api.get_devices()


def _ascii_only(value: str, what: str) -> str:
    """Strip characters that cannot be sent to the API (surrogates, Cyrillic).

    Consoles may hand over cp1251 bytes decoded with surrogateescape, which
    later crash md5() inside the library with a cryptic UnicodeEncodeError.
    """
    cleaned = "".join(ch for ch in value if 32 <= ord(ch) < 127).strip()
    if cleaned != value.strip():
        print(f"  ! во вводе '{what}' были недопустимые символы — они удалены")
    return cleaned


async def main() -> None:
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setLevel(logging.WARNING)
    root.addHandler(handler)
    buffer_handler = _Collector()
    buffer_handler.setLevel(logging.DEBUG)
    root.addHandler(buffer_handler)
    root.setLevel(logging.DEBUG)

    email = _ascii_only(input("Ecovacs e-mail: "), "email")
    password = _ascii_only(getpass.getpass("Пароль Ecovacs: "), "пароль")

    device_id = md5('deebot-probe-stable-v1')
    print(f"\nаккаунт: {email} | пароль принят: {len(password)} символов")
    print(f"страна: {COUNTRY}, device_id: {device_id}\n")

    async with aiohttp.ClientSession() as session:
        config = create_rest_config(session, device_id=device_id, alpha_2_country=COUNTRY)
        auth = Authenticator(config, email, md5(password))
        api = ApiClient(auth)

        print("Вход в облако...")
        devices = await get_devices(auth, api)

        print("\n=== MQTT-устройства (поддерживаемые) ===")
        for dev in devices.mqtt:
            raw = _api_of(dev)
            print(json.dumps({
                "class": _field(raw, "class"),
                "nick": _field(raw, "nick"),
                "name": _field(raw, "name"),
                "did": _field(raw, "did"),
                "resource": _field(raw, "resource"),
                "service": _field(raw, "service"),
            }, ensure_ascii=False, indent=2))
            caps = getattr(getattr(dev, "static", None), "capabilities", None)
            if caps is not None:
                print("  capabilities:", type(caps).__name__, "->", caps)

        if devices.not_supported:
            print("\n=== НЕ поддерживаемые ===")
            for raw in devices.not_supported:
                print(json.dumps({
                    "class": _field(raw, "class"),
                    "nick": _field(raw, "nick"),
                    "did": _field(raw, "did"),
                }, ensure_ascii=False))

        if not devices.mqtt:
            print("\nНет устройств с профилем — дальше идти некуда.")
            return

        bot = Device(devices.mqtt[0], auth)
        seen: list[str] = []

        async def on_battery(event: BatteryEvent) -> None:
            seen.append(f" BatteryEvent: {event.value}%")
            print(f"  -> заряд {event.value}%")

        async def on_state(event: StateEvent) -> None:
            seen.append(f" StateEvent: {event.state}")
            print(f"  -> состояние {event.state}")

        bot.events.subscribe(BatteryEvent, on_battery)
        bot.events.subscribe(StateEvent, on_state)

        mqtt_config = create_mqtt_config(device_id=device_id, country=COUNTRY)
        mqtt = MqttClient(mqtt_config, auth)

        print(f"\nПодключаю MQTT и слушаю {WATCH_SECONDS} секунд (команд не отправляю)...")
        try:
            await bot.initialize(mqtt)
        except Exception as exc:  # noqa: BLE001
            print(f"ОШИБКА initialize: {type(exc).__name__}: {exc}")
            raise

        await asyncio.sleep(WATCH_SECONDS)
        await bot.teardown()

        print("\n=== пойманные события ===")
        print("\n".join(seen) if seen else "  ничего")

        interesting = [
            line for line in _log_buffer
            if any(k in line.lower() for k in ("unknown", "unsupported", "not supported", "30000", "telemetry"))
        ]
        print("\n=== подозрительные строки лога ===")
        print("\n".join(interesting[-25:]) if interesting else "  нет")


if __name__ == "__main__":
    asyncio.run(main())
