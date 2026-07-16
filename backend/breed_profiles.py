"""Breed standard curves for Ross 308 and Cobb 500.

Values are approximations of published performance objectives (as-hatched, mixed sex)
for straight-run broilers. They are used as benchmarks in Mother Hen's reasoning
and as reference overlays on charts. Grow-day 0 = day of chick placement.
"""

# Target live weight (grams) by grow day
ROSS_308_WEIGHT = {
    0: 42, 1: 60, 2: 75, 3: 92, 4: 111, 5: 132, 6: 156, 7: 183,
    10: 275, 14: 430, 17: 570, 21: 800, 24: 990,
    28: 1300, 31: 1550, 35: 1900, 38: 2170, 42: 2600, 45: 2900, 49: 3300,
}

COBB_500_WEIGHT = {
    0: 42, 1: 58, 2: 73, 3: 89, 4: 107, 5: 128, 6: 151, 7: 176,
    10: 265, 14: 415, 17: 555, 21: 780, 24: 970,
    28: 1275, 31: 1520, 35: 1870, 38: 2140, 42: 2565, 45: 2860, 49: 3260,
}

# Recommended ambient temperature °C by grow day
ROSS_308_TEMP = {
    0: 34.0, 3: 32.0, 6: 30.0, 9: 28.0, 12: 26.5,
    15: 25.0, 18: 24.0, 21: 22.5, 25: 21.5, 30: 21.0, 35: 20.5, 42: 20.0, 49: 20.0,
}
COBB_500_TEMP = ROSS_308_TEMP.copy()

# Daily water consumption per bird (ml)
ROSS_308_WATER = {
    0: 30, 3: 55, 7: 100, 10: 140, 14: 200, 17: 240, 21: 300,
    24: 340, 28: 400, 31: 430, 35: 470, 38: 490, 42: 520, 45: 540, 49: 560,
}
COBB_500_WATER = ROSS_308_WATER.copy()

# Cumulative feed intake per bird (grams)
ROSS_308_FEED = {
    0: 0, 3: 45, 7: 165, 10: 285, 14: 500, 17: 720, 21: 1050,
    24: 1350, 28: 1800, 31: 2170, 35: 2700, 38: 3140, 42: 3760, 45: 4260, 49: 4880,
}
COBB_500_FEED = ROSS_308_FEED.copy()

BREED_STANDARDS = {
    "Ross 308": {
        "weight_g": ROSS_308_WEIGHT,
        "temp_c": ROSS_308_TEMP,
        "water_ml_per_bird": ROSS_308_WATER,
        "cum_feed_g_per_bird": ROSS_308_FEED,
        "target_fcr_at_42d": 1.55,
        "target_mortality_pct": 3.5,
    },
    "Cobb 500": {
        "weight_g": COBB_500_WEIGHT,
        "temp_c": COBB_500_TEMP,
        "water_ml_per_bird": COBB_500_WATER,
        "cum_feed_g_per_bird": COBB_500_FEED,
        "target_fcr_at_42d": 1.56,
        "target_mortality_pct": 3.5,
    },
}


def _interp(curve: dict, day: int) -> float:
    """Linear interpolation on a sparse day->value dict."""
    if day <= min(curve.keys()):
        return float(curve[min(curve.keys())])
    if day >= max(curve.keys()):
        return float(curve[max(curve.keys())])
    keys = sorted(curve.keys())
    for i, k in enumerate(keys):
        if k == day:
            return float(curve[k])
        if k > day:
            lo, hi = keys[i - 1], k
            frac = (day - lo) / (hi - lo)
            return float(curve[lo] + (curve[hi] - curve[lo]) * frac)
    return float(curve[keys[-1]])


def get_standard(breed: str, metric: str, day: int) -> float:
    """metric in: weight_g, temp_c, water_ml_per_bird, cum_feed_g_per_bird"""
    if breed not in BREED_STANDARDS:
        breed = "Ross 308"
    return _interp(BREED_STANDARDS[breed][metric], day)


def get_full_curve(breed: str, metric: str, max_day: int = 49) -> list:
    return [{"day": d, "value": round(get_standard(breed, metric, d), 2)} for d in range(0, max_day + 1)]
