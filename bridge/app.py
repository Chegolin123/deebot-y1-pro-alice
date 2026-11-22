"""Ecovacs DEEBOT <-> Yandex Smart Home bridge.

Exposes a DEEBOT Y1 Pro (device class kduunv, profile reused from cqyi87) as a
Yandex Smart Home provider: device list, state query and control commands.

Single-user service: OAuth endpoints hand out one token pair, persisted to disk
so that a restart does not break the already linked skill.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
from fastapi import FastAPI, Form, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from deebot_client.api_client import ApiClient
from deebot_client.authentication import AccountCredentials, Authenticator, create_rest_config
from deebot_client.commands.json.clean import CleanAction
from deebot_client.commands.json.native_control import NativeReturnToDock, NativeSetFanSpeed
from deebot_client.device import Device
from deebot_client.events import BatteryEvent, FanSpeedEvent, StateEvent
from deebot_client.events.fan_speed import FanSpeedLevel
from deebot_client.models import State
from deebot_client.mqtt_client import MqttClient, create_mqtt_config
from deebot_client.util import md5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOGGER = logging.getLogger("bridge")

EMAIL = os.environ.get("ECOVACS_EMAIL", "")
PASSWORD = os.environ.get("ECOVACS_PASSWORD", "")
COUNTRY = os.environ.get("ECOVACS_COUNTRY", "PL")
DEVICE_CLASS = os.environ.get("ECOVACS_CLASS", "kduunv")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "Пылесос")
DEVICE_ROOM = os.environ.get("DEVICE_ROOM", "Коридор")
BRIDGE_PASSWORD = os.environ.get("BRIDGE_PASSWORD", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
ACCOUNT_FILE = DATA_DIR / "account.json"

# Same id that was verified during the probe run, so Ecovacs does not ask for a
# new e-mail code on every login.
DEVICE_ID = md5("deebot-probe-stable-v1")

FAN_TO_YANDEX = {
    FanSpeedLevel.QUIET: "slow",
    FanSpeedLevel.NORMAL: "medium",
    FanSpeedLevel.MAX: "fast",
    FanSpeedLevel.MAX_PLUS: "turbo",
}
YANDEX_TO_FAN = {
    "auto": FanSpeedLevel.NORMAL,
    "slow": FanSpeedLevel.QUIET,
    "medium": FanSpeedLevel.NORMAL,
    "fast": FanSpeedLevel.MAX,
    "turbo": FanSpeedLevel.MAX_PLUS,
}

ACTIVE_STATES = {State.CLEANING, State.RETURNING, State.PAUSED}


class Robot:
    """Thin wrapper around the deebot-client device with cached state."""

    def __init__(self) -> None:
        self.device: Device | None = None
        self.state: State | None = None
        self.battery: int | None = None
        self.fan: FanSpeedLevel | None = None
        self.error: str | None = None
        self.last_event_at: float | None = None
        self._session: aiohttp.ClientSession | None = None
        self._auth: Authenticator | None = None
        self._mqtt: MqttClient | None = None
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self.device is not None

    def _touch(self) -> None:
        self.last_event_at = time.time()

    async def _on_state(self, event: StateEvent) -> None:
        self.state = event.state
        self._touch()
        _LOGGER.info("state -> %s", event.state.name)

    async def _on_battery(self, event: BatteryEvent) -> None:
        self.battery = int(event.value)
        self._touch()
        _LOGGER.info("battery -> %s%%", event.value)

    async def _on_fan(self, event: FanSpeedEvent) -> None:
        self.fan = event.speed
        self._touch()
        _LOGGER.info("fan -> %s", event.speed.name)

    async def start(self) -> None:
        if not EMAIL or not PASSWORD:
            raise RuntimeError("ECOVACS_EMAIL / ECOVACS_PASSWORD are not set")

        self._session = aiohttp.ClientSession()
        config = create_rest_config(self._session, device_id=DEVICE_ID, alpha_2_country=COUNTRY)
        self._auth = Authenticator(
            config,
            EMAIL,
            md5(PASSWORD),
            account_credentials=self._load_account_credentials(),
        )
        self._auth.subscribe_account_credentials(self._persist_account_credentials)
        await self._connect()

    @staticmethod
    def _load_account_credentials() -> AccountCredentials | None:
        """Token-based login avoids the e-mail code Ecovacs asks for from a new IP."""
        try:
            raw = json.loads(ACCOUNT_FILE.read_text("utf-8"))
            return AccountCredentials(access_token=raw["access_token"], user_id=raw["user_id"])
        except FileNotFoundError:
            return None
        except Exception:  # noqa: BLE001
            _LOGGER.warning("cannot read account credentials", exc_info=True)
            return None

    @staticmethod
    async def _persist_account_credentials(credentials: AccountCredentials) -> None:
        try:
            ACCOUNT_FILE.write_text(
                json.dumps(
                    {"access_token": credentials.access_token, "user_id": credentials.user_id}
                ),
                "utf-8",
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("cannot persist account credentials", exc_info=True)

    async def _connect(self) -> None:
        """Fetch the device and attach MQTT. Raises if verification is required."""
        if self._auth is None:
            raise RuntimeError("authenticator is not initialised")
        api = ApiClient(self._auth)

        devices = await api.get_devices()
        target = None
        for info in devices.mqtt:
            api_info = getattr(info, "api", {})
            if _field(api_info, "class") == DEVICE_CLASS:
                target = info
                break
        if target is None:
            classes = [_field(getattr(i, "api", {}), "class") for i in devices.mqtt]
            raise RuntimeError(f"device class {DEVICE_CLASS} not found, got {classes}")

        self.device = Device(target, self._auth)
        self.device.events.subscribe(StateEvent, self._on_state)
        self.device.events.subscribe(BatteryEvent, self._on_battery)
        self.device.events.subscribe(FanSpeedEvent, self._on_fan)

        self._mqtt = MqttClient(
            create_mqtt_config(device_id=DEVICE_ID, country=COUNTRY), self._auth
        )
        await self.device.initialize(self._mqtt)
        _LOGGER.info("robot initialised")

    async def request_code(self) -> None:
        """Ask Ecovacs to e-mail a one-time code for this device id."""
        if self._auth is None:
            await self.start()
        if self._auth is None:
            raise RuntimeError("authenticator is not initialised")
        await self._auth.request_device_verification_code()

    async def submit_code(self, code: str) -> None:
        """Confirm the device id with the received code and finish connecting."""
        if self._auth is None:
            raise RuntimeError("authenticator is not initialised")
        await self._auth.verify_device(code)
        await self._connect()

    async def stop(self) -> None:
        if self.device:
            await self.device.teardown()
        if self._session:
            await self._session.close()

    async def _send(self, command) -> None:
        async with self._lock:
            if self.device is None:
                raise RuntimeError("robot is not connected")
            response = await self.device.execute_command(command)
            if isinstance(response, dict):
                failed = response.get("ret") == "fail" or response.get("code") not in (None, 0)
                if failed:
                    reason = response.get("error") or response.get("msg") or "робот отклонил команду"
                    raise RuntimeError(str(reason))

    def _state_hint(self) -> str:
        if robot.state == State.RETURNING:
            return "Робот возвращается на базу — подождите, пока встанет на зарядку"
        if robot.state == State.DOCKED and robot.battery is not None and robot.battery < 15:
            return "Робот на зарядке, заряд низкий"
        return "Робот не принял команду"

    async def start_clean(self) -> None:
        action = self.device.capabilities.clean.action  # type: ignore[union-attr]
        await self._send(action.command(CleanAction.START))

    async def stop_clean(self) -> None:
        action = self.device.capabilities.clean.action  # type: ignore[union-attr]
        await self._send(action.command(CleanAction.STOP))

    async def pause(self, paused: bool) -> None:
        action = self.device.capabilities.clean.action  # type: ignore[union-attr]
        cmd = CleanAction.PAUSE if paused else CleanAction.RESUME
        await self._send(action.command(cmd))

    async def dock(self) -> None:
        await self._send(NativeReturnToDock())

    async def set_fan(self, level: FanSpeedLevel) -> None:
        await self._send(NativeSetFanSpeed(level))


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


robot = Robot()


# --------------------------------------------------------------------------- #
# token storage
# --------------------------------------------------------------------------- #

class TokenStore:
    """Minimal single-user token storage persisted between restarts."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._access: dict[str, float] = {}
        self._refresh: dict[str, float] = {}
        self._codes: dict[str, tuple[str, float]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text("utf-8"))
            self._access = dict(raw.get("access", {}))
            self._refresh = dict(raw.get("refresh", {}))
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.warning("cannot read token store, starting empty", exc_info=True)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"access": self._access, "refresh": self._refresh}), "utf-8"
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("cannot persist tokens", exc_info=True)

    def issue(self) -> dict[str, Any]:
        access = secrets.token_hex(24)
        refresh = secrets.token_hex(24)
        ttl = 365 * 24 * 3600
        expires = time.time() + ttl
        self._access[access] = expires
        self._refresh[refresh] = expires
        self._save()
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ttl,
            "refresh_token": refresh,
        }

    def new_code(self) -> str:
        code = secrets.token_hex(16)
        self._codes[code] = (code, time.time() + 600)
        return code

    def exchange(self, code: str) -> dict[str, Any] | None:
        entry = self._codes.pop(code, None)
        if entry is None or entry[1] < time.time():
            return None
        return self.issue()

    def refresh(self, token: str) -> dict[str, Any] | None:
        expires = self._refresh.pop(token, None)
        if expires is None or expires < time.time():
            return None
        self._save()
        return self.issue()

    def valid(self, token: str) -> bool:
        expires = self._access.get(token)
        return expires is not None and expires > time.time()

    def revoke(self, token: str) -> None:
        if self._access.pop(token, None) is not None:
            self._save()


