# check_mk-ha

Custom Home Assistant integration to pull selected Checkmk metrics as sensor entities.

## Install

1. Copy `custom_components/checkmk_metrics` into your Home Assistant `config/custom_components` directory.
2. Restart Home Assistant.
3. Go to **Settings -> Devices & Services -> Add Integration**.
4. Search for **Checkmk Metrics**.

## Advanced setup flow

The integration now uses a guided wizard:

1. Enter API connection settings.
2. Select a host from Checkmk.
3. Select a service from the selected host.
4. Select one or more discovered metrics (or add manual metric names).
5. Optionally add more host/service combinations.

## Notes

- Auth uses the Checkmk bearer format: `Authorization: Bearer <username> <secret>`.
- API target:
  - If `base_url` already ends with `/check_mk/api/1.0`, it is used directly.
  - Otherwise the integration builds: `<base_url>/<site>/check_mk/api/1.0`.
- Metric discovery uses multiple endpoint/response fallbacks because Checkmk payload shape differs across versions.
