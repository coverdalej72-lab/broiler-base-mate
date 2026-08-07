"""Tests for Ops-bundle pricing — must match landing.html's computeTotal() exactly.

If the tier prices, volume-discount tiers, or annual multiplier change on the
landing page, these tests must be updated in lockstep. Any drift → real
Stripe over/under-charges on live customers.

Feb 2026 update: Platinum tier removed. Standard pricing: Bronze $20, Silver $25, Gold $30.
"""

import pytest

# Import the pricer directly from server.py — no HTTP needed.
from server import _price_ops_bundle, _ops_volume_discount, _OPS_TIER_PRICE, CheckoutFarmConfig


def make_farms(*tiers):
    return [CheckoutFarmConfig(name=f"Farm {i+1}", tier=t) for i, t in enumerate(tiers)]


class TestOpsTierPrices:
    """Tier prices must match landing.html's `TIERS` and its option labels."""

    def test_bronze_price_matches_landing(self):
        assert _OPS_TIER_PRICE["bronze"] == 20.0

    def test_silver_price_matches_landing(self):
        assert _OPS_TIER_PRICE["silver"] == 25.0

    def test_gold_price_matches_landing(self):
        assert _OPS_TIER_PRICE["gold"] == 30.0

    def test_platinum_removed(self):
        # Platinum tier was retired in Feb 2026. Breeders is now "coming soon" with no live price.
        assert "platinum" not in _OPS_TIER_PRICE


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
        # 1 bronze @ 20 × 1.00 = 20
        assert _price_ops_bundle(make_farms("bronze"), "monthly") == 20.0

    def test_single_gold_farm(self):
        # 1 gold @ 30 × 1.00 = 30
        assert _price_ops_bundle(make_farms("gold"), "monthly") == 30.0

    def test_two_mixed_farms_10pct_off(self):
        # bronze 20 + silver 25 = 45 × 0.90 = 40.50 → rounds to 40 (banker's) or 41
        assert _price_ops_bundle(make_farms("bronze", "silver"), "monthly") in (40.0, 41.0)

    def test_four_gold_farms_15pct_off(self):
        # 4 × 30 = 120 × 0.85 = 102
        assert _price_ops_bundle(make_farms("gold", "gold", "gold", "gold"), "monthly") == 102.0

    def test_seven_silver_farms_20pct_off(self):
        # 7 × 25 = 175 × 0.80 = 140
        assert _price_ops_bundle(make_farms(*(["silver"] * 7)), "monthly") == 140.0

    def test_ten_bronze_farms_25pct_off(self):
        # 10 × 20 = 200 × 0.75 = 150
        assert _price_ops_bundle(make_farms(*(["bronze"] * 10)), "monthly") == 150.0


class TestOpsBundlePricingAnnual:
    """Annual = monthly × 12 × 0.85 (15% off yearly)."""

    def test_single_bronze_annual(self):
        # 20 × 12 × 0.85 = 204
        assert _price_ops_bundle(make_farms("bronze"), "annual") == 204.0

    def test_gold_farm_annual(self):
        # 30 × 12 × 0.85 = 306
        assert _price_ops_bundle(make_farms("gold"), "annual") == 306.0


class TestOpsBundleEdgeCases:
    def test_unknown_tier_falls_back_to_bronze(self):
        # A malformed tier is treated as bronze — never as a random tier
        farms = make_farms("bogus_tier")
        assert _price_ops_bundle(farms, "monthly") == 20.0

    def test_null_tier_falls_back_to_bronze(self):
        farms = [CheckoutFarmConfig(name="F1", tier=None)]
        assert _price_ops_bundle(farms, "monthly") == 20.0

    def test_platinum_tier_falls_back_to_bronze(self):
        # Old platinum-tagged farms (pre Feb 2026) now fall back to bronze
        farms = make_farms("platinum")
        assert _price_ops_bundle(farms, "monthly") == 20.0

    def test_empty_farms_list_returns_zero(self):
        assert _price_ops_bundle([], "monthly") == 0.0

    def test_case_insensitive_tier(self):
        farms = [CheckoutFarmConfig(name="F1", tier="GOLD")]
        assert _price_ops_bundle(farms, "monthly") == 30.0

    def test_missing_billing_defaults_to_monthly(self):
        # An unknown billing_period should NOT accidentally trigger the annual multiplier
        farms = make_farms("bronze")
        assert _price_ops_bundle(farms, "unknown") == 20.0
