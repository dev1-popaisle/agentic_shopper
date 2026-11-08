import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SELLER_CONFIG_PATH = PROJECT_ROOT / "Config" / "seller.json"
WEEKDAY_NUMBERS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class SellerScheduleError(Exception):
    pass


@dataclass(frozen=True)
class SellerScheduleConfig:
    marketplace_address: str
    timezone: str
    available_weekdays: tuple[int, ...]
    open_time: time
    close_time: time
    slot_minutes: int

    @classmethod
    def load(cls, path: Path = DEFAULT_SELLER_CONFIG_PATH) -> "SellerScheduleConfig":
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SellerScheduleError(f"Could not read seller configuration: {path}") from exc
        except json.JSONDecodeError as exc:
            raise SellerScheduleError("Seller configuration must contain valid JSON.") from exc

        if not isinstance(config, dict):
            raise SellerScheduleError("Seller configuration must be a JSON object.")
        address = config.get("marketplace_address")
        timezone = config.get("timezone")
        availability = config.get("availability")
        if (
            not isinstance(address, str)
            or not address.strip()
            or len(address) > 500
            or not isinstance(timezone, str)
            or not isinstance(availability, dict)
        ):
            raise SellerScheduleError("Seller configuration is missing address or availability settings.")

        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise SellerScheduleError("Seller configuration has an unknown timezone.") from exc

        days = availability.get("days")
        open_text = availability.get("open_time")
        close_text = availability.get("close_time")
        slot_minutes = availability.get("slot_minutes")
        if (
            not isinstance(days, list)
            or not days
            or any(not isinstance(day, str) or day.lower() not in WEEKDAY_NUMBERS for day in days)
            or len({day.lower() for day in days}) != len(days)
            or not isinstance(open_text, str)
            or not isinstance(close_text, str)
            or isinstance(slot_minutes, bool)
            or not isinstance(slot_minutes, int)
            or not 15 <= slot_minutes <= 240
            or slot_minutes % 15 != 0
        ):
            raise SellerScheduleError("Seller availability settings are invalid.")
        try:
            opening = time.fromisoformat(open_text)
            closing = time.fromisoformat(close_text)
        except ValueError as exc:
            raise SellerScheduleError("Seller availability times must use HH:MM format.") from exc
        if opening >= closing or opening.second or closing.second:
            raise SellerScheduleError("Seller opening time must be earlier than closing time.")

        return cls(
            marketplace_address=address.strip(),
            timezone=timezone,
            available_weekdays=tuple(WEEKDAY_NUMBERS[day.lower()] for day in days),
            open_time=opening,
            close_time=closing,
            slot_minutes=slot_minutes,
        )

    def local_today(self) -> date:
        return datetime.now(ZoneInfo(self.timezone)).date()

    def available_slots(self, start: date, end: date) -> list[datetime]:
        slots: list[datetime] = []
        current = start
        while current <= end:
            if current.weekday() in self.available_weekdays:
                slot_time = datetime.combine(current, self.open_time)
                close_time = datetime.combine(current, self.close_time)
                while slot_time < close_time:
                    slots.append(slot_time)
                    slot_time += timedelta(minutes=self.slot_minutes)
            current += timedelta(days=1)
        return slots

    def nearest_slot(
        self,
        preferred: datetime,
        earliest_date: date,
        unavailable_slots: set[datetime] | None = None,
    ) -> datetime | None:
        slots = self.available_slots(earliest_date, earliest_date + timedelta(days=90))
        if unavailable_slots:
            slots = [slot for slot in slots if slot not in unavailable_slots]
        return min(slots, key=lambda slot: (abs(slot - preferred), slot)) if slots else None

    def next_slot(
        self,
        earliest_date: date,
        unavailable_slots: set[datetime] | None = None,
    ) -> datetime | None:
        slots = self.available_slots(earliest_date, earliest_date + timedelta(days=90))
        if unavailable_slots:
            slots = [slot for slot in slots if slot not in unavailable_slots]
        return slots[0] if slots else None
