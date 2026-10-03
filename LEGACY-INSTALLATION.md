# Purobot Event Timeline for Home Assistant

This package saves every new image published by these two Home Assistant entities:

- `image.purobot_max_pro_2_last_visit_event` → **Visits**
- `image.purobot_max_pro_2_last_toileting_event` → **Toilet Used**

It provides a dedicated timeline card with date navigation, thumbnails, paging,
full-screen viewing, swipe between images, pinch zoom up to 8×, drag-to-pan, and
automatic disk retention.

The archive is local to Home Assistant. It does not use MQTT and it does not
modify the original Purobot image entities.

## Install

1. Copy the folder:

   `/custom_components/purobot_timeline`

   to:

   `/config/custom_components/purobot_timeline`

2. Copy:

   `/www/purobot-timeline-card.js`

   to:

   `/config/www/purobot-timeline-card.js`

3. Merge the block from `examples/configuration.yaml` into your existing
   `/config/configuration.yaml`.

4. Restart Home Assistant.

5. In **Settings → Dashboards → Resources**, add:

   - URL: `/local/purobot-timeline-card.js?v=1.0.3`
   - Resource type: **JavaScript Module**

6. Add the card YAML from `examples/card.yaml` to an existing dashboard, or use
   `examples/dashboard-view.yaml` to add a complete panel view.

7. If the browser reports that the custom element does not exist, hard-refresh
   the browser or reload the Home Assistant app after updating the resource URL.

## Update from v1.0.2

Replace `/config/www/purobot-timeline-card.js` with the updated file and change
the dashboard resource URL to `/local/purobot-timeline-card.js?v=1.0.3`.
Refresh the dashboard. Toilet Used now appears first and is the default tab.
If your existing card sets `initial_category: visits`, change it to
`initial_category: toilet_used` to open Toilet Used on load.
No integration restart or archive migration is needed for this card update.

## Configuration

```yaml
purobot_timeline:
  directory: /media/purobot_timeline
  visit_entity: image.purobot_max_pro_2_last_visit_event
  toilet_entity: image.purobot_max_pro_2_last_toileting_event
  capture_current_on_startup: true
  retention_days: 30
  max_storage_mb: 2048
```

`capture_current_on_startup: true` saves the current image from each entity the
first time the integration runs. Restarting Home Assistant does not create a
duplicate because the image entity, category, update timestamp, and image hash
form the persistent event ID.

Startup capture retries for up to two minutes so Purobot image entities supplied
by a config entry have time to register after YAML integrations initialize.

Every later state timestamp change is fetched immediately and written as an
immutable JPEG plus a small thumbnail. Attribute-only changes—especially the
image access token that Home Assistant rotates periodically—are ignored.

The archive starts with the images currently exposed by the two entities. It
cannot recover older Purobot images that were replaced before installation.

## Dashboard card options

```yaml
type: custom:purobot-timeline
title: Purobot Event Timeline
initial_category: toilet_used
refresh_seconds: 15
thumbnail_width: 170
page_size: 80
```

- `initial_category`: `visits` or `toilet_used` (default)
- `refresh_seconds`: `0` disables automatic refresh
- `thumbnail_width`: 120–320 pixels
- `page_size`: 10–200 events

## Storage and privacy

- Default location: `/media/purobot_timeline`
- Default retention: 30 days
- Default size limit: 2,048 MB
- Oldest events are removed when either limit is exceeded.
- Timeline and image API endpoints require Home Assistant authentication.
- Only hashed archive filenames are managed; unrelated files are not removed.

## Verify operation

After Home Assistant restarts:

1. Open **Settings → System → Logs** and search for `Purobot Event Timeline`.
2. Open the dashboard card. The current image from each available entity should
   appear once when startup capture is enabled.
3. Cause a new visit or litter-box use. The matching tab should receive a new
   snapshot after the Purobot integration updates its image entity.

If the card shows a receiver error, confirm both entity IDs exist and currently
display images in Home Assistant.