tokens = TokenStore(DATA_DIR / "tokens.json")


# --------------------------------------------------------------------------- #
# Yandex device description
# --------------------------------------------------------------------------- #

DEVICE_ID_NAME = os.environ.get("DEVICE_ID", "deebot")
MODE_INSTANCE = os.environ.get("MODE_INSTANCE", "work_speed")

MODE_VALUES = {
    "work_speed": ["auto", "fast", "medium", "slow", "turbo"],
    "cleanup_mode": [
        "auto",
        "eco",
        "express",
        "normal",
        "quiet",
        "wet_cleaning",
        "dry_cleaning",
        "mixed_cleaning",
    ],
}

CLEANUP_TO_FAN = {
    "auto": FanSpeedLevel.NORMAL,
    "eco": FanSpeedLevel.QUIET,
    "quiet": FanSpeedLevel.QUIET,
    "normal": FanSpeedLevel.NORMAL,
    "express": FanSpeedLevel.MAX,
}


def device_payload() -> dict[str, Any]:
    enabled = {c.strip() for c in os.environ.get("CAPABILITIES", "on_off,toggle,mode").split(",") if c.strip()}

    capabilities: list[dict[str, Any]] = []
    if "on_off" in enabled:
        capabilities.append({"type": "devices.capabilities.on_off", "retrievable": True})
    if "toggle" in enabled:
        capabilities.append(
            {
                "type": "devices.capabilities.toggle",
                "retrievable": True,
                "parameters": {"instance": "pause"},
            }
        )
    if "mode" in enabled:
        values = MODE_VALUES.get(MODE_INSTANCE, MODE_VALUES["work_speed"])
        capabilities.append(
            {
                "type": "devices.capabilities.mode",
                "retrievable": True,
                "parameters": {
                    "instance": MODE_INSTANCE,
                    "value_list": [{"value": v} for v in values],
                },
            }
        )

    properties: list[dict[str, Any]] = []
    if "battery" in enabled or "on_off" in enabled:
        properties.append(
            {
                "type": "devices.properties.float",
                "retrievable": True,
                "parameters": {"instance": "battery_level", "unit": "unit.percent"},
            }
        )

    return {
        "id": DEVICE_ID_NAME,
        "name": DEVICE_NAME,
        "description": "Робот-пылесос Ecovacs DEEBOT Y1 Pro",
        "room": DEVICE_ROOM,
        "type": "devices.types.vacuum_cleaner",
        "capabilities": capabilities,
        "properties": properties,
        "device_info": {
            "manufacturer": "Ecovacs",
            "model": "DEEBOT Y1 Pro",
            "hw_version": "Y1 Pro",
            "sw_version": "ecovacs-home",
        },
    }


