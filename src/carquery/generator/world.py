"""The static part of the simulation: dimensions, supply batches and lookup helpers.

Everything here is independent of vehicle volume except the dealer and fleet-customer counts,
so it is built once and shared by every build-month chunk.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pyarrow as pa

from carquery.generator.calendar import date_array, to_day
from carquery.generator.config import GeneratorConfig, Preset, ReferenceData

NEVER = np.iinfo(np.int32).max  # "closed_date" sentinel for dealers that never close
TRACKED_CATEGORIES = ["Brakes", "Drive", "Electrical", "Infotainment", "Suspension", "Body"]
# "Drive" = Battery for PHEV/BEV, Powertrain for ICE/Hybrid.


def rng_for(seed: int, stream: str, chunk: int = 0) -> np.random.Generator:
    """Independent, reproducible random stream per (seed, stream name, chunk)."""
    return np.random.default_rng([seed, zlib.crc32(stream.encode()), chunk])


def weighted_choice(rng: np.random.Generator, n: int, weights: np.ndarray) -> np.ndarray:
    weights = np.asarray(weights, dtype=float)
    return rng.choice(len(weights), size=n, p=weights / weights.sum())


def logit(p: float) -> float:
    return float(np.log(p / (1 - p)))


@dataclass
class Timeline:
    history_start: int
    end: int  # last day of the history
    horizon_end: int  # last day `generate day N` can reach

    def frac_to_day(self, frac: float) -> int:
        return round(self.history_start + frac * (self.end - self.history_start))


@dataclass
class World:
    cfg: GeneratorConfig
    ref: ReferenceData
    preset: Preset
    timeline: Timeline
    regions: list[str]
    region_weights: np.ndarray
    # trims (dim_vehicle_model rows), indexed 0..n-1; model_id = index + 1
    trims: dict[str, np.ndarray]
    trim_parts: np.ndarray  # [n_trims, 6] part index per tracked slot
    model_names: list[str]
    plants: dict[str, np.ndarray]
    plant_builds: np.ndarray  # [n_plants, n_model_names] bool
    dealers: dict[str, np.ndarray]
    region_dealers: list[np.ndarray]
    anchor_dealer: np.ndarray  # per region: a dealer open over the whole timeline
    suppliers: dict[str, np.ndarray]
    parts: dict[str, np.ndarray]
    fleet: dict[str, np.ndarray]
    region_fleet: list[np.ndarray]
    supply: dict[str, np.ndarray]
    supply_lookup_keys: np.ndarray
    p1_window: tuple[int, int]
    tables: dict[str, pa.Table] = field(default_factory=dict)

    # ---------------------------------------------------------------- lookups
    def region_index(self, name: str) -> int:
        return self.regions.index(name)

    def plant_index(self, name: str) -> int:
        return int(np.flatnonzero(self.plants["plant_name"] == name)[0])

    def part_index(self, name: str) -> int:
        return int(np.flatnonzero(self.parts["part_name"] == name)[0])

    def supplier_index(self, name: str) -> int:
        return int(np.flatnonzero(self.suppliers["supplier_name"] == name)[0])

    def model_name_index(self, name: str) -> int:
        return self.model_names.index(name)

    def dealer_open(self, dealer: np.ndarray, day: np.ndarray) -> np.ndarray:
        return (self.dealers["opened"][dealer] <= day) & (day < self.dealers["closed"][dealer])

    def pick_dealers(
        self, rng: np.random.Generator, region: np.ndarray, *check_days: np.ndarray
    ) -> np.ndarray:
        """A dealer in each row's region that is open on every one of ``check_days``."""
        result = np.full(len(region), -1, dtype=np.int64)
        for r, candidates in enumerate(self.region_dealers):
            pending = np.flatnonzero(region == r)
            weights = self.dealers["sales_weight"][candidates]
            for _ in range(12):
                if not len(pending):
                    break
                pick = candidates[weighted_choice(rng, len(pending), weights)]
                ok = np.ones(len(pending), dtype=bool)
                for days in check_days:
                    ok &= self.dealer_open(pick, days[pending])
                result[pending[ok]] = pick[ok]
                pending = pending[~ok]
            result[pending] = self.anchor_dealer[r]
        return result

    def batch_for(self, part: np.ndarray, plant: np.ndarray, day: np.ndarray) -> np.ndarray:
        """Index of the latest supply batch of ``part`` delivered to ``plant`` on or before
        ``day``."""
        keys = _supply_key(part, plant, day, len(self.plants["plant_id"]))
        index = np.searchsorted(self.supply_lookup_keys, keys, side="right") - 1
        n_plants = len(self.plants["plant_id"])
        same = (self.supply["part"][index] * n_plants + self.supply["plant"][index]) == (
            part * n_plants + plant
        )
        if not same.all():
            raise RuntimeError("no supply batch found for some vehicles (supply starts too late)")
        return index


