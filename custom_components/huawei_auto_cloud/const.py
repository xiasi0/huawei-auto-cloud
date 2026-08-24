"""Constants for Huawei Auto Cloud."""

from __future__ import annotations

from typing import Any, Mapping

DOMAIN = "huawei_auto_cloud"
PLATFORMS: tuple[str, ...] = ("binary_sensor", "sensor", "switch", "climate", "device_tracker")

CONF_ASSET_KEY = "asset_key"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL_SECONDS = 30
MIN_SCAN_INTERVAL_SECONDS = 10
FIRMWARE_REFRESH_SECONDS = 24 * 60 * 60
FIRMWARE_RETRY_SECONDS = 60 * 60
DISCOVERED_VEHICLE_SPEC_ID = "discovered"
UNROUTABLE_ENTERPRISE_CODE = "UNKNOWN"
UNROUTABLE_OMP_ENDPOINT_ID = "unroutable_omp"
UNROUTABLE_OMP_SESSION_ID = "unroutable"
UNROUTABLE_VEHICLE_ID = "unroutable"

OMP_BASE_ORIGIN = "https://omp.uopes.cn"
SERES_APIG_ORIGIN = "https://apig.fgaiservice.com"

DEFAULT_DEVICE_MODEL = "iPhone"
DEFAULT_NATIVE_DEVICE_MODEL = "iPhone8,1"
DEFAULT_OMP_CLIENT_TYPE = "ios"
DEFAULT_APIG_CLIENT_VERSION = "HUAWEI_IVCS_APP_3.002.300"
DEFAULT_USER_AGENT = "XCar-APP-iOS/3.0.2.300 (iPhone; iOS 15.8.8; Scale/2.0)"
DEFAULT_PACKAGE_NAME = "app.huawei.auto"
HUAWEI_AUTO_CLIENT_ID = "104872091"


def scan_interval_seconds(options: Mapping[str, Any] | None) -> int:
    try:
        seconds = int((options or {}).get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_SCAN_INTERVAL_SECONDS
    return max(MIN_SCAN_INTERVAL_SECONDS, seconds)