def on_off_state() -> bool:
    return robot.state in ACTIVE_STATES


def fan_to_yandex() -> str:
    if robot.fan is None:
        return "auto"
    return FAN_TO_YANDEX.get(robot.fan, "auto")


def _enabled() -> set[str]:
    return {c.strip() for c in os.environ.get("CAPABILITIES", "on_off,toggle,mode").split(",") if c.strip()}


def mode_value() -> str:
    if MODE_INSTANCE == "work_speed":
        return fan_to_yandex()
    if robot.fan == FanSpeedLevel.QUIET:
        return "quiet"
    if robot.fan in (FanSpeedLevel.MAX, FanSpeedLevel.MAX_PLUS):
        return "express"
    return "auto"


def capabilities_state() -> list[dict[str, Any]]:
    enabled = _enabled()
    out: list[dict[str, Any]] = []
    if "on_off" in enabled:
        out.append(
            {
                "type": "devices.capabilities.on_off",
                "state": {"instance": "on", "value": on_off_state()},
            }
        )
    if "toggle" in enabled:
        out.append(
            {
                "type": "devices.capabilities.toggle",
                "state": {"instance": "pause", "value": robot.state == State.PAUSED},
            }
        )
    if "mode" in enabled:
        out.append(
            {
                "type": "devices.capabilities.mode",
                "state": {"instance": MODE_INSTANCE, "value": mode_value()},
            }
        )
    return out