def _supply_key(part: np.ndarray, plant: np.ndarray, day: np.ndarray, n_plants: int) -> np.ndarray:
    return (np.asarray(part) * n_plants + np.asarray(plant)) * 10_000_000 + np.asarray(day)


# --------------------------------------------------------------------------------------
# Building the world
# --------------------------------------------------------------------------------------


def build_world(cfg: GeneratorConfig, ref: ReferenceData) -> World:
    preset = cfg.active_preset
    end = cfg.end_date
    start = _years_before(end, cfg.history_years) + timedelta(days=1)
    timeline = Timeline(to_day(start), to_day(end), to_day(end) + cfg.day_horizon_days)

    regions = [r.name for r in ref.regions]
    region_weights = np.array([r.weight for r in ref.regions])

    trims, model_names = _trims(ref)
    plants, plant_builds = _plants(ref, model_names)
    suppliers = _suppliers(ref)
    parts = _parts(ref, suppliers)
    trim_parts = _trim_parts(ref, trims, model_names)
    dealers = _dealers(cfg, ref, preset, timeline)
    fleet = _fleet_customers(cfg, ref, preset)

    world = World(
        cfg=cfg,
        ref=ref,
        preset=preset,
        timeline=timeline,
        regions=regions,
        region_weights=region_weights,
        trims=trims,
        trim_parts=trim_parts,
        model_names=model_names,
        plants=plants,
        plant_builds=plant_builds,
        dealers=dealers,
        region_dealers=[np.flatnonzero(dealers["region"] == r) for r in range(len(regions))],
        anchor_dealer=np.array(
            [
                np.flatnonzero((dealers["region"] == r) & dealers["anchor"])[0]
                for r in range(len(regions))
            ]
        ),
        suppliers=suppliers,
        parts=parts,
        fleet=fleet,
        region_fleet=[np.flatnonzero(fleet["region"] == r) for r in range(len(regions))],
        supply={},
        supply_lookup_keys=np.array([]),
        p1_window=(0, 0),
    )
    _build_supply(world)
    return world


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:  # 29 February
        return value.replace(year=value.year - years, day=28)


def _trims(ref: ReferenceData) -> tuple[dict[str, np.ndarray], list[str]]:
    rows: list[dict[str, object]] = []
    model_names: list[str] = []
    for brand in ref.brands:
        for model in brand.models:
            model_names.append(model.name)
            for trim in model.trims:
                rows.append(
                    {
                        "brand": brand.name,
                        "wmi": brand.wmi,
                        "model_name": model.name,
                        "model_name_idx": len(model_names) - 1,
                        "body_type": model.body_type,
                        "segment": model.segment,
                        **trim.model_dump(),
                    }
                )
    columns = {key: np.array([row[key] for row in rows]) for key in rows[0]}
    columns["last_model_year"] = np.array(
        [9999 if row["last_model_year"] is None else row["last_model_year"] for row in rows]
    )
    columns["model_id"] = np.arange(1, len(rows) + 1)
    return columns, model_names


def _plants(ref: ReferenceData, model_names: list[str]) -> tuple[dict[str, np.ndarray], np.ndarray]:
    plants = {
        "plant_id": np.arange(1, len(ref.plants) + 1),
        "plant_name": np.array([p.name for p in ref.plants]),
        "capacity": np.array([p.daily_capacity for p in ref.plants], dtype=float),
    }
    builds = np.zeros((len(ref.plants), len(model_names)), dtype=bool)
    for i, plant in enumerate(ref.plants):
        for name in plant.models:
            builds[i, model_names.index(name)] = True
    return plants, builds


def _suppliers(ref: ReferenceData) -> dict[str, np.ndarray]:
    return {
        "supplier_id": np.arange(1, len(ref.suppliers) + 1),
        "supplier_name": np.array([s.name for s in ref.suppliers]),
    }


