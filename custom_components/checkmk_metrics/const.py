"""Constants for the Checkmk Metrics integration."""

DOMAIN = "checkmk_metrics"
PLATFORMS = ["sensor"]

CONF_BASE_URL = "base_url"
CONF_SITE = "site"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_METRICS = "metrics"

DEFAULT_NAME = "Checkmk Metrics"
DEFAULT_SCAN_INTERVAL = 60
DEFAULT_VERIFY_SSL = True
