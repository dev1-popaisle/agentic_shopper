import unittest
from datetime import timedelta

from a2a_agent.seller_schedule import SellerScheduleConfig


class SellerScheduleConfigTests(unittest.TestCase):
    def test_seller_address_and_default_availability_are_loaded(self) -> None:
        config = SellerScheduleConfig.load()

        self.assertEqual(
            config.marketplace_address,
            "1 Ferry Building, San Francisco, CA, 94105",
        )
        self.assertEqual(config.timezone, "America/Los_Angeles")
        self.assertEqual(config.available_weekdays, (0, 1, 2, 3, 4, 5))
        self.assertEqual(config.slot_minutes, 60)

    def test_nearest_delivery_slot_is_in_seller_business_hours(self) -> None:
        config = SellerScheduleConfig.load()
        preferred = config.local_today() + timedelta(days=1)
        requested = config.nearest_slot(
            config.next_slot(preferred).replace(hour=18),
            preferred,
        )

        self.assertIsNotNone(requested)
        self.assertIn(requested.weekday(), config.available_weekdays)
        self.assertGreaterEqual(requested.time(), config.open_time)
        self.assertLess(requested.time(), config.close_time)


if __name__ == "__main__":
    unittest.main()