def _parts(ref: ReferenceData, suppliers: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    names = list(suppliers["supplier_name"])
    return {
        "part_id": np.arange(1, len(ref.parts) + 1),
        "part_name": np.array([p.name for p in ref.parts]),
        "category": np.array([p.category for p in ref.parts]),
        "unit_cost": np.array([p.unit_cost for p in ref.parts]),
        "primary": np.array([names.index(p.primary_supplier) for p in ref.parts]),
        "secondary": np.array([names.index(p.secondary_supplier) for p in ref.parts]),
        "secondary_share": np.array(
            [np.nan if p.secondary_share is None else p.secondary_share for p in ref.parts]
        ),
    }


def _trim_parts(
    ref: ReferenceData, trims: dict[str, np.ndarray], model_names: list[str]
) -> np.ndarray:
    result = np.zeros((len(trims["model_id"]), len(TRACKED_CATEGORIES)), dtype=np.int64)
    for t in range(len(trims["model_id"])):
        model, powertrain = trims["model_name"][t], trims["powertrain"][t]
        for slot, category in enumerate(TRACKED_CATEGORIES):
            if category == "Drive":
                category = "Battery" if powertrain in ("PHEV", "BEV") else "Powertrain"
            for p, part in enumerate(ref.parts):
                if (
                    part.category == category
                    and (part.models is None or model in part.models)
                    and (part.powertrains is None or powertrain in part.powertrains)
                ):
                    result[t, slot] = p
                    break
            else:
                raise ValueError(f"no {category} part matches {model} {powertrain}")
    return result


def _dealers(
    cfg: GeneratorConfig, ref: ReferenceData, preset: Preset, timeline: Timeline
) -> dict[str, np.ndarray]:
    rng = rng_for(cfg.seed, "dealers")
    n = preset.dealers
    n_regions = len(ref.regions)
    region = np.sort(weighted_choice(rng, n, np.array([r.weight for r in ref.regions])))
    # Make sure every region has at least two dealers.
    region[: 2 * n_regions] = np.repeat(np.arange(n_regions), 2)
    region = np.sort(region)

    country, city = _places(rng, ref, region)
    tiers = list(cfg.dealers.tier_shares)
    tier = np.array(tiers)[
        weighted_choice(rng, n, np.array(list(cfg.dealers.tier_shares.values())))
    ]
    words, suffixes = ref.dealer_name_words, ref.dealer_name_suffixes
    names = [
        f"{city[i]} {words[rng.integers(len(words))]} {suffixes[rng.integers(len(suffixes))]}"
        for i in range(n)
    ]
    names = _dedupe(names)

    # The first dealer of each region is an "anchor": established and never closes.
    anchor = np.zeros(n, dtype=bool)
    anchor[np.unique(region, return_index=True)[1]] = True
    hs, horizon = timeline.history_start, timeline.horizon_end
    opened = hs - rng.integers(365, 30 * 365, size=n)
    new = (rng.random(n) < cfg.dealers.new_dealer_share) & ~anchor
    opened[new] = rng.integers(hs + 30, horizon + 1, size=new.sum())
    closed = np.full(n, NEVER, dtype=np.int64)
    closes = (rng.random(n) < cfg.dealers.closure_rate) & ~anchor & ~new
    closed[closes] = rng.integers(hs + 120, horizon + 1, size=closes.sum())

    weight = np.array([cfg.dealers.tier_sales_weight[t] for t in tier])

    # P4: a few established, never-closing, non-flagship dealers are consistently slow to sell.
    p4 = cfg.patterns.p4_slow_dealers
    eligible = np.flatnonzero(~anchor & ~new & ~closes & (tier != "Flagship"))
    slow_idx = rng.choice(eligible, size=min(p4.count, len(eligible)), replace=False)
    slow = np.zeros(n, dtype=bool)
    slow[slow_idx] = True
    days_to_sell = np.where(slow, p4.days_to_sell_mean, cfg.sales.days_to_sell_mean)
    return {
        "dealer_id": np.arange(1, n + 1),
        "dealer_name": np.array(names),
        "country": country,
        "city": city,
        "region": region,
        "tier": tier,
        "opened": opened,
        "closed": closed,
        "anchor": anchor,
        "sales_weight": weight,
        "days_to_sell_mean": days_to_sell,
        "slow": slow,
    }


def _fleet_customers(
    cfg: GeneratorConfig, ref: ReferenceData, preset: Preset
) -> dict[str, np.ndarray]:
    rng = rng_for(cfg.seed, "fleet_customers")
    n = preset.fleet_customers
    region = weighted_choice(rng, n, np.array([r.weight for r in ref.regions]))
    country, city = _places(rng, ref, region)
    # Fleet sizes are skewed: a few big fleets buy most vehicles.
    size_weight = 1.0 / np.arange(1, n + 1) ** 0.8
    return {
        "customer_id": np.arange(1, n + 1),
        "region": region,
        "country": country,
        "city": city,
        "weight": rng.permutation(size_weight),
        "first_purchase": np.full(n, NEVER, dtype=np.int64),
    }


def _places(
    rng: np.random.Generator, ref: ReferenceData, region: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    country = np.empty(len(region), dtype=object)
    city = np.empty(len(region), dtype=object)
    for r, reg in enumerate(ref.regions):
        rows = np.flatnonzero(region == r)
        c_idx = weighted_choice(rng, len(rows), np.array([c.weight for c in reg.countries]))
        for c, ctry in enumerate(reg.countries):
            sub = rows[c_idx == c]
            country[sub] = ctry.name
            city[sub] = np.array(ctry.cities)[rng.integers(len(ctry.cities), size=len(sub))]
    return country.astype(str), city.astype(str)


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        result.append(name if seen[name] == 1 else f"{name} {seen[name]}")
    return result


def _build_supply(world: World) -> None:
    """Weekly batches of every tracked part to every plant that fits it."""
    cfg, rng = world.cfg, rng_for(world.cfg.seed, "supply")
    tl = world.timeline
    n_plants = len(world.plants["plant_id"])
    first = tl.history_start - cfg.supply.lead_days

    # which (part, plant) pairs are needed
    pairs = set()
    for t in range(len(world.trims["model_id"])):
        plants = np.flatnonzero(world.plant_builds[:, world.trims["model_name_idx"][t]])
        for part in world.trim_parts[t]:
            pairs.update((int(part), int(p)) for p in plants)
    pairs_sorted = sorted(pairs)

    parts_col, plant_col, day_col = [], [], []
    for part, plant in pairs_sorted:
        days = np.arange(first + rng.integers(0, 7), tl.horizon_end + 1, 7)
        parts_col.append(np.full(len(days), part))
        plant_col.append(np.full(len(days), plant))
        day_col.append(days)
    part = np.concatenate(parts_col)
    plant = np.concatenate(plant_col)
    day = np.concatenate(day_col)
    n = len(part)

    share = world.parts["secondary_share"][part]
    share = np.where(np.isnan(share), cfg.supply.secondary_share, share)
    secondary = rng.random(n) < share
    supplier = np.where(secondary, world.parts["secondary"][part], world.parts["primary"][part])

    weeks = max(1.0, (tl.end - tl.history_start) / 7)
    base_qty = max(40.0, world.preset.vehicles / weeks / n_plants * 1.3)
    quantity = np.round(base_qty * rng.uniform(0.8, 1.3, size=n)).astype(np.int64)
    years_in = (day - tl.history_start) / 365.0
    unit_cost = np.round(
        world.parts["unit_cost"][part]
        * (1 + 0.02 * years_in)
        * np.where(secondary, 1.03, 1.0)
        * rng.uniform(0.97, 1.03, size=n),
        2,
    )
    defect = rng.gamma(2.0, cfg.supply.defect_rate_mean / 2, size=n)

    # P1: batches of the brake part from its primary supplier to the P1 plant during the window.
    p1 = cfg.patterns.p1_brake_batch
    w_start = tl.frac_to_day(p1.window_start_frac)
    w_end = w_start + 7 * p1.window_weeks
    p1_part, p1_plant = world.part_index(p1.part), world.plant_index(p1.plant)
    defective = (
        (part == p1_part)
        & (plant == p1_plant)
        & (supplier == world.parts["primary"][p1_part])
        & (day >= w_start - 6)
        & (day < w_end)
    )
    defect[defective] = p1.batch_defect_rate * rng.uniform(0.8, 1.2, size=defective.sum())
    defect = np.round(np.clip(defect, 0, 1), 4)
    defect_null = rng.random(n) < cfg.supply.defect_rate_null_rate

    order = np.argsort(_supply_key(part, plant, day, n_plants), kind="stable")
    world.supply = {
        "supply_batch_id": np.arange(1, n + 1) + 100_000,
        "part": part[order],
        "plant": plant[order],
        "supplier": supplier[order],
        "day": day[order],
        "quantity": quantity[order],
        "unit_cost": unit_cost[order],
        "defect": defect[order],
        "defect_null": defect_null[order],
        "defective": defective[order],
    }
    world.supply_lookup_keys = _supply_key(
        world.supply["part"], world.supply["plant"], world.supply["day"], n_plants
    )
    world.p1_window = (w_start, w_end)


# --------------------------------------------------------------------------------------
# Dimension tables (staging form: contract columns + optional `_available` snapshot date)
# --------------------------------------------------------------------------------------


def dimension_tables(world: World) -> dict[str, pa.Table]:
    ref, tr = world.ref, world.trims

    def nullable(values: np.ndarray, missing: np.ndarray) -> pa.Array:
        return pa.array(np.where(missing, 0, values), mask=missing)

    tables = {
        "dim_vehicle_model": pa.table(
            {
                "model_id": tr["model_id"],
                "brand": tr["brand"],
                "model_name": tr["model_name"],
                "trim": tr["trim"],
                "body_type": tr["body_type"],
                "segment": tr["segment"],
                "powertrain": tr["powertrain"],
                "engine_displacement_l": _float_or_null(tr["engine_displacement_l"]),
                "battery_kwh": _float_or_null(tr["battery_kwh"]),
                "range_km": _float_or_null(tr["range_km"]),
                "fuel_consumption_l_per_100km": _float_or_null(tr["fuel_consumption_l_per_100km"]),
                "first_model_year": tr["first_model_year"],
                "last_model_year": nullable(tr["last_model_year"], tr["last_model_year"] == 9999),
                "base_msrp": tr["base_msrp"].astype(float),
                "_available": date_array(
                    [to_day(date(int(y) - 1, 7, 1)) for y in tr["first_model_year"]]
                ),
            }
        ),
        "dim_plant": pa.table(
            {
                "plant_id": world.plants["plant_id"],
                "plant_name": world.plants["plant_name"],
                "country": [p.country for p in ref.plants],
                "city": [p.city for p in ref.plants],
                "region": [p.region for p in ref.plants],
                "daily_capacity": [p.daily_capacity for p in ref.plants],
                "opened_year": [p.opened_year for p in ref.plants],
            }
        ),
        "dim_supplier": pa.table(
            {
                "supplier_id": world.suppliers["supplier_id"],
                "supplier_name": world.suppliers["supplier_name"],
                "country": [s.country for s in ref.suppliers],
                "supplier_tier": [s.tier for s in ref.suppliers],
                "quality_rating": [s.quality_rating for s in ref.suppliers],
            }
        ),
        "dim_part": pa.table(
            {
                "part_id": world.parts["part_id"],
                "part_name": world.parts["part_name"],
                "component_category": world.parts["category"],
                "unit_cost": world.parts["unit_cost"].astype(float),
                "primary_supplier_id": world.suppliers["supplier_id"][world.parts["primary"]],
            }
        ),
    }
    d = world.dealers
    closed = d["closed"] == NEVER
    tables["dim_dealer"] = pa.table(
        {
            "dealer_id": d["dealer_id"],
            "dealer_name": d["dealer_name"],
            "country": d["country"],
            "city": d["city"],
            "region": np.array(world.regions)[d["region"]],
            "dealer_tier": d["tier"],
            "opened_date": date_array(d["opened"]),
            "closed_date": date_array(np.where(closed, 0, d["closed"]), null_mask=closed),
            "is_active": closed,  # recomputed as of the snapshot date by the writer
            "_available": date_array(d["opened"]),
        }
    )
    return tables


def _float_or_null(values: np.ndarray) -> pa.Array:
    as_float = np.array([np.nan if v is None else float(v) for v in values])
    return pa.array(np.nan_to_num(as_float), mask=np.isnan(as_float))


def supply_table(world: World) -> pa.Table:
    s = world.supply
    return pa.table(
        {
            "supply_batch_id": s["supply_batch_id"],
            "part_id": world.parts["part_id"][s["part"]],
            "supplier_id": world.suppliers["supplier_id"][s["supplier"]],
            "plant_id": world.plants["plant_id"][s["plant"]],
            "delivery_date": date_array(s["day"]),
            "quantity": s["quantity"],
            "unit_cost": s["unit_cost"],
            "inspected_defect_rate": pa.array(s["defect"], mask=s["defect_null"]),
            "_arrival": date_array(s["day"]),
        }
    )
