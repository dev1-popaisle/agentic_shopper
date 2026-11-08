import json
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SELLER_POLICY_PATH = PROJECT_ROOT / "Config" / "seller_policy.json"
DEFAULT_DISCOUNT_PATH = PROJECT_ROOT / "Config" / "default_discount.json"


class SellerPolicyError(Exception):
    pass


class SellerNegotiationPolicy:
    def __init__(
        self,
        *,
        daily_sales_by_upc: dict[str, int],
        demand_discounts_percent: dict[str, int],
        wait_discounts_percent: dict[str, int],
        city_postal_code_prefixes: tuple[str, ...],
        delivery_fee_amount: int,
        free_minimum_quantity: int,
        default_discounts_by_upc: dict[str, int] | None = None,
    ) -> None:
        self.daily_sales_by_upc = daily_sales_by_upc
        self.demand_discounts_percent = demand_discounts_percent
        self.wait_discounts_percent = wait_discounts_percent
        self.city_postal_code_prefixes = city_postal_code_prefixes
        self.delivery_fee_amount = delivery_fee_amount
        self.free_minimum_quantity = free_minimum_quantity
        self.default_discounts_by_upc = default_discounts_by_upc or {}

    @classmethod
    def load(
        cls,
        path: Path = DEFAULT_SELLER_POLICY_PATH,
        default_discount_path: Path = DEFAULT_DISCOUNT_PATH,
    ) -> "SellerNegotiationPolicy":
        try:
            policy = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SellerPolicyError(f"Could not read seller policy file: {path}") from exc
        except json.JSONDecodeError as exc:
            raise SellerPolicyError("Seller policy file must contain valid JSON.") from exc
        try:
            default_discounts = json.loads(default_discount_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SellerPolicyError(
                f"Could not read default discount file: {default_discount_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise SellerPolicyError("Default discount file must contain valid JSON.") from exc

        if not isinstance(policy, dict) or policy.get("version") != 1:
            raise SellerPolicyError("Seller policy must be a version 1 JSON object.")
        if not isinstance(default_discounts, dict) or default_discounts.get("version") != 1:
            raise SellerPolicyError("Default discount file must be a version 1 JSON object.")

        daily_sales = policy.get("daily_sales_by_upc")
        demand_discounts = policy.get("demand_discounts_percent")
        wait_discounts = policy.get("wait_discounts_percent")
        delivery = policy.get("delivery")
        if not all(
            isinstance(value, dict)
            for value in (daily_sales, demand_discounts, wait_discounts, delivery)
        ):
            raise SellerPolicyError("Seller policy is missing a required policy section.")

        normalized_sales: dict[str, int] = {}
        for upc, units in daily_sales.items():
            if (
                not isinstance(upc, str)
                or not re.fullmatch(r"\d{12}", upc)
                or isinstance(units, bool)
                or not isinstance(units, int)
                or units < 0
            ):
                raise SellerPolicyError("Daily sales must map 12-digit UPC-A codes to non-negative integers.")
            normalized_sales[upc] = units

        required_demand_tiers = {
            "above_100_units_per_day",
            "50_to_100_units_per_day",
            "1_to_49_units_per_day",
            "zero_units_per_day",
        }
        normalized_demand_discounts = cls._read_percent_map(
            demand_discounts,
            required_demand_tiers,
            "demand discounts",
        )
        normalized_wait_discounts = cls._read_percent_map(
            wait_discounts,
            {"7_days", "14_days"},
            "wait discounts",
        )
        discounts_by_upc = default_discounts.get("discounts_by_upc")
        if not isinstance(discounts_by_upc, dict):
            raise SellerPolicyError("Default discounts must map UPC-A codes to percentages.")
        normalized_default_discounts: dict[str, int] = {}
        for upc, percent in discounts_by_upc.items():
            if (
                not isinstance(upc, str)
                or not re.fullmatch(r"\d{12}", upc)
                or isinstance(percent, bool)
                or not isinstance(percent, int)
                or not 1 <= percent <= 100
            ):
                raise SellerPolicyError(
                    "Default discounts must map 12-digit UPC-A codes to integer percentages from 1 to 100."
                )
            normalized_default_discounts[upc] = percent

        prefixes = delivery.get("city_postal_code_prefixes")
        fee = delivery.get("fee_amount")
        free_minimum = delivery.get("free_minimum_quantity")
        if (
            not isinstance(prefixes, list)
            or not prefixes
            or any(not isinstance(prefix, str) or not re.fullmatch(r"\d{3,5}", prefix) for prefix in prefixes)
            or isinstance(fee, bool)
            or not isinstance(fee, int)
            or fee < 0
            or isinstance(free_minimum, bool)
            or not isinstance(free_minimum, int)
            or free_minimum < 1
        ):
            raise SellerPolicyError("Seller delivery policy is invalid.")

        return cls(
            daily_sales_by_upc=normalized_sales,
            demand_discounts_percent=normalized_demand_discounts,
            wait_discounts_percent=normalized_wait_discounts,
            city_postal_code_prefixes=tuple(prefixes),
            delivery_fee_amount=fee,
            free_minimum_quantity=free_minimum,
            default_discounts_by_upc=normalized_default_discounts,
        )

    @staticmethod
    def _read_percent_map(
        values: dict[str, Any],
        expected_keys: set[str],
        name: str,
    ) -> dict[str, int]:
        if set(values) != expected_keys:
            raise SellerPolicyError(f"Seller {name} must define exactly: {', '.join(sorted(expected_keys))}.")
        result: dict[str, int] = {}
        for key, percent in values.items():
            if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
                raise SellerPolicyError(f"Seller {name} must be integer percentages from 0 to 100.")
            result[key] = percent
        return result

    def demand_discount_percent(self, upc: str) -> int:
        if upc not in self.daily_sales_by_upc:
            raise SellerPolicyError("No seller demand data is configured for this product.")
        daily_sales = self.daily_sales_by_upc[upc]
        if daily_sales > 100:
            tier = "above_100_units_per_day"
        elif daily_sales >= 50:
            tier = "50_to_100_units_per_day"
        elif daily_sales > 0:
            tier = "1_to_49_units_per_day"
        else:
            tier = "zero_units_per_day"
        return self.demand_discounts_percent[tier]

    def wait_discount_percent(self, wait_days: int) -> int:
        if wait_days >= 14:
            return self.wait_discounts_percent["14_days"]
        if wait_days >= 7:
            return self.wait_discounts_percent["7_days"]
        return 0

    def default_discount_percent(self, upc: str) -> int | None:
        return self.default_discounts_by_upc.get(upc)

    def delivery_fee(self, quantity: int, postal_code: str) -> int:
        if not any(postal_code.startswith(prefix) for prefix in self.city_postal_code_prefixes):
            raise SellerPolicyError("The mock seller only quotes delivery within its configured city zone.")
        if quantity >= self.free_minimum_quantity:
            return 0
        return self.delivery_fee_amount
