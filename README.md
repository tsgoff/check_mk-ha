# check_mk-ha

Custom Home Assistant integration to pull selected Checkmk metrics as sensor entities.

## Install

1. Copy `custom_components/checkmk_metrics` into your Home Assistant `config/custom_components` directory.
2. Restart Home Assistant.
3. Go to **Settings -> Devices & Services -> Add Integration**.
4. Search for **Checkmk Metrics**.

## Setup format for metrics

In the config form, define one metric per line:

```text
host;service;metric;name(optional);unit(optional)
```

Example:

```text
web01;CPU load;load15;CPU Load; 
web01;Memory;mem_used;Memory Used;MiB
db01;Filesystem /;fs_used;Root FS Used;GiB
```

## Notes

- Auth uses the Checkmk bearer format: `Authorization: Bearer <username> <secret>`.
- API target:
  - If `base_url` already ends with `/check_mk/api/1.0`, it is used directly.
  - Otherwise the integration builds: `<base_url>/<site>/check_mk/api/1.0`.
- If a metric endpoint response differs between Checkmk versions, the integration tries multiple payload formats and parses common response shapes.
