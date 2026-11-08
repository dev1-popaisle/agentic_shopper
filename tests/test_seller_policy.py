import unittest

from a2a_agent.seller_policy import SellerNegotiationPolicy, SellerPolicyError


class SellerPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = SellerNegotiationPolicy(
            daily_sales_by_upc={
                "000000000001": 101,
                "000000000002": 100,
                "000000000003": 50,
                "000000000004": 49,
                "000000000005": 1,
                "000000000006": 0,
            },
            demand_discounts_percent={
                "above_100_units_per_day": 10,
                "50_to_100_units_per_day": 10,
                "1_to_49_units_per_day": 20,
                "zero_units_per_day": 25,
            },
            wait_discounts_percent={"7_days": 10, "14_days": 15},
            city_postal_code_prefixes=("941",),
            delivery_fee_amount=1000,
            free_minimum_quantity=3,
        )

    def test_demand_tier_boundaries(self) -> None:
        self.assertEqual(self.policy.demand_discount_percent("000000000001"), 10)
        self.assertEqual(self.policy.demand_discount_percent("000000000002"), 10)
        self.assertEqual(self.policy.demand_discount_percent("000000000003"), 10)
        self.assertEqual(self.policy.demand_discount_percent("000000000004"), 20)
        self.assertEqual(self.policy.demand_discount_percent("000000000005"), 20)
        self.assertEqual(self.policy.demand_discount_percent("000000000006"), 25)

    def test_wait_discounts_are_additive_policy_inputs(self) -> None:
        self.assertEqual(self.policy.wait_discount_percent(0), 0)
        self.assertEqual(self.policy.wait_discount_percent(7), 10)
        self.assertEqual(self.policy.wait_discount_percent(14), 15)

    def test_default_discount_is_available_only_for_configured_upcs(self) -> None:
        policy = SellerNegotiationPolicy(
            daily_sales_by_upc={},
            demand_discounts_percent={},
            wait_discounts_percent={},
            city_postal_code_prefixes=("941",),
            delivery_fee_amount=1000,
            free_minimum_quantity=3,
            default_discounts_by_upc={"049000028911": 5},
        )

        self.assertEqual(policy.default_discount_percent("049000028911"), 5)
        self.assertIsNone(policy.default_discount_percent("049000028904"))

    def test_default_discount_file_contains_diet_coke_offer(self) -> None:
        self.assertEqual(
            SellerNegotiationPolicy.load().default_discount_percent("049000028911"),
            5,
        )

    def test_city_delivery_fee_and_quantity_waiver(self) -> None:
        self.assertEqual(self.policy.delivery_fee(1, "94109"), 1000)
        self.assertEqual(self.policy.delivery_fee(2, "94109"), 1000)
        self.assertEqual(self.policy.delivery_fee(3, "94109"), 0)
        with self.assertRaisesRegex(SellerPolicyError, "configured city zone"):
            self.policy.delivery_fee(3, "10001")


if __name__ == "__main__":
    unittest.main()
