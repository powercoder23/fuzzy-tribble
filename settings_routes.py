"""
settings_routes.py — FastAPI router for the Settings page.
Included from dashboard_app.py (see diff in that file).
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import settings_store as store
import docker_control as dc

router = APIRouter(prefix="/api/settings", tags=["settings"])


class AlertFlagUpdate(BaseModel):
    container_name: str
    alert_type: str
    enabled: bool
    channel: str


class AlertFlagBulkUpdate(BaseModel):
    channel: str


class KillSwitchUpdate(BaseModel):
    on: bool


class ContainerActionRequest(BaseModel):
    services: list[str]
    action: str


class StartupProfileUpdate(BaseModel):
    container_name: str
    autostart: bool


@router.get("/overview")
def overview():
    services = dc.list_services()
    return {
        "services": services,
        "profiles": dc.list_service_profiles(),
        "status": dc.get_running_status(),
        "alert_matrix": store.get_alert_matrix(),
        "alert_types": store.ALERT_TYPES,
        "channels": store.CHANNELS,
        "kill_switch": store.get_kill_switch(),
        "startup_profile": store.get_startup_profile(),
    }


@router.post("/alert-flag")
def update_alert_flag(payload: AlertFlagUpdate):
    try:
        store.set_alert_flag(payload.container_name, payload.alert_type, payload.enabled, payload.channel)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/alert-flag/bulk")
def update_alert_flag_bulk(payload: AlertFlagBulkUpdate):
    """Master override: apply one channel to every container × alert-type.
    'none' disables all alerts; any other channel enables all with that route."""
    if payload.channel not in store.CHANNELS:
        raise HTTPException(400, f"channel must be one of {store.CHANNELS}")
    enabled = payload.channel != "none"
    store.set_all_alert_flags(enabled, payload.channel)
    return {"ok": True, "channel": payload.channel, "enabled": enabled}


@router.post("/kill-switch")
def update_kill_switch(payload: KillSwitchUpdate):
    store.set_kill_switch(payload.on)
    return {"ok": True, "kill_switch": payload.on}


@router.post("/startup-profile")
def update_startup_profile(payload: StartupProfileUpdate):
    store.set_startup_profile(payload.container_name, payload.autostart)
    return {"ok": True}


@router.post("/startup-profile/apply")
def apply_startup_profile():
    profile = store.get_startup_profile()
    selected = [name for name, on in profile.items() if on]
    return dc.compose_up_selected(selected)


@router.post("/container-action")
def container_action(payload: ContainerActionRequest):
    if payload.action not in ("start", "stop", "restart", "rebuild"):
        raise HTTPException(400, f"invalid action {payload.action}")
    try:
        results = dc.bulk_action(payload.services, payload.action)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"results": results}


@router.get("/containers/status")
def containers_status():
    return dc.get_running_status()
