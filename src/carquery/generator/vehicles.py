"""Production, tracked components, sales and customers for one chunk of build months."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from carquery.generator.calendar import date_array, model_year, months, quarter_end, weekday
from carquery.generator.world import TRACKED_CATEGORIES, World, logit, rng_for, weighted_choice

SALE_TYPES = np.array(["Retail", "Fleet", "Lease"])


@dataclass
class Counters:
    """Running id counters shared by all chunks (chunks run in a fixed order)."""

    vehicle: int = 0
    sale: int = 0
    customer: int = 0
    visit: int = 0
    claim: int = 0


@dataclass
class Vehicles:
    """Per-vehicle state that the after-sales simulation needs."""

    vin: np.ndarray
    trim: np.ndarray
    plant: np.ndarray
    build: np.ndarray
    region: np.ndarray
    parts: np.ndarray  # [n, 6] part index
    batches: np.ndarray  # [n, 6] supply batch index
    sold: np.ndarray  # bool: has an effective (non-cancelled) sale within the horizon
    sale_day: np.ndarray  # effective sale day (valid where sold)
    dealer: np.ndarray
    sale_type: np.ndarray  # index into SALE_TYPES


def production_schedule(world: World) -> tuple[np.ndarray, np.ndarray]:
    """Vehicles built per working day, from the history start to the day horizon."""
    tl, cfg = world.timeline, world.cfg
    days = np.arange(tl.history_start, tl.horizon_end + 1)
    days = days[weekday(days) <= 5]
    weight = np.array(cfg.production.monthly_seasonality)[months(days) - 1]
    rate = weight * world.preset.vehicles / weight[days <= tl.end].sum()
    counts = rng_for(cfg.seed, "schedule").poisson(rate)
    return days, counts


def chunk_days(world: World, days: np.ndarray) -> list[np.ndarray]:
    """Split build days into chunks of ``chunk_months`` calendar months."""
    month_index = days.astype("datetime64[D]").astype("datetime64[M]").astype(np.int64)
    chunk = (month_index - month_index[0]) // world.preset.chunk_months
    return [np.flatnonzero(chunk == c) for c in range(int(chunk.max()) + 1)]


def simulate_production(
    world: World, chunk: int, build_days: np.ndarray, counters: Counters
) -> tuple[Vehicles, dict[str, pa.Table]]:
    cfg, tl, tr = world.cfg, world.timeline, world.trims
    rng = rng_for(cfg.seed, "production", chunk)
    build = build_days
    n = len(build)

    # Demand region and BEV share (P2: logistic growth, different rate per region).
    region = weighted_choice(rng, n, world.region_weights)
    t = (build - tl.history_start) / max(1, tl.end - tl.history_start)
    p_bev = np.zeros(n)
    for r, name in enumerate(world.regions):
        lo, hi = cfg.patterns.p2_bev_share[name]
        z = logit(lo) + t * (logit(hi) - logit(lo))
        p_bev = np.where(region == r, 1 / (1 + np.exp(-z)), p_bev)
    mix = cfg.production.powertrain_mix_non_bev
    powertrain = np.array(list(mix))[weighted_choice(rng, n, np.array(list(mix.values())))]
    powertrain = np.where(rng.random(n) < p_bev, "BEV", powertrain)

    # Trim: available in the vehicle's model year, matching the powertrain, by popularity.
    my = model_year(build)
    trim = np.full(n, -1)
    for pt in np.unique(powertrain):
        for year in np.unique(my):
            rows = np.flatnonzero((powertrain == pt) & (my == year))
            ok = (
                (tr["powertrain"] == pt)
                & (tr["first_model_year"] <= year)
                & (tr["last_model_year"] >= year)
            )
            if not ok.any():
                raise ValueError(f"no {pt} trim available in model year {year}")
            candidates = np.flatnonzero(ok)
            trim[rows] = candidates[weighted_choice(rng, len(rows), tr["weight"][candidates])]

    # Plant: one of the plants building the model, weighted by capacity.
    plant = np.full(n, -1)
    model_idx = tr["model_name_idx"][trim]
    for m in np.unique(model_idx):
        rows = np.flatnonzero(model_idx == m)
        candidates = np.flatnonzero(world.plant_builds[:, m])
        plant[rows] = candidates[
            weighted_choice(rng, len(rows), world.plants["capacity"][candidates])
        ]

    # Quality (P5: line change at one plant for one model lowers the first-pass rate).
    p5 = cfg.patterns.p5_line_change
    p5_rows = (
        (plant == world.plant_index(p5.plant))
        & (model_idx == world.model_name_index(p5.model))
        & (build >= tl.frac_to_day(p5.change_frac))
    )
    pass_rate = np.where(p5_rows, p5.pass_rate_after, cfg.production.first_inspection_pass_rate)
    passed = rng.random(n) < pass_rate
    rework_mean = np.where(p5_rows, p5.rework_hours_mean_after, cfg.production.rework_hours_mean)
    rework = np.where(passed, 0.0, np.round(np.maximum(0.5, rng.gamma(2.0, rework_mean / 2)), 1))

    colours = world.ref.colours
    colour = np.array(list(colours))[weighted_choice(rng, n, np.array(list(colours.values())))]
    colour_null = rng.random(n) < cfg.production.colour_null_rate
    lo, hi = cfg.production.production_cost_share
    cost = np.round(tr["base_msrp"][trim] * rng.uniform(lo, hi, size=n), 2)

    serial = counters.vehicle + np.arange(1, n + 1)
    counters.vehicle += n
    vin = _vins(tr["wmi"][trim], tr["model_id"][trim], plant, my, serial)

    # Tracked components: latest batch delivered to the plant on or before the build day.
    parts = world.trim_parts[trim]
    batches = np.column_stack(
        [world.batch_for(parts[:, s], plant, build) for s in range(len(TRACKED_CATEGORIES))]
    )

    production = pa.table(
        {
            "vin": vin,
            "model_id": tr["model_id"][trim],
            "plant_id": world.plants["plant_id"][plant],
            "build_date": date_array(build),
            "model_year": my,
            "exterior_colour": pa.array(colour, mask=colour_null),
            "production_cost": cost,
            "passed_first_inspection": passed,
            "rework_hours": rework,
            "_arrival": date_array(build),
        }
    )
    components = pa.table(
        {
            "vin": np.repeat(vin, parts.shape[1]),
            "part_id": world.parts["part_id"][parts.ravel()],
            "supply_batch_id": world.supply["supply_batch_id"][batches.ravel()],
            "_arrival": date_array(np.repeat(build, parts.shape[1])),
        }
    )
    vehicles = Vehicles(
        vin=vin,
        trim=trim,
        plant=plant,
        build=build,
        region=region,
        parts=parts,
        batches=batches,
        sold=np.zeros(n, dtype=bool),
        sale_day=np.zeros(n, dtype=np.int64),
        dealer=np.full(n, -1),
        sale_type=np.zeros(n, dtype=np.int64),
    )
    return vehicles, {"fact_production": production, "fact_vehicle_component": components}


def _vins(
    wmi: np.ndarray, model_id: np.ndarray, plant: np.ndarray, my: np.ndarray, serial: np.ndarray
) -> np.ndarray:
    """17 characters: WMI(3) + model id(3) + plant letter(1) + model year(2) + serial(8)."""
    plant_letter = np.array(list("ABCDEFGHJKLMNPRSTUVWXYZ"))[plant]
    parts = [
        pa.array(wmi),
        pc.utf8_lpad(pc.cast(pa.array(model_id), pa.string()), 3, "0"),
        pa.array(plant_letter),
        pc.utf8_lpad(pc.cast(pa.array(my % 100), pa.string()), 2, "0"),
        pc.utf8_lpad(pc.cast(pa.array(serial), pa.string()), 8, "0"),
    ]
    return pc.binary_join_element_wise(*parts, "").to_numpy(zero_copy_only=False).astype(str)


# --------------------------------------------------------------------------------------
# Sales and customers
# --------------------------------------------------------------------------------------


def simulate_sales(
    world: World, chunk: int, v: Vehicles, counters: Counters
) -> dict[str, pa.Table]:
    cfg, tl = world.cfg, world.timeline
    sc, p3 = cfg.sales, cfg.patterns.p3_seasonality
    rng = rng_for(cfg.seed, "sales", chunk)
    n = len(v.vin)

    lo, hi = cfg.production.transit_days
    arrival = v.build + rng.integers(lo, hi + 1, size=n)
    dealer = world.pick_dealers(rng, v.region, arrival, arrival + 150)

    # Days to sell: dealer-specific (P4), then seasonal demand thinning and quarter-end pull (P3).
    dts_mean = world.dealers["days_to_sell_mean"][dealer]
    sale = arrival + np.round(rng.gamma(2.0, dts_mean / 2)).astype(np.int64)
    demand = np.array(p3.monthly_demand)
    for _ in range(6):
        reject = rng.random(n) > demand[months(sale) - 1] / demand.max()
        sale = np.where(reject, sale + np.round(rng.gamma(1.0, 8.0, size=n)).astype(np.int64), sale)
    q_end = quarter_end(sale)
    pull = (q_end - sale <= p3.quarter_end_lookahead_days) & (
        rng.random(n) < p3.quarter_end_pull_probability
    )
    target = q_end - rng.integers(0, p3.quarter_end_window_days, size=n)
    sale = np.where(pull, np.maximum(sale, target), sale)
    sale = np.minimum(sale, world.dealers["closed"][dealer] - 1)
    in_quarter_end = sale > quarter_end(sale) - p3.quarter_end_window_days

    has_sale = sale <= tl.horizon_end
    idx = np.flatnonzero(has_sale)
    sale_type = weighted_choice(rng, n, np.array([sc.sale_type_shares[s] for s in SALE_TYPES]))

    # Cancellations; most cancelled vehicles are resold (Retail) a few weeks later.
    cancelled = rng.random(n) < sc.cancellation_rate
    resale = sale + rng.integers(sc.resale_days[0], sc.resale_days[1] + 1, size=n)
    resold = (
        cancelled
        & (rng.random(n) < sc.resale_rate)
        & (resale <= tl.horizon_end)
        & (resale < world.dealers["closed"][dealer])
    )

    first = _sale_rows(
        world, rng, v, idx, arrival, sale, dealer, sale_type, cancelled, in_quarter_end, counters
    )
    re_idx = np.flatnonzero(has_sale & resold)
    retail = np.zeros(n, dtype=np.int64)
    second = _sale_rows(
        world,
        rng,
        v,
        re_idx,
        arrival,
        resale,
        dealer,
        retail,
        np.zeros(n, dtype=bool),
        np.zeros(n, dtype=bool),
        counters,
    )

    v.sold = has_sale & (~cancelled | resold)
    v.sale_day = np.where(cancelled, resale, sale)
    v.dealer = dealer
    v.sale_type = np.where(cancelled, 0, sale_type)

    sales = pa.concat_tables([first["sales"], second["sales"]])
    customers = pa.concat_tables([first["customers"], second["customers"]])
    return {"fact_sales": sales, "_individual_customers": customers}


def _sale_rows(
    world: World,
    rng: np.random.Generator,
    v: Vehicles,
    idx: np.ndarray,
    arrival: np.ndarray,
    sale: np.ndarray,
    dealer: np.ndarray,
    sale_type: np.ndarray,
    cancelled: np.ndarray,
    quarter_end_sale: np.ndarray,
    counters: Counters,
) -> dict[str, pa.Table]:
    cfg, tl, tr = world.cfg, world.timeline, world.trims
    sc = cfg.sales
    m = len(idx)
    stype = sale_type[idx]
    day = sale[idx]
    d = dealer[idx]

    # Pricing (P6: fleet gets much larger discounts).
    years_in = (day - tl.history_start) / 365.0
    list_price = np.round(
        tr["base_msrp"][v.trim[idx]]
        * (1 + rng.uniform(*sc.options_uplift, size=m))
        * (1 + sc.annual_price_increase) ** years_in,
        2,
    )
    ranges = np.array([sc.discount_ranges[s] for s in SALE_TYPES])
    rate = rng.uniform(ranges[stype, 0], ranges[stype, 1])
    rate = rate + np.where(
        quarter_end_sale[idx], cfg.patterns.p3_seasonality.quarter_end_extra_discount, 0
    )
    discount = np.round(list_price * rate, 2)
    net = np.round(list_price - discount, 2)

    # Customers: fleet sales go to an existing fleet customer in the region, everything else
    # creates a new individual customer.
    customer = np.zeros(m, dtype=np.int64)
    is_fleet = stype == 1
    fleet = world.fleet
    for r, pool in enumerate(world.region_fleet):
        rows = np.flatnonzero(is_fleet & (v.region[idx] == r))
        if len(rows) and len(pool):
            pick = pool[weighted_choice(rng, len(rows), fleet["weight"][pool])]
            customer[rows] = fleet["customer_id"][pick]
            np.minimum.at(fleet["first_purchase"], pick, day[rows])
        elif len(rows):
            is_fleet[rows] = False  # no fleet customer in this region: treat as retail
    individual = np.flatnonzero(~is_fleet)
    new_ids = world.preset.fleet_customers + counters.customer + np.arange(1, len(individual) + 1)
    counters.customer += len(individual)
    customer[individual] = new_ids
    stype = np.where(is_fleet | (stype != 1), stype, 0)

    cust = world.cfg.customers
    k = len(individual)
    bands = np.array(list(cust.age_bands))[
        weighted_choice(rng, k, np.array(list(cust.age_bands.values())))
    ]
    band_null = rng.random(k) < cust.age_band_null_rate
    dd = d[individual]
    same_city = rng.random(k) < cust.dealer_city_share
    other_city = _random_city_in_country(world, rng, world.dealers["country"][dd])
    customers = pa.table(
        {
            "customer_id": new_ids,
            "customer_type": np.full(k, "Individual"),
            "age_band": pa.array(bands, mask=band_null),
            "country": world.dealers["country"][dd],
            "city": np.where(same_city, world.dealers["city"][dd], other_city),
            "region": np.array(world.regions)[world.dealers["region"][dd]],
            "first_purchase_date": date_array(day[individual]),
            "_available": date_array(day[individual]),
        }
    )

    sale_id = counters.sale + np.arange(1, m + 1)
    counters.sale += m
    sales = pa.table(
        {
            "sale_id": sale_id,
            "vin": v.vin[idx],
            "dealer_id": world.dealers["dealer_id"][d],
            "customer_id": customer,
            "sale_date": date_array(day),
            "dealer_arrival_date": date_array(arrival[idx]),
            "sale_type": SALE_TYPES[stype],
            "list_price": list_price,
            "discount_amount": discount,
            "net_price": net,
            "is_cancelled": cancelled[idx],
            "_arrival": date_array(day),
        }
    )
    return {"sales": sales, "customers": customers}


def _random_city_in_country(
    world: World, rng: np.random.Generator, countries: np.ndarray
) -> np.ndarray:
    cities = {c.name: c.cities for r in world.ref.regions for c in r.countries}
    result = np.empty(len(countries), dtype=object)
    for country in np.unique(countries):
        rows = np.flatnonzero(countries == country)
        options = np.array(cities[country])
        result[rows] = options[rng.integers(len(options), size=len(rows))]
    return result.astype(str)


def fleet_customer_table(world: World) -> pa.Table:
    f = world.fleet
    bought = f["first_purchase"] < np.iinfo(np.int32).max
    return pa.table(
        {
            "customer_id": f["customer_id"][bought],
            "customer_type": np.full(bought.sum(), "Fleet"),
            "age_band": pa.nulls(bought.sum(), pa.string()),
            "country": f["country"][bought],
            "city": f["city"][bought],
            "region": np.array(world.regions)[f["region"][bought]],
            "first_purchase_date": date_array(f["first_purchase"][bought]),
            "_available": date_array(f["first_purchase"][bought]),
        }
    )
