"""Demo data seeder for Coop Overwatch. Idempotent: only seeds if empty."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from models import Farm, Shed, Batch, Reading, Policy, Alert, new_id, now_iso
from breed_profiles import get_standard


DEMO_POLICIES = [
    ("temp_setpoint", "recommend", 3.0),
    ("fan_speed", "recommend", 10.0),
    ("damper", "auto", 5.0),
    ("cool_flap", "auto", 5.0),
    ("lighting", "manual", 0.0),
    ("ventilation", "recommend", 10.0),
]


async def seed_if_empty(db):
    farms = await db.farms.count_documents({})
    if farms > 0:
        return {"seeded": False, "reason": "existing data"}

    # Farms
    farm1 = Farm(name="Ridgeview Farm", location="Piet Retief, ZA", vendor_system="Rotem Platinum")
    farm2 = Farm(name="Golden Valley", location="Standerton, ZA", vendor_system="Fancom Lumina")
    await db.farms.insert_many([farm1.model_dump(), farm2.model_dump()])

    # Sheds
    sheds = [
        Shed(farm_id=farm1.id, name="Shed A1", capacity=22000, vendor_system="Rotem Platinum"),
        Shed(farm_id=farm1.id, name="Shed A2", capacity=22000, vendor_system="Rotem Platinum"),
        Shed(farm_id=farm1.id, name="Shed A3", capacity=18000, vendor_system="Rotem Platinum"),
        Shed(farm_id=farm2.id, name="Shed B1", capacity=25000, vendor_system="Fancom Lumina"),
        Shed(farm_id=farm2.id, name="Shed B2", capacity=25000, vendor_system="Fancom Lumina"),
    ]
    await db.sheds.insert_many([s.model_dump() for s in sheds])

    # Batches (mix of grow days and breeds)
    batches = []
    grow_days = [21, 14, 32, 8, 28]
    breeds = ["Ross 308", "Cobb 500", "Ross 308", "Cobb 500", "Ross 308"]
    for i, shed in enumerate(sheds):
        gd = grow_days[i]
        start = (datetime.now(timezone.utc) - timedelta(days=gd)).date().isoformat()
        birds = shed.capacity - random.randint(50, 400)  # some mortality already
        b = Batch(
            shed_id=shed.id,
            breed=breeds[i],
            start_date=start,
            bird_count_start=shed.capacity,
            bird_count_current=birds,
            mortality_total=shed.capacity - birds,
            status="active",
        )
        batches.append(b)
    await db.batches.insert_many([b.model_dump() for b in batches])

    # Historical readings (one per day per batch)
    readings = []
    for i, (shed, batch) in enumerate(zip(sheds, batches)):
        gd = grow_days[i]
        for day in range(0, gd + 1):
            temp_target = get_standard(batch.breed, "temp_c", day)
            water_target = get_standard(batch.breed, "water_ml_per_bird", day)
            weight_target = get_standard(batch.breed, "weight_g", day)
            ts = (datetime.now(timezone.utc) - timedelta(days=gd - day, hours=random.randint(0, 6))).isoformat()
            # inject a couple of anomalies for demo
            anomaly = (i == 2 and day == gd)  # shed A3 latest: hot
            crit = (i == 0 and day == gd)  # shed A1 latest: ammonia
            r = Reading(
                shed_id=shed.id,
                batch_id=batch.id,
                timestamp=ts,
                temp_c=round(temp_target + (2.8 if anomaly else random.uniform(-0.6, 0.6)), 1),
                temp_required_c=round(temp_target, 1),
                temp_outside_c=round(random.uniform(14, 26), 1),
                humidity_pct=round(random.uniform(55, 68), 1),
                static_pressure_pa=round(random.uniform(15, 32), 1),
                ammonia_ppm=round(random.uniform(6, 14) + (28 if crit else 0), 1),
                airflow_pct=round(random.uniform(40, 85), 1),
                damper_pct=round(random.uniform(30, 70), 1),
                cool_flap_pct=round(random.uniform(10, 45), 1),
                curtain_pct=round(random.uniform(0, 20), 1),
                heaters_on=random.choice([0, 0, 1, 2]) if day < 14 else 0,
                fans_on=random.choice([2, 3, 4, 5]) if day > 7 else random.choice([0, 1]),
                lighting_pct=100 if day < 3 else random.choice([40, 60, 80]),
                water_liters=round((water_target * batch.bird_count_current) / 1000 * random.uniform(0.85, 1.1), 1),
                feed_kg=round(get_standard(batch.breed, "cum_feed_g_per_bird", day) * batch.bird_count_current / 1000 * random.uniform(0.9, 1.05), 1),
                mortality_today=random.choice([0, 0, 0, 1, 2, 3, 4, 5]),
                avg_weight_g=round(weight_target * random.uniform(0.94, 1.03), 1),
                grow_day=day,
            )
            readings.append(r)
    await db.readings.insert_many([r.model_dump() for r in readings])

    # Policies
    policies = [Policy(action_type=a, mode=m, max_change_pct=p) for a, m, p in DEMO_POLICIES]
    await db.policies.insert_many([p.model_dump() for p in policies])

    return {"seeded": True, "farms": 2, "sheds": len(sheds), "batches": len(batches), "readings": len(readings)}