def properties_state() -> list[dict[str, Any]]:
    return [
        {
            "type": "devices.properties.float",
            "state": {
                "instance": "battery_level",
                "value": robot.battery if robot.battery is not None else 0,
            },
        }
    ]


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await robot.start()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("robot initialisation failed; service stays up for diagnostics")
    yield
    await robot.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def _authorised(authorization: str | None) -> bool:
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    return tokens.valid(authorization.split(" ", 1)[1].strip())


def _unauthorised() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/v1.0/", methods=["GET", "HEAD"])
async def root() -> dict[str, Any]:
    """Base endpoint. Yandex probes it with HEAD before enabling the skill."""
    return {"service": "ecovacs-yandex-bridge", "ok": True}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "robot_ready": robot.ready,
        "has_state": robot.state is not None,
        "last_event_age": int(time.time() - robot.last_event_at) if robot.last_event_at else None,
    }


@app.get("/debug")
async def debug(authorization: str | None = Header(default=None)) -> Any:
    if not _authorised(authorization):
        return _unauthorised()
    return {
        "state": robot.state.name if robot.state else None,
        "battery": robot.battery,
        "fan": robot.fan.name if robot.fan else None,
        "ready": robot.ready,
    }


@app.post("/admin/request-code")
async def admin_request_code(request: Request) -> Any:
    """Trigger the one-time e-mail code Ecovacs requires for a new login location."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    password = str(body.get("password") or request.query_params.get("password") or "")
    if not BRIDGE_PASSWORD or password != BRIDGE_PASSWORD:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        await robot.request_code()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("request_code failed")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return {"ok": True, "message": "Код отправлен на почту аккаунта Ecovacs"}


@app.post("/admin/verify")
async def admin_verify(request: Request) -> Any:
    """Confirm the e-mailed code and finish connecting the robot."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    password = str(body.get("password") or request.query_params.get("password") or "")
    code = str(body.get("code") or request.query_params.get("code") or "")
    if not BRIDGE_PASSWORD or password != BRIDGE_PASSWORD:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not code:
        return JSONResponse({"ok": False, "error": "code is required"}, status_code=400)
    try:
        await robot.submit_code(code)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("verify failed")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return {"ok": True, "robot_ready": robot.ready}


@app.api_route("/v1.0/user/devices", methods=["GET", "HEAD"])
async def devices(authorization: str | None = Header(default=None)) -> Any:
    if not _authorised(authorization):
        return _unauthorised()
    return {
        "request_id": str(uuid.uuid4()),
        "payload": {"user_id": "deebot-owner", "devices": [device_payload()]},
    }


@app.post("/v1.0/user/unlink")
async def unlink(authorization: str | None = Header(default=None)) -> Any:
    """Yandex calls this when the user removes the skill; must answer 200."""
    if authorization and authorization.lower().startswith("bearer "):
        tokens.revoke(authorization.split(" ", 1)[1].strip())
    return JSONResponse({}, status_code=200)


@app.post("/v1.0/user/devices/query")
async def query(request: Request, authorization: str | None = Header(default=None)) -> Any:
    if not _authorised(authorization):
        return _unauthorised()
    body = await request.json()
    requested = [d.get("id") for d in body.get("devices", [])]
    devices_out = []
    if DEVICE_ID_NAME in requested or not requested:
        devices_out.append(
            {
                "id": DEVICE_ID_NAME,
                "capabilities": capabilities_state(),
                "properties": properties_state(),
            }
        )
    return {"request_id": str(uuid.uuid4()), "payload": {"devices": devices_out}}


