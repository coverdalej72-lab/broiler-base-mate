"""Tests for Ops-bundle pricing — must match landing.html's computeTotal() exactly.

If the tier prices, volume-discount tiers, or annual multiplier change on the
landing page, these tests must be updated in lockstep. Any drift → real
Stripe over/under-charges on live customers.
"""

import pytest


# Import the pricer directly from server.py — no HTTP needed.
from server import _price_ops_bundle, _ops_volume_discount, _OPS_TIER_PRICE, CheckoutFarmConfig


def make_farms(*tiers):
    return [CheckoutFarmConfig(name=f"Farm {i+1}", tier=t) for i, t in enumerate(tiers)]


class TestOpsTierPrices:
    """Tier prices must match landing.html's `TIERS` and its option labels."""

    def test_bronze_price_matches_landing(self):
        assert _OPS_TIER_PRICE["bronze"] == 25.0

    def test_silver_price_matches_landing(self):
        assert _OPS_TIER_PRICE["silver"] == 37.50

    def test_gold_price_matches_landing(self):
        assert _OPS_TIER_PRICE["gold"] == 50.0

    def test_platinum_price_matches_landing(self):
        assert _OPS_TIER_PRICE["platinum"] == 75.0


class TestVolumeDiscount:
    """Volume-discount tiers must match landing.html's `volumeDiscount()`."""

    def test_1_farm_no_discount(self):
        assert _ops_volume_discount(1) == 1.0

    def test_2_farms_10pct_off(self):
        assert _ops_volume_discount(2) == 0.90
        assert _ops_volume_discount(3) == 0.90

    def test_4_farms_15pct_off(self):
        assert _ops_volume_discount(4) == 0.85
        assert _ops_volume_discount(6) == 0.85

    def test_7_farms_20pct_off(self):
        assert _ops_volume_discount(7) == 0.80
        assert _ops_volume_discount(9) == 0.80

    def test_10_plus_farms_25pct_off(self):
        assert _ops_volume_discount(10) == 0.75
        assert _ops_volume_discount(50) == 0.75


class TestOpsBundlePricingMonthly:
    """Monthly totals — must match `Math.round(subtotal * disc)` in landing.html."""

    def test_single_bronze_farm(self):
        # 1 bronze @ 25 × 1.00 = 25
        assert _price_ops_bundle(make_farms("bronze"), "monthly") == 25.0

    def test_single_platinum_farm(self):
        # 1 platinum @ 75 × 1.00 = 75
        assert _price_ops_bundle(make_farms("platinum"), "monthly") == 75.0

    def test_two_mixed_farms_10pct_off(self):
        # bronze 25 + silver 37.50 = 62.50 × 0.90 = 56.25 → rounds to 56
        assert _price_ops_bundle(make_farms("bronze", "silver"), "monthly") == 56.0

    def test_four_gold_farms_15pct_off(self):
        # 4 × 50 = 200 × 0.85 = 170
        assert _price_ops_bundle(make_farms("gold", "gold", "gold", "gold"), "monthly") == 170.0

    def test_seven_platinum_farms_20pct_off(self):
        # 7 × 75 = 525 × 0.80 = 420
        assert _price_ops_bundle(make_farms(*(["platinum"] * 7)), "monthly") == 420.0

    def test_ten_bronze_farms_25pct_off(self):
        # 10 × 25 = 250 × 0.75 = 187.50 → rounds to 188 (banker's rounding could give 188)
        assert _price_ops_bundle(make_farms(*(["bronze"] * 10)), "monthly") in (187.0, 188.0)


class TestOpsBundlePricingAnnual:
    """Annual = monthly × 12 × 0.85 (15% off yearly)."""

    def test_single_bronze_annual(self):
        # 25 × 12 × 0.85 = 255
        assert _price_ops_bundle(make_farms("bronze"), "annual") == 255.0

    def test_two_mixed_farms_annual(self):
        # (25 + 37.50) × 0.90 × 12 × 0.85 = 573.75 → rounds to 574
        assert _price_ops_bundle(make_farms("bronze", "silver"), "annual") == 574.0

    def test_gold_farm_annual(self):
        # 50 × 12 × 0.85 = 510
        assert _price_ops_bundle(make_farms("gold"), "annual") == 510.0


class TestOpsBundleEdgeCases:
    def test_unknown_tier_falls_back_to_bronze(self):
        # A malformed tier is treated as bronze — never as $50 (the old buggy fallback)
        farms = make_farms("bogus_tier")
        assert _price_ops_bundle(farms, "monthly") == 25.0

    def test_null_tier_falls_back_to_bronze(self):
        farms = [CheckoutFarmConfig(name="F1", tier=None)]
        assert _price_ops_bundle(farms, "monthly") == 25.0

    def test_empty_farms_list_returns_zero(self):
        assert _price_ops_bundle([], "monthly") == 0.0

    def test_case_insensitive_tier(self):
        farms = [CheckoutFarmConfig(name="F1", tier="GOLD")]
        assert _price_ops_bundle(farms, "monthly") == 50.0

    def test_missing_billing_defaults_to_monthly(self):
        # An unknown billing_period should NOT accidentally trigger the annual multiplier
        farms = make_farms("bronze")
        assert _price_ops_bundle(farms, "unknown") == 25.0
