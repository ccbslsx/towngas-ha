"""Sensor platform for the Towngas (港华燃气) integration."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CURRENCY_CNY, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TownGasCoordinator


def _f(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _fen(value: Any) -> float | None:
    """Convert 分 (cents, as returned by the API) to 元 (yuan)."""
    f = _f(value)
    if f is None:
        return None
    return round(f / 100.0, 2)


SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="current_usage",
        name="本期用气",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        icon="mdi:gas-cylinder",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="current_reading",
        name="本期表数",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
    ),
    SensorEntityDescription(
        key="previous_reading",
        name="上期表数",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.GAS,
        icon="mdi:counter",
    ),
    SensorEntityDescription(
        key="bill_amount",
        name="本期账单金额",
        native_unit_of_measurement=CURRENCY_CNY,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:receipt-text-outline",
    ),
    SensorEntityDescription(
        key="gas_price",
        name="用气单价",
        native_unit_of_measurement=f"{CURRENCY_CNY}/{UnitOfVolume.CUBIC_METERS}",
        icon="mdi:cash-multiple",
    ),
    SensorEntityDescription(
        key="balance",
        name="账户余额",
        native_unit_of_measurement=CURRENCY_CNY,
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:wallet-outline",
    ),
    SensorEntityDescription(
        key="arrears",
        name="欠费金额",
        native_unit_of_measurement=CURRENCY_CNY,
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:alert-circle-outline",
    ),
    SensorEntityDescription(
        key="bill_period",
        name="账期",
        icon="mdi:calendar-month-outline",
    ),
)


class TownGasSensorEntity(CoordinatorEntity[TownGasCoordinator], SensorEntity):
    """A single Towngas sensor tied to one subscription device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TownGasCoordinator,
        sub_key: str,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        sub_data = coordinator.data[sub_key]
        self.entity_description = description
        self._sub_key = sub_key
        self._subs_code = sub_data["subs_code"]
        self._org_code = sub_data["org_code"]
        self._attr_unique_id = (
            f"{DOMAIN}_{self._org_code}_{self._subs_code}_{description.key}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        data = self.coordinator.data.get(self._sub_key, {})
        info = data.get("sub_info") or {}
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._org_code}_{self._subs_code}")},
            name=f"港华燃气 {self._subs_code}",
            manufacturer="港华燃气 Towngas",
            model=info.get("orgName") or self._org_code,
            hw_version=None,
            sw_version=None,
            configuration_url=self.coordinator.entry.data.get("base_url"),
        )

    @property
    def _sub_data(self) -> dict[str, Any]:
        return self.coordinator.data.get(self._sub_key, {})

    @property
    def _latest_bill(self) -> dict[str, Any]:
        bills = self._sub_data.get("bills") or []
        return bills[0] if bills else {}

    @property
    def _steps(self) -> list[dict[str, Any]]:
        return self._latest_bill.get("stepFeeResults") or []

    @property
    def native_value(self) -> float | str | None:
        key = self.entity_description.key
        bill = self._latest_bill
        if key == "current_usage":
            return _f(bill.get("amount"))
        if key == "current_reading":
            return _f(bill.get("currReading"))
        if key == "previous_reading":
            return _f(bill.get("lastReading"))
        if key == "bill_amount":
            # chrgSum 单位为「分」
            return _fen(bill.get("chrgSum"))
        if key == "gas_price":
            price = None
            if self._steps:
                price = _f(self._steps[0].get("price"))
            if price is None:
                raw_price = bill.get("price")
                if isinstance(raw_price, str):
                    m = re.search(r"[\d.]+", raw_price)
                    if m:
                        price = _f(m.group(0))
            return price
        if key == "balance":
            # api 已转换为「元」
            return _f(self._sub_data.get("balance"))
        if key == "arrears":
            # coordinator 已转换为「元」
            return _f(self._sub_data.get("arrears"))
        if key == "bill_period":
            ym = bill.get("yrMonth")
            if isinstance(ym, str) and len(ym) == 6:
                return f"{ym[0:4]}-{ym[4:6]}"
            return ym
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        key = self.entity_description.key
        bill = self._latest_bill
        attrs: dict[str, Any] = {}
        if key == "current_usage":
            attrs["账期"] = bill.get("yrMonth")
            attrs["本期表数"] = _f(bill.get("currReading"))
            attrs["上期表数"] = _f(bill.get("lastReading"))
            attrs["阶梯明细"] = [
                {
                    "单价": _f(s.get("price")),
                    "用量": _f(s.get("amount")),
                    "金额": _f(s.get("chrgSum")),
                }
                for s in self._steps
            ]
        elif key == "bill_amount":
            attrs["账期"] = bill.get("yrMonth")
            attrs["用气量"] = _f(bill.get("amount"))
            attrs["本期表数"] = _f(bill.get("currReading"))
            attrs["上期表数"] = _f(bill.get("lastReading"))
            # unpaidLateFee / paidSum 单位为「分」
            attrs["违约金"] = _fen(bill.get("unpaidLateFee"))
            attrs["余额抵扣"] = _fen(bill.get("paidSum"))
            attrs["是否欠费"] = bool(_f(bill.get("totalUnpaidFee")))
            # 阶梯明细里 chrgSum 已是「元」(字符串)，amount 为 m³
            attrs["阶梯明细"] = [
                {
                    "单价": _f(s.get("price")),
                    "用量": _f(s.get("amount")),
                    "金额": _f(s.get("chrgSum")),
                }
                for s in self._steps
            ]
        elif key == "arrears":
            attrs["欠费笔数"] = self._sub_data.get("unpaid_count", 0)
        elif key == "balance":
            info = self._sub_data.get("sub_info") or {}
            attrs["户主"] = info.get("name")
            attrs["户号"] = info.get("subsCode") or self._subs_code
            attrs["户址"] = info.get("displayAddr")
            attrs["缴费单位"] = info.get("orgName")
        return attrs


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Towngas sensors from a config entry."""
    coordinator: TownGasCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[TownGasSensorEntity] = []
    for sub_key in coordinator.data:
        for description in SENSOR_DESCRIPTIONS:
            entities.append(TownGasSensorEntity(coordinator, sub_key, description))
    async_add_entities(entities)
