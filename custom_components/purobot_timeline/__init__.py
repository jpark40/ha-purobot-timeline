"""Archive Purobot image entity updates and expose an authenticated timeline."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from aiohttp import web
try:
    import probatio as vol
except ImportError:
    import voluptuous as vol

from homeassistant.components.image import async_get_image
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .store import CATEGORIES, EventStore, RejectedImage

DOMAIN = "purobot_timeline"
DEFAULT_VISITS = "image.purobot_max_pro_2_last_visit_event"
DEFAULT_TOILET = "image.purobot_max_pro_2_last_toileting_event"
_LOGGER = logging.getLogger(__name__)


CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({
    vol.Optional("directory", default="/media/purobot_timeline"): cv.string,
    vol.Optional("visit_entity", default=DEFAULT_VISITS): cv.entity_id,
    vol.Optional("toilet_entity", default=DEFAULT_TOILET): cv.entity_id,
    vol.Optional("capture_current_on_startup", default=True): cv.boolean,
    vol.Optional("retention_days", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=3650)),
    vol.Optional("max_storage_mb", default=2048): vol.All(vol.Coerce(int), vol.Range(min=16, max=1048576)),
})}, extra=vol.ALLOW_EXTRA)


class IndexView(HomeAssistantView):
    url = "/api/purobot_timeline"
    name = "api:purobot_timeline:index"
    requires_auth = True

    def __init__(self, hass, store, runtime):
        self.hass, self.store, self.runtime = hass, store, runtime

    async def get(self, request):
        q = request.query
        try:
            result = await self.hass.async_add_executor_job(
                self.store.list_events, q.get("category", "visits"), q.get("date"),
                q.get("before"), int(q.get("limit", "80")),
            )
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        result["status"].update(self.runtime)
        return web.json_response(result, headers={"Cache-Control": "no-store"})


class ImageView(HomeAssistantView):
    requires_auth = True

    def __init__(self, hass, store, small=False):
        self.hass, self.store, self.small = hass, store, small
        kind = "thumbnail" if small else "image"
        self.url = f"/api/purobot_timeline/{kind}/{{ident}}"
        self.name = f"api:purobot_timeline:{kind}"

    async def get(self, request, ident):
        path = await self.hass.async_add_executor_job(self.store.image_path, ident, self.small)
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"
        })


async def async_setup(hass, config):
    cfg = config[DOMAIN]
    sources = {
        cfg["visit_entity"]: "visits",
        cfg["toilet_entity"]: "toilet_used",
    }
    if len(sources) != 2:
        _LOGGER.error("Purobot visit_entity and toilet_entity must be different entities")
        return False

    store = EventStore(cfg["directory"], cfg["retention_days"], cfg["max_storage_mb"])
    await hass.async_add_executor_job(store.initialize)
    runtime = {
        "received": 0, "saved": 0, "duplicates": 0, "rejected": 0,
        "dropped": 0, "queue_error": None,
    }
    queue = asyncio.Queue(maxsize=16)

    async def fetch_and_queue(entity_id, category, timestamp, report_errors=True):
        try:
            result = await async_get_image(hass, entity_id)
            item = (entity_id, category, timestamp, result.content, result.content_type)
            if queue.full():
                if report_errors:
                    runtime["dropped"] += 1
                    runtime["queue_error"] = "An image update was dropped because the archive queue was full."
                    _LOGGER.error("Purobot image dropped: archive queue is full")
                return False
            runtime["received"] += 1
            queue.put_nowait(item)
            return True
        except (HomeAssistantError, ValueError) as err:
            if report_errors:
                runtime["rejected"] += 1
                runtime["queue_error"] = f"Could not fetch {entity_id}: {err}"
                _LOGGER.warning("Could not fetch updated Purobot image %s: %s", entity_id, err)
            return False
        except Exception:
            if report_errors:
                runtime["rejected"] += 1
                runtime["queue_error"] = f"Could not fetch {entity_id}; check Home Assistant logs."
                _LOGGER.exception("Could not fetch updated Purobot image %s", entity_id)
            return False

    @callback
    def state_changed(event):
        entity_id = event.data["entity_id"]
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable", ""):
            return
        # Image access tokens rotate periodically. Only the state timestamp means
        # that the image itself changed.
        if old_state is not None and old_state.state == new_state.state:
            return
        hass.async_create_task(
            fetch_and_queue(entity_id, sources[entity_id], new_state.state),
            f"Archive updated Purobot image {entity_id}",
        )

    async def worker():
        while True:
            entity_id, category, timestamp, content, content_type = await queue.get()
            try:
                outcome = await hass.async_add_executor_job(
                    store.ingest, entity_id, category, timestamp, content,
                    content_type, hass.config.time_zone,
                )
                runtime["saved" if outcome == "saved" else "duplicates"] += 1
                if outcome == "saved":
                    runtime["queue_error"] = None
            except RejectedImage as err:
                runtime["rejected"] += 1
                runtime["queue_error"] = str(err)
                _LOGGER.warning("Purobot timeline skipped an image: %s", err)
                await hass.async_add_executor_job(
                    lambda reason=str(err): store.record_status(last_error=reason)
                )
            except Exception:
                runtime["rejected"] += 1
                runtime["queue_error"] = "Could not save an image; check HA logs and free disk space."
                _LOGGER.exception("Could not save a Purobot timeline image")
            finally:
                queue.task_done()

    task = hass.async_create_background_task(worker(), "Purobot timeline writer")
    unsubscribe = async_track_state_change_event(hass, list(sources), state_changed)
    hass.http.register_view(IndexView(hass, store, runtime))
    hass.http.register_view(ImageView(hass, store))
    hass.http.register_view(ImageView(hass, store, True))

    async def cleanup(_now):
        await hass.async_add_executor_job(store.cleanup)

    remove_timer = async_track_time_interval(hass, cleanup, timedelta(hours=1))

    async def seed_current():
        # YAML integrations can initialize before config-entry image platforms.
        # Retry for two minutes so restored states and their backing ImageEntity
        # objects are both available before giving up.
        pending = dict(sources)
        for _attempt in range(24):
            for entity_id, category in list(pending.items()):
                state = hass.states.get(entity_id)
                if (state is not None and state.state not in
                        ("unknown", "unavailable", "none", "")):
                    if await fetch_and_queue(
                            entity_id, category, state.state, report_errors=False):
                        pending.pop(entity_id, None)
            if not pending:
                _LOGGER.info("Archived the current Purobot image for both timeline tabs")
                return
            await asyncio.sleep(5)
        names = ", ".join(pending)
        runtime["rejected"] += len(pending)
        runtime["queue_error"] = f"Startup capture could not read: {names}"
        _LOGGER.warning("Purobot startup capture timed out waiting for: %s", names)

    seed_task = (hass.async_create_task(seed_current(), "Seed current Purobot timeline images")
                 if cfg["capture_current_on_startup"] else None)

    async def stop(_event):
        unsubscribe()
        remove_timer()
        if seed_task is not None:
            seed_task.cancel()
        try:
            async with asyncio.timeout(15):
                await queue.join()
        finally:
            task.cancel()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    hass.data[DOMAIN] = {"store": store, "runtime": runtime, "worker": task}
    _LOGGER.info("Purobot Event Timeline ready for %s", ", ".join(sources))
    return True
