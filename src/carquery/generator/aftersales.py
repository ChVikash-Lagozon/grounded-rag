"""After-sales for one chunk of vehicles: recalls, service visits and warranty claims."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from carquery.generator.calendar import date_array
from carquery.generator.vehicles import SALE_TYPES, Counters, Vehicles
from carquery.generator.world import NEVER, World, rng_for, weighted_choice

DAYS_PER_MONTH = 30.44
_BRAKES_SLOT, _DRIVE_SLOT = 0, 1


@dataclass
class RecallCampaign:
    recall_id: int
    campaign_name: str
    part: int
    announced: int
    remedy_description: str
    model: int | None = None  # model_name index, for build-window recalls
    build_window: tuple[int, int] | None = None


def recall_campaigns(world: World) -> list[RecallCampaign]:
    p1 = world.cfg.patterns.p1_brake_batch
    campaigns = [
        RecallCampaign(
            recall_id=1,
            campaign_name=p1.campaign_name,
            part=world.part_index(p1.part),
            announced=world.p1_window[1] + p1.recall_after_days,
            remedy_description=p1.remedy_description,
        )
    ]
    tl = world.timeline
    for i, other in enumerate(world.cfg.recalls.other, start=2):
        lo, hi = other.build_window_frac
        campaigns.append(
            RecallCampaign(
                recall_id=i,
                campaign_name=other.campaign_name,
                part=world.part_index(other.part),
                announced=tl.frac_to_day(other.announce_frac),
                remedy_description=other.remedy_description,
                model=world.model_name_index(other.model),
                build_window=(tl.frac_to_day(lo), tl.frac_to_day(hi)),
            )
        )
    return campaigns


def recall_campaign_table(world: World, campaigns: list[RecallCampaign]) -> pa.Table:
    return pa.table(
        {
            "recall_id": [c.recall_id for c in campaigns],
            "campaign_name": [c.campaign_name for c in campaigns],
            "component_category": [str(world.parts["category"][c.part]) for c in campaigns],
            "part_id": [int(world.parts["part_id"][c.part]) for c in campaigns],
            "announced_date": date_array(np.array([c.announced for c in campaigns])),
            "remedy_description": [c.remedy_description for c in campaigns],
            "_available": date_array(np.array([c.announced for c in campaigns])),
        }
    )


class _Visits:
    """Accumulates service visit rows before they get ids."""

    def __init__(self) -> None:
        self.cols: dict[str, list[np.ndarray]] = {
            k: []
            for k in ("vehicle", "day", "type", "category", "dealer", "labour", "cost", "warranty")
        }

    def add(self, **columns: np.ndarray) -> None:
        n = len(columns["vehicle"])
        for key in self.cols:
            value = columns[key]
            self.cols[key].append(np.broadcast_to(value, n) if np.ndim(value) == 0 else value)

    def concat(self) -> dict[str, np.ndarray]:
        return {k: np.concatenate(v) if v else np.array([]) for k, v in self.cols.items()}


def simulate_aftersales(
    world: World,
    chunk: int,
    v: Vehicles,
    campaigns: list[RecallCampaign],
    counters: Counters,
) -> dict[str, pa.Table]:
    cfg, tl = world.cfg, world.timeline
    rng = rng_for(cfg.seed, "aftersales", chunk)
    horizon = tl.horizon_end
    svc, war = cfg.service, cfg.warranty
    n = len(v.vin)

    # Annual mileage by sale type (P6: fleet cars drive much more, so visit more often).
    mean_km = np.array([svc.annual_km[s] for s in SALE_TYPES])[v.sale_type]
    sigma = svc.annual_km_sigma
    km_per_day = mean_km * np.exp(sigma * rng.standard_normal(n) - sigma**2 / 2) / 365
    s = v.sale_day
    visits = _Visits()

    # ---------------------------------------------------------------- recalls
    recall_rows: list[pa.Table] = []
    p1_remedy = np.full(n, NEVER, dtype=np.int64)
    for campaign in campaigns:
        affected = _recall_affected(world, v, campaign)
        idx = np.flatnonzero(affected)
        remedy = (
            np.maximum(campaign.announced, s[idx])
            + 1
            + np.round(rng.gamma(2.0, cfg.recalls.remedy_days_mean / 2, size=len(idx))).astype(
                np.int64
            )
        )
        done = (
            v.sold[idx]
            & (rng.random(len(idx)) >= cfg.recalls.non_response_rate)
            & (remedy <= horizon)
        )
        if campaign.recall_id == 1:
            p1_remedy[idx[done]] = remedy[done]
        recall_rows.append(
            pa.table(
                {
                    "recall_id": np.full(len(idx), campaign.recall_id),
                    "vin": v.vin[idx],
                    "remedy_completed_date": date_array(np.where(done, remedy, 0), ~done),
                    "_arrival": date_array(np.full(len(idx), campaign.announced)),
                }
            )
        )
        r_idx = idx[done]
        labour = np.round(1.0 + rng.gamma(2.0, 0.25, size=len(r_idx)), 1)
        visits.add(
            vehicle=r_idx,
            day=remedy[done],
            type=np.array("Recall"),
            category=np.array(str(world.parts["category"][campaign.part])),
            dealer=_service_dealer(world, rng, v, r_idx, remedy[done]),
            labour=labour,
            cost=np.round(
                labour * svc.labour_rate_eur + world.parts["unit_cost"][campaign.part], 2
            ),
            warranty=np.array(True),
        )

    sold = np.flatnonzero(v.sold)

    # ---------------------------------------------------------------- scheduled maintenance
    interval = 365 * np.minimum(
        svc.scheduled_interval_months / 12, svc.scheduled_interval_km / (km_per_day[sold] * 365)
    )
    count = np.maximum(0, np.floor((horizon - s[sold]) / interval)).astype(np.int64)
    veh = np.repeat(sold, count)
    k = np.arange(count.sum()) - np.repeat(np.cumsum(count) - count, count) + 1
    day = s[veh] + np.round(k * np.repeat(interval, count) + rng.normal(0, 7, size=len(veh)))
    day = np.clip(day.astype(np.int64), s[veh] + 1, horizon)
    labour = np.round(1.0 + rng.gamma(2.0, 0.4, size=len(veh)), 1)
    visits.add(
        vehicle=veh,
        day=day,
        type=np.array("Scheduled"),
        category=np.array(""),
        dealer=_service_dealer(world, rng, v, veh, day),
        labour=labour,
        cost=np.round(labour * svc.labour_rate_eur + rng.uniform(60, 260, size=len(veh)), 2),
        warranty=np.array(False),
    )

    # ---------------------------------------------------------------- non-warranty repairs
    p8 = cfg.patterns.p8_regional_repairs
    p8_vehicle = (world.trims["model_name_idx"][v.trim] == world.model_name_index(p8.model)) & (
        v.region == world.region_index(p8.region)
    )
    years_owned = np.maximum(0, horizon - s[sold]) / 365
    for category, rate in svc.repair_rates_per_year.items():
        mult = np.where(p8_vehicle[sold] & (category == p8.category), p8.multiplier, 1.0)
        count = rng.poisson(rate * mult * years_owned)
        veh = np.repeat(sold, count)
        day = s[veh] + np.floor(rng.random(len(veh)) * (horizon - s[veh] + 1)).astype(np.int64)
        labour = np.round(0.8 + rng.gamma(2.0, 0.6, size=len(veh)), 1)
        visits.add(
            vehicle=veh,
            day=day,
            type=np.array("Repair"),
            category=np.array(category),
            dealer=_service_dealer(world, rng, v, veh, day),
            labour=labour,
            cost=np.round(labour * svc.labour_rate_eur + rng.uniform(80, 600, size=len(veh)), 2),
            warranty=np.array(False),
        )

    # ---------------------------------------------------------------- warranty failures
    claims = _failures(world, rng, v, sold, km_per_day, p1_remedy)
    c_veh, c_slot, c_day, c_mode = claims
    c_part = v.parts[c_veh, c_slot]
    c_category = world.parts["category"][c_part]
    m = len(c_veh)
    c_dealer = _service_dealer(world, rng, v, c_veh, c_day)
    hours = np.array([war.labour_hours[c] for c in c_category]) * rng.gamma(4.0, 0.25, size=m)
    hours = np.round(np.maximum(0.3, hours), 1)
    labour_cost = np.round(hours * svc.labour_rate_eur, 2)
    parts_cost = np.round(world.parts["unit_cost"][c_part] * rng.uniform(0.9, 1.15, size=m), 2)
    total = np.round(labour_cost + parts_cost, 2)
    visits.add(
        vehicle=c_veh,
        day=c_day,
        type=np.array("Repair"),
        category=c_category,
        dealer=c_dealer,
        labour=hours,
        cost=total,
        warranty=np.array(True),
    )

    shares = war.reporting_lag_shares
    lag_kind = weighted_choice(
        rng, m, np.array([shares["prompt"], shares["late"], shares["very_late"]])
    )
    lag = np.choose(
        lag_kind,
        [rng.integers(0, 4, size=m), rng.integers(4, 31, size=m), rng.integers(31, 121, size=m)],
    )
    received = c_day + lag
    decision = (
        received + 1 + np.round(rng.gamma(2.0, war.decision_days_mean / 2, size=m)).astype(np.int64)
    )
    status = np.where(rng.random(m) < war.rejection_rate, "Rejected", "Approved")
    mode_null = rng.random(m) < war.failure_mode_null_rate
    claim_id = counters.claim + np.arange(1, m + 1)
    counters.claim += m
    claim_table = pa.table(
        {
            "claim_id": claim_id,
            "vin": v.vin[c_veh],
            "dealer_id": world.dealers["dealer_id"][c_dealer],
            "part_id": world.parts["part_id"][c_part],
            "claim_date": date_array(c_day),
            "received_date": date_array(received),
            "failure_mode": pa.array(c_mode, mask=mode_null),
            "odometer_km": np.round(km_per_day[c_veh] * (c_day - s[c_veh])).astype(np.int64),
            "labour_cost": labour_cost,
            "parts_cost": parts_cost,
            "total_cost": total,
            "claim_status": status,
            "_arrival": date_array(received),
            "_decision_date": date_array(decision),
        }
    )

    # ---------------------------------------------------------------- visit table
    vis = visits.concat()
    nv = len(vis["vehicle"])
    veh = vis["vehicle"].astype(np.int64)
    vday = vis["day"].astype(np.int64)
    odo = np.round(km_per_day[veh] * np.maximum(0, vday - s[veh])).astype(np.int64)
    odo_null = rng.random(nv) < svc.odometer_null_rate
    category = vis["category"].astype(str)
    visit_id = counters.visit + np.arange(1, nv + 1)
    counters.visit += nv
    visit_table = pa.table(
        {
            "visit_id": visit_id,
            "vin": v.vin[veh],
            "dealer_id": world.dealers["dealer_id"][vis["dealer"].astype(np.int64)],
            "visit_date": date_array(vday),
            "visit_type": vis["type"].astype(str),
            "component_category": pa.array(category, mask=category == ""),
            "odometer_km": pa.array(odo, mask=odo_null),
            "labour_hours": vis["labour"].astype(float),
            "total_cost": vis["cost"].astype(float),
            "is_warranty": vis["warranty"].astype(bool),
            "_arrival": date_array(vday),
        }
    )
    return {
        "fact_service_visit": visit_table,
        "fact_warranty_claim": claim_table,
        "fact_recall_vehicle": pa.concat_tables(recall_rows),
    }


def _recall_affected(world: World, v: Vehicles, campaign: RecallCampaign) -> np.ndarray:
    if campaign.recall_id == 1:
        return world.supply["defective"][v.batches[:, _BRAKES_SLOT]]
    assert campaign.build_window is not None
    lo, hi = campaign.build_window
    return (
        (world.trims["model_name_idx"][v.trim] == campaign.model)
        & (v.build >= lo)
        & (v.build < hi)
        & (v.parts == campaign.part).any(axis=1)
    )


def _service_dealer(
    world: World, rng: np.random.Generator, v: Vehicles, veh: np.ndarray, day: np.ndarray
) -> np.ndarray:
    """The selling dealer if it is still open (most of the time), else another in the region."""
    loyal = v.dealer[veh]
    keep = (rng.random(len(veh)) < world.cfg.service.loyal_dealer_share) & world.dealer_open(
        loyal, day
    )
    other = world.pick_dealers(rng, v.region[veh], day)
    return np.where(keep, loyal, other)


def _failures(
    world: World,
    rng: np.random.Generator,
    v: Vehicles,
    sold: np.ndarray,
    km_per_day: np.ndarray,
    p1_remedy: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Warranty failures: (vehicle, slot, day, failure_mode) arrays."""
    cfg, war = world.cfg, world.cfg.warranty
    horizon = world.timeline.horizon_end
    s = v.sale_day[sold]
    n_slots = v.parts.shape[1]
    out_v, out_slot, out_day, out_mode = [], [], [], []

    # Baseline: Weibull-shaped hazard per tracked part, within warranty time and mileage.
    km_limit = s + war.km / km_per_day[sold]
    for slot in range(n_slots):
        category = world.parts["category"][v.parts[sold, slot]]
        years = np.where(category == "Battery", war.battery_years, war.years)
        cover_end = np.minimum(np.minimum(s + years * 365, km_limit), horizon)
        exposure = np.maximum(0, cover_end - s) / 365
        rate = np.array([war.failure_rates_per_year[c] for c in category])
        count = np.minimum(rng.poisson(rate * exposure**war.weibull_shape), 2)
        rows = np.repeat(np.arange(len(sold)), count)
        u = rng.random(len(rows)) ** (1 / war.weibull_shape)
        day = s[rows] + np.floor(u * exposure[rows] * 365).astype(np.int64)
        modes = _modes(world, rng, category[rows])
        out_v.append(sold[rows])
        out_slot.append(np.full(len(rows), slot))
        out_day.append(day)
        out_mode.append(modes)

    # P1: vehicles fitted with a defective brake batch fail far more often until remedied.
    p1 = cfg.patterns.p1_brake_batch
    affected = world.supply["defective"][v.batches[sold, _BRAKES_SLOT]]
    end = np.minimum.reduce(
        [s + p1.affected_months * DAYS_PER_MONTH, p1_remedy[sold], np.full(len(s), horizon)]
    )
    exposure = np.where(affected, np.maximum(0, end - s) / 365, 0)
    out = _extra_events(rng, sold, s, s + exposure * 365, p1.extra_claims_per_year * exposure)
    out_v.append(out[0])
    out_slot.append(np.full(len(out[0]), _BRAKES_SLOT))
    out_day.append(out[1])
    out_mode.append(np.full(len(out[0]), p1.failure_mode, dtype=object))

    # P7: one supplier's BEV battery packs wear out after a mileage / age threshold.
    p7 = cfg.patterns.p7_battery_wear
    batch = v.batches[sold, _DRIVE_SLOT]
    is_p7 = (world.trims["powertrain"][v.trim[sold]] == "BEV") & (
        world.supply["supplier"][batch] == world.supplier_index(p7.supplier)
    )
    threshold = s + np.minimum(
        p7.months_threshold * DAYS_PER_MONTH, p7.km_threshold / km_per_day[sold]
    )
    end = np.minimum(s + war.battery_years * 365, horizon)
    exposure = np.where(is_p7, np.maximum(0, end - threshold) / 365, 0)
    out = _extra_events(
        rng, sold, threshold, threshold + exposure * 365, p7.extra_claims_per_year * exposure
    )
    out_v.append(out[0])
    out_slot.append(np.full(len(out[0]), _DRIVE_SLOT))
    out_day.append(out[1])
    out_mode.append(np.full(len(out[0]), p7.failure_mode, dtype=object))

    return (
        np.concatenate(out_v).astype(np.int64),
        np.concatenate(out_slot).astype(np.int64),
        np.concatenate(out_day).astype(np.int64),
        np.concatenate(out_mode).astype(str),
    )


def _extra_events(
    rng: np.random.Generator,
    vehicles: np.ndarray,
    start: np.ndarray,
    end: np.ndarray,
    expected: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Poisson events spread uniformly over [start, end] per vehicle."""
    count = np.minimum(rng.poisson(expected), 2)
    rows = np.repeat(np.arange(len(vehicles)), count)
    day = start[rows] + rng.random(len(rows)) * (end[rows] - start[rows])
    return vehicles[rows], np.floor(day).astype(np.int64)


def _modes(world: World, rng: np.random.Generator, categories: np.ndarray) -> np.ndarray:
    """A random failure mode of each row's component category."""
    result = np.empty(len(categories), dtype=object)
    for category in np.unique(categories):
        rows = np.flatnonzero(categories == category)
        options = np.array(world.ref.failure_modes[str(category)], dtype=object)
        result[rows] = options[rng.integers(len(options), size=len(rows))]
    return result