@app.post("/v1.0/user/devices/action")
async def action(request: Request, authorization: str | None = Header(default=None)) -> Any:
    if not _authorised(authorization):
        return _unauthorised()

    body = await request.json()
    devices_in = body.get("payload", {}).get("devices", [])
    results: list[dict[str, Any]] = []

    for dev in devices_in:
        if dev.get("id") != DEVICE_ID_NAME:
            continue
        caps_out = []
        for cap in dev.get("capabilities", []):
            cap_type = cap.get("type")
            state = cap.get("state", {})
            instance = state.get("instance")
            value = state.get("value")
            _LOGGER.info("action requested: %s %s=%s", cap_type, instance, value)
            status, message = "DONE", None
            try:
                status, message = await _apply(cap_type, instance, value)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("command failed: %s %s=%s", cap_type, instance, value)
                status, message = "ERROR", str(exc)
            result: dict[str, Any] = {"status": status}
            if message:
                result["error_message"] = message
            caps_out.append(
                {"type": cap_type, "state": {"instance": instance, "action_result": result}}
            )
        results.append({"id": DEVICE_ID_NAME, "capabilities": caps_out})

    return {"request_id": str(uuid.uuid4()), "payload": {"devices": results}}


async def _apply(cap_type: str | None, instance: str | None, value: Any) -> tuple[str, str | None]:
    if not robot.ready:
        return "ERROR", "Робот не подключён к облаку"

    if cap_type == "devices.capabilities.on_off":
        if bool(value):
            # Yandex has no reliable voice phrase for toggle/pause = false, so
            # "turn on" doubles as "resume" when the robot is paused.
            if robot.state == State.PAUSED:
                _LOGGER.info("on_off=true while paused -> resume")
                await robot.pause(False)
            elif robot.state == State.RETURNING:
                return "ERROR", robot._state_hint()
            else:
                await robot.start_clean()
        else:
            await robot.stop_clean()
        return "DONE", None

    if cap_type == "devices.capabilities.toggle" and instance == "pause":
        await robot.pause(bool(value))
        return "DONE", None

    if cap_type == "devices.capabilities.mode" and instance == MODE_INSTANCE:
        if MODE_INSTANCE == "work_speed":
            level = YANDEX_TO_FAN.get(str(value))
        else:
            level = CLEANUP_TO_FAN.get(str(value))
        if level is None:
            return "ERROR", f"Неизвестный режим: {value}"
        await robot.set_fan(level)
        return "DONE", None

    return "ERROR", f"Неподдерживаемая команда {cap_type}/{instance}"


# --------------------------------------------------------------------------- #
# OAuth for the Yandex skill
# --------------------------------------------------------------------------- #

_FORM = """<!doctype html><html lang="ru"><meta charset="utf-8">
<title>Привязка DEEBOT</title>
<style>body{{font-family:system-ui,sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem}}
input{{width:100%;padding:.6rem;font-size:1rem;margin:.4rem 0}}
button{{width:100%;padding:.7rem;font-size:1rem;margin-top:.6rem}}</style>
<h2>Привязка робота к Алисе</h2>
<p>Введите пароль моста, чтобы выдать доступ Яндексу.</p>
<form method="post" action="/auth/authorize">
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="password" name="password" placeholder="Пароль" autofocus>
  <button type="submit">Разрешить доступ</button>
</form></html>"""


@app.get("/auth/authorize", response_class=HTMLResponse)
async def authorize_page(
    client_id: str = "", redirect_uri: str = "", state: str = "", response_type: str = "code"
) -> HTMLResponse:
    return HTMLResponse(
        _FORM.format(
            client_id=_escape(client_id),
            redirect_uri=_escape(redirect_uri),
            state=_escape(state),
        )
    )


@app.post("/auth/authorize")
async def authorize_submit(
    client_id: str = Form(default=""),
    redirect_uri: str = Form(default=""),
    state: str = Form(default=""),
    password: str = Form(default=""),
) -> Any:
    if not redirect_uri.startswith("https://"):
        return HTMLResponse("<h3>Некорректный redirect_uri</h3>", status_code=400)
    if not BRIDGE_PASSWORD or password != BRIDGE_PASSWORD:
        return HTMLResponse("<h3>Неверный пароль</h3>", status_code=403)

    code = tokens.new_code()
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)


@app.post("/auth/token")
async def token_endpoint(request: Request) -> Any:
    form = await request.form()
    grant_type = str(form.get("grant_type", ""))

    if grant_type == "authorization_code":
        payload = tokens.exchange(str(form.get("code", "")))
    elif grant_type == "refresh_token":
        payload = tokens.refresh(str(form.get("refresh_token", "")))
    else:
        payload = None

    if payload is None:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    return JSONResponse(payload)


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
