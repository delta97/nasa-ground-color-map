"""Authenticated named-region and monitor APIs."""

from __future__ import annotations

import hmac
import json
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from shapely.geometry import shape

from ..environment.catalog import get_product
from ..monitoring.rules import evaluate, validate_rule
from ..monitoring.webhooks import deliver, validate_webhook_url
from . import deps
from .routes_areas import _normalized
from .routes_environment import environment_product

router = APIRouter(prefix="/v1", tags=["monitoring"])


def _store(request: Request):
    store = getattr(request.app.state, "monitoring_store", None)
    if store is None: raise HTTPException(503, "monitoring is disabled or not configured")
    return store


async def require_admin(request: Request, authorization: str | None = Header(None)):
    expected = request.app.state.settings.monitoring_admin_token
    supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return _store(request)


@router.get("/regions", dependencies=[Depends(require_admin)])
async def list_regions(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    store = _store(request); rows = await store.fetchall("SELECT * FROM regions ORDER BY id LIMIT ? OFFSET ?", (limit, offset))
    for row in rows: row["geometry"] = json.loads(row.pop("geometry_json")); row["wraps_antimeridian"] = bool(row["wraps_antimeridian"])
    return {"items": rows, "limit": limit, "offset": offset}


@router.post("/regions", status_code=201)
async def create_region(request: Request, body: dict, store=Depends(require_admin)):
    if not str(body.get("name", "")).strip(): raise HTTPException(422, "name is required")
    normalized = _normalized(body.get("geometry") or {})
    try:
        cursor = await store.execute("INSERT INTO regions(name,geometry_json,wraps_antimeridian) VALUES (?,?,?)",
                                     (body["name"].strip(), json.dumps(normalized["geometry"]), normalized["wraps_antimeridian"]))
    except aiosqlite.IntegrityError: raise HTTPException(409, "region name already exists")
    return {"id": cursor.lastrowid, "name": body["name"].strip(), **normalized}


@router.get("/regions/{region_id}")
async def get_region(request: Request, region_id: int, store=Depends(require_admin)):
    row = await store.fetchone("SELECT * FROM regions WHERE id=?", (region_id,))
    if not row: raise HTTPException(404, "region not found")
    row["geometry"] = json.loads(row.pop("geometry_json")); row["wraps_antimeridian"] = bool(row["wraps_antimeridian"]); return row


@router.put("/regions/{region_id}")
async def update_region(request: Request, region_id: int, body: dict, store=Depends(require_admin)):
    current = await store.fetchone("SELECT * FROM regions WHERE id=?", (region_id,))
    if not current: raise HTTPException(404, "region not found")
    geometry = json.loads(current["geometry_json"]); wraps = current["wraps_antimeridian"]
    if "geometry" in body:
        normalized = _normalized(body["geometry"]); geometry, wraps = normalized["geometry"], normalized["wraps_antimeridian"]
    await store.execute("UPDATE regions SET name=?,geometry_json=?,wraps_antimeridian=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (body.get("name", current["name"]), json.dumps(geometry), wraps, region_id))
    return await get_region(request, region_id, store)


@router.delete("/regions/{region_id}", status_code=204)
async def delete_region(request: Request, region_id: int, store=Depends(require_admin)):
    cursor = await store.execute("DELETE FROM regions WHERE id=?", (region_id,))
    if cursor.rowcount == 0: raise HTTPException(404, "region not found")


def _validate_monitor(body: dict, settings):
    product = get_product(body.get("product", ""))
    if product is None: raise HTTPException(422, "unknown product")
    if body.get("metric") not in product.metrics: raise HTTPException(422, f"metric must be one of {list(product.metrics)}")
    try: validate_rule(body.get("rule_type", ""), body.get("threshold"))
    except ValueError as exc: raise HTTPException(422, str(exc))
    hour = body.get("run_hour", 0)
    if not isinstance(hour, int) or not 0 <= hour <= 23: raise HTTPException(422, "run_hour must be in [0, 23]")
    if body.get("minimum_quality", "usable") not in {"usable", "suspect", "unusable"}: raise HTTPException(422, "invalid minimum_quality")
    if body.get("webhook_url"):
        try: validate_webhook_url(body["webhook_url"], settings.allow_private_webhooks)
        except ValueError as exc: raise HTTPException(422, str(exc))


@router.get("/monitors")
async def list_monitors(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), store=Depends(require_admin)):
    items = await store.fetchall("SELECT * FROM monitors ORDER BY id LIMIT ? OFFSET ?", (limit, offset))
    for item in items: item["enabled"] = bool(item["enabled"]); item["active"] = bool(item["active"])
    return {"items": items, "limit": limit, "offset": offset}


@router.post("/monitors", status_code=201)
async def create_monitor(request: Request, body: dict, store=Depends(require_admin)):
    _validate_monitor(body, request.app.state.settings)
    if not await store.fetchone("SELECT id FROM regions WHERE id=?", (body.get("region_id"),)): raise HTTPException(422, "region not found")
    cursor = await store.execute("""INSERT INTO monitors(region_id,product,metric,rule_type,threshold,minimum_quality,run_hour,webhook_url,enabled)
        VALUES (?,?,?,?,?,?,?,?,?)""", (body["region_id"], body["product"], body["metric"], body["rule_type"], body.get("threshold"),
        body.get("minimum_quality", "usable"), body.get("run_hour", 0), body.get("webhook_url"), bool(body.get("enabled", True))))
    return await store.fetchone("SELECT * FROM monitors WHERE id=?", (cursor.lastrowid,))


@router.get("/monitors/{monitor_id}")
async def get_monitor(request: Request, monitor_id: int, store=Depends(require_admin)):
    row = await store.fetchone("SELECT * FROM monitors WHERE id=?", (monitor_id,))
    if not row: raise HTTPException(404, "monitor not found")
    return row


@router.put("/monitors/{monitor_id}")
async def update_monitor(request: Request, monitor_id: int, body: dict, store=Depends(require_admin)):
    current = await get_monitor(request, monitor_id, store); merged = {**current, **body}; _validate_monitor(merged, request.app.state.settings)
    await store.execute("""UPDATE monitors SET region_id=?,product=?,metric=?,rule_type=?,threshold=?,minimum_quality=?,run_hour=?,webhook_url=?,enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (merged["region_id"], merged["product"], merged["metric"], merged["rule_type"], merged.get("threshold"), merged["minimum_quality"], merged["run_hour"], merged.get("webhook_url"), bool(merged["enabled"]), monitor_id))
    return await get_monitor(request, monitor_id, store)


@router.delete("/monitors/{monitor_id}", status_code=204)
async def delete_monitor(request: Request, monitor_id: int, store=Depends(require_admin)):
    cursor = await store.execute("DELETE FROM monitors WHERE id=?", (monitor_id,))
    if cursor.rowcount == 0: raise HTTPException(404, "monitor not found")


def _quality_for(payload):
    if payload.get("quality") in {"usable", "suspect", "unusable"}:
        return payload["quality"]
    fraction = payload.get("valid_fraction")
    if fraction is None: fraction = 1 - payload.get("no_data_fraction", 1)
    return "usable" if fraction >= .3 else ("suspect" if fraction > 0 else "unusable")


async def execute_monitor(app, monitor_id: int, observation_date: str | None = None):
    store = app.state.monitoring_store
    monitor = await store.fetchone("SELECT * FROM monitors WHERE id=?", (monitor_id,))
    if not monitor: raise HTTPException(404, "monitor not found")
    region = await store.fetchone("SELECT * FROM regions WHERE id=?", (monitor["region_id"],))
    geom = shape(json.loads(region["geometry_json"])); bbox = ",".join(str(v) for v in geom.bounds)
    day = observation_date or datetime.now(timezone.utc).date().isoformat()
    try:
        payload = await environment_product(monitor["product"], bbox, day, 1, 1, app.state.gibs_client, app.state.latest_dates)
        value = payload.get(monitor["metric"]); quality = _quality_for(payload)
        result = evaluate(rule_type=monitor["rule_type"], threshold=monitor["threshold"], value=value,
                          previous_value=monitor["previous_value"], quality=quality, previous_quality=monitor["previous_quality"],
                          minimum_quality=monitor["minimum_quality"], was_active=bool(monitor["active"]))
        try:
            cursor = await store.execute("INSERT INTO observations(monitor_id,observation_date,value,quality,payload_json,accepted) VALUES (?,?,?,?,?,?)",
                                         (monitor_id, day, value, quality, json.dumps(payload), result["accepted"]))
        except aiosqlite.IntegrityError:
            return {"status": "already_evaluated", "observation_date": day}
        event = None
        if result["event"]:
            event_payload = {"type": result["event"], "monitor_id": monitor_id, "observation_date": day, "value": value, "quality": quality}
            event_cursor = await store.execute("INSERT INTO monitor_events(monitor_id,observation_id,event_type,payload_json) VALUES (?,?,?,?)",
                                               (monitor_id, cursor.lastrowid, result["event"], json.dumps(event_payload)))
            event = {**event_payload, "id": event_cursor.lastrowid}
            if monitor["webhook_url"]:
                secret = app.state.settings.webhook_signing_secret or app.state.settings.monitoring_admin_token
                async def record(attempt, status, error):
                    await store.execute("INSERT INTO webhook_attempts(event_id,attempt,response_status,error) VALUES (?,?,?,?)", (event["id"], attempt, status, error))
                await deliver(app.state.gibs_client.http, monitor["webhook_url"], event_payload, secret, record)
        if result["accepted"]:
            await store.execute("UPDATE monitors SET active=?,previous_value=?,previous_quality=? WHERE id=?", (result["active"], value, quality, monitor_id))
        return {"status": "evaluated", "observation_date": day, "value": value, "quality": quality, "event": event}
    except HTTPException: raise
    except Exception as exc:
        payload = {"monitor_id": monitor_id, "observation_date": day, "error": str(exc)}
        await store.execute("INSERT INTO monitor_events(monitor_id,event_type,payload_json) VALUES (?,?,?)", (monitor_id, "error", json.dumps(payload)))
        return {"status": "error", **payload}


@router.post("/monitors/{monitor_id}/run")
async def run_monitor(request: Request, monitor_id: int, body: dict | None = None, store=Depends(require_admin)):
    return await execute_monitor(request.app, monitor_id, (body or {}).get("observation_date"))


@router.get("/monitors/{monitor_id}/observations")
async def observations(request: Request, monitor_id: int, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), store=Depends(require_admin)):
    return {"items": await store.fetchall("SELECT * FROM observations WHERE monitor_id=? ORDER BY observation_date DESC LIMIT ? OFFSET ?", (monitor_id, limit, offset)), "limit": limit, "offset": offset}


@router.get("/monitor-events")
async def events(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), store=Depends(require_admin)):
    items = await store.fetchall("SELECT * FROM monitor_events ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
    for item in items: item["payload"] = json.loads(item.pop("payload_json"))
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/monitoring/status")
async def monitoring_status(request: Request, store=Depends(require_admin)):
    pending = (await store.fetchone("SELECT COUNT(*) AS count FROM monitor_events e JOIN monitors m ON m.id=e.monitor_id WHERE m.webhook_url IS NOT NULL AND e.event_type IN ('triggered','recovered') AND NOT EXISTS (SELECT 1 FROM webhook_attempts w WHERE w.event_id=e.id AND w.response_status BETWEEN 200 AND 299)"))["count"]
    return {"enabled": True, "database_healthy": await store.health(), "scheduler_healthy": bool(getattr(request.app.state, "monitor_scheduler_healthy", False)),
            "last_completed_cycle": getattr(request.app.state, "monitor_last_cycle", None), "pending_deliveries": pending}
