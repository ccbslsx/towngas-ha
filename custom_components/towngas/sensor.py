"""Sensor platform for the Towngas (港华燃气) integration."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume

# CURRENCY_CNY was removed from homeassistant.const in newer HA releases.
# It is just the string "CNY"; fall back to the literal so we stay
# compatible across old and new HA versions.
try:
    from homeassistant.const import CURRENCY_CNY
except ImportError:  # pragma: no cover - depends on HA version
    CURRENCY_CNY = "CNY"
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

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


# 账户级 token 展示（供用户在界面上查询/复制最新 token，例如粘贴到统计脚本）。
# 注意：token 属敏感凭证，仅在本机 HA 中可见。
TOKEN_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="access_token",
        name="Access Token",
        icon="mdi:key-variant",
    ),
    SensorEntityDescription(
        key="refresh_token",
        name="Refresh Token",
        icon="mdi:key-change",
    ),
    SensorEntityDescription(
        key="token_expires_at",
        name="Token 过期时间",
        icon="mdi:clock-alert-outline",
    ),
    SensorEntityDescription(
        key="token_refresh_status",
        name="Token 刷新状态",
        icon="mdi:sync-alert",
    ),
)


class TownGasTokenEntity(CoordinatorEntity[TownGasCoordinator], SensorEntity):
    """Account-level entities that mirror the current token state."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TownGasCoordinator,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_token_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "account")},
            name="港华燃气 账户",
            manufacturer="港华燃气 Towngas",
            configuration_url=coordinator.entry.data.get("base_url"),
        )

    @property
    def native_value(self) -> str | None:
        key = self.entity_description.key
        tokens = self.coordinator.client.tokens
        if key == "access_token":
            return tokens.access_token
        if key == "refresh_token":
            return tokens.refresh_token
        if key == "token_expires_at":
            exp = tokens.expires_at or 0
            if not exp:
                return "未知"
            return datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S")
        if key == "token_refresh_status":
            # 暴露最近一次刷新失败的原因，避免用户只看到"又过期了"无从排查
            return tokens.last_refresh_error or "正常"
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """可观测信息：上次刷新、下次刷新倒计时，便于肉眼确认自动刷新在跑。"""
        attrs: dict[str, Any] = {}
        coord = self.coordinator
        if coord.last_token_refresh:
            attrs["last_token_refresh"] = datetime.fromtimestamp(
                coord.last_token_refresh
            ).strftime("%Y-%m-%d %H:%M:%S")
        if coord.last_token_refresh_ok is True:
            attrs["last_token_refresh_ok"] = "成功"
        elif coord.last_token_refresh_ok is False:
            attrs["last_token_refresh_ok"] = "失败"
        else:
            attrs["last_token_refresh_ok"] = "未知"
        attrs["refresh_enabled"] = bool(coord._token_refresh_interval)
        now = dt_util.utcnow()
        nt = coord.next_token_refresh_at()
        nd = coord.next_data_refresh_at()
        if nt is not None:
            attrs["next_token_refresh"] = nt.isoformat()
            attrs["next_token_refresh_in"] = f"{max(int((nt - now).total_seconds()), 0)}s"
        if nd is not None:
            attrs["next_data_refresh"] = nd.isoformat()
            attrs["next_data_refresh_in"] = f"{max(int((nd - now).total_seconds()), 0)}s"
        return attrs


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
        self._subs_code = sub_data.get("subs_code") or sub_data.get("subs_id") or sub_key
        self._org_code = sub_data.get("org_code") or ""
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
        reading = self._sub_data.get("reading") or {}
        if key == "current_usage":
            # 优先账单里的用气量；读数接口本身不含用量时退化为 unknown
            return _f(bill.get("amount")) if bill else None
        if key == "current_reading":
            # 核心：来自 preCheck 读数（已验证可用）
            return _f(reading.get("currReading"))
        if key == "previous_reading":
            # 上期表数：preCheck 不含上期，优先取最新账单明细的 lastReading
            return _f(bill.get("lastReading")) or _f(reading.get("lastReading"))
        if key == "bill_amount":
            # 优先用账单的 chrgSum；预付费/无账单时回退 preCheck.totalFee
            # （预付费户本期气费通常为 0，已包含在读数接口响应里）。
            amt = _f(bill.get("chrgSum")) if bill else None
            if amt is None:
                amt = _f((self._sub_data.get("reading") or {}).get("totalFee"))
            return amt
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
            r = self._sub_data.get("reading") or {}
            attrs["账期"] = bill.get("yrMonth")
            attrs["本期表数"] = _f(r.get("currReading")) or _f(bill.get("currReading"))
            attrs["上期表数"] = _f(r.get("lastReading")) or _f(bill.get("lastReading"))
            attrs["读数来源"] = r.get("source") or "账单"
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
            # vcc-cbs 单位待探测确认：先原值透传，不 ÷100
            attrs["chrgSum(原始)"] = _f(bill.get("chrgSum"))
            attrs["违约金"] = _f(bill.get("unpaidLateFee"))
            attrs["余额抵扣"] = _f(bill.get("paidSum"))
            attrs["是否欠费"] = bool(_f(bill.get("totalUnpaidFee")))
            # 实测确认：chrgSum(65.55) = price(2.85) × amount(23)，单位为「元」，
            # 无需 ÷100（与旧营业厅网关「分」不同，vcc-cbs 直接返回元）。
            attrs["金额单位"] = "元"
            # 阶梯明细里 chrgSum 为字符串，amount 为 m³
            attrs["阶梯明细"] = [
                {
                    "单价": _f(s.get("price")),
                    "用量": _f(s.get("amount")),
                    "金额": _f(s.get("chrgSum")),
                }
                for s in self._steps
            ]
        elif key == "current_reading":
            # 透出原始读数，便于在不暴露 authCode 的情况下核对字段名/单位
            r = self._sub_data.get("reading") or {}
            attrs["原始读数"] = r
            attrs["读数来源"] = r.get("source") or "未知"
        elif key == "arrears":
            attrs["欠费笔数"] = self._sub_data.get("unpaid_count", 0)
        elif key == "balance":
            info = self._sub_data.get("sub_info") or {}
            bal_info = self._sub_data.get("balance_info") or {}
            attrs["户主"] = info.get("name")
            attrs["户号"] = info.get("subsCode") or self._subs_code
            attrs["户址"] = info.get("displayAddr")
            attrs["缴费单位"] = info.get("orgName")
            attrs["上次抄表日期"] = bal_info.get("last_meter_reading_date")
            attrs["可用余额(接口)"] = _f(bal_info.get("available_balance"))
            attrs["应付费用(接口)"] = _f(bal_info.get("fee_payable"))
        return attrs


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Towngas sensors from a config entry."""
    coordinator: TownGasCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for sub_key in coordinator.data:
        for description in SENSOR_DESCRIPTIONS:
            entities.append(TownGasSensorEntity(coordinator, sub_key, description))
    # 账户级：展示最新 token（token 为账户级，不随户号重复创建）
    for description in TOKEN_DESCRIPTIONS:
        entities.append(TownGasTokenEntity(coordinator, description))
    async_add_entities(entities)
