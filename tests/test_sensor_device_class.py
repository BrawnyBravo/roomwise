"""Sensors only use Home Assistant's own device classes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass

import custom_components.roomwise.sensor as sensor

DEVICE = "aa:bb:cc:dd:ee:ff"


def _valid(device_class):
    return device_class is None or device_class in set(SensorDeviceClass)


@pytest.mark.parametrize(
    "cls",
    [
        sensor.BermudaSensor,
        sensor.BermudaSensorFloor,
        sensor.BermudaSensorScanner,
        sensor.BermudaSensorRssi,
        sensor.BermudaSensorRange,
        sensor.BermudaSensorAreaSwitchReason,
        sensor.BermudaSensorAreaLastSeen,
    ],
)
def test_device_sensors_use_real_device_classes(cls):
    coordinator = MagicMock(devices={DEVICE: SimpleNamespace(name="Phone")})
    assert _valid(cls(coordinator, MagicMock(), DEVICE).device_class)


@pytest.mark.parametrize(
    "cls",
    [
        sensor.BermudaTotalProxyCount,
        sensor.BermudaActiveProxyCount,
        sensor.BermudaTotalDeviceCount,
        sensor.BermudaVisibleDeviceCount,
    ],
)
def test_count_sensors_have_no_device_class(cls):
    assert cls(MagicMock(), MagicMock()).device_class is None
