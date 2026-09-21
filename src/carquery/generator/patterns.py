"""Planted patterns: resolved parameters, detection SQL and ``ground_truth.yaml``.

Each pattern has a detection query that runs against the table views (see
``carquery.datafiles.connect_views``) on the *history* data. It returns one row of metrics
plus a boolean ``detected`` column. The pattern tests run these queries, and they are written
to ``ground_truth.yaml`` so a person (or the Phase 6 golden set) can see how each pattern
shows up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

import duckdb

from carquery.generator.aftersales import DAYS_PER_MONTH, RecallCampaign
from carquery.generator.calendar import to_date
from carquery.generator.world import World


@dataclass
class Pattern:
    key: str
    title: str
    description: str
    parameters: dict[str, Any]
    detection_sql: str
    expectation: str
    measured: dict[str, Any] = field(default_factory=dict)


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_patterns(world: World, campaigns: list[RecallCampaign]) -> list[Pattern]:
    cfg, tl = world.cfg, world.timeline
    pat = cfg.patterns
    start, end = to_date(tl.history_start), to_date(tl.end)
    d_start, d_end = f"DATE '{start}'", f"DATE '{end}'"

    # P1 -------------------------------------------------------------------------------
    p1 = pat.p1_brake_batch
    w_start, w_end = (to_date(d) for d in world.p1_window)
    supplier = str(
        world.suppliers["supplier_name"][world.parts["primary"][world.part_index(p1.part)]]
    )
    defective = world.supply["supply_batch_id"][world.supply["defective"]]
    recall = campaigns[0]
    p1_sql = f"""
WITH fitted AS (
    SELECT p.vin, pl.plant_name, p.build_date, sp.supplier_name
    FROM fact_production p
    JOIN dim_plant pl USING (plant_id)
    JOIN fact_vehicle_component vc USING (vin)
    JOIN dim_part pt ON pt.part_id = vc.part_id
    JOIN fact_part_supply ps ON ps.supply_batch_id = vc.supply_batch_id
    JOIN dim_supplier sp ON sp.supplier_id = ps.supplier_id
    WHERE pt.part_name = {_q(p1.part)}
), claims AS (
    SELECT w.vin, count(*) AS n
    FROM fact_warranty_claim w JOIN dim_part pt USING (part_id)
    WHERE pt.part_name = {_q(p1.part)}
    GROUP BY w.vin
), flagged AS (
    SELECT coalesce(c.n, 0) AS n,
           plant_name = {_q(p1.plant)} AND supplier_name = {_q(supplier)}
             AND build_date >= DATE '{w_start}' AND build_date < DATE '{w_end}' AS in_window
    FROM fitted f LEFT JOIN claims c USING (vin)
), m AS (
    SELECT sum(n) FILTER (in_window) * 1000.0 / count(*) FILTER (in_window) AS affected_claims_per_1000,
           sum(n) FILTER (NOT in_window) * 1000.0 / count(*) FILTER (NOT in_window) AS other_claims_per_1000,
           (SELECT count(*) FROM fact_recall_vehicle JOIN dim_recall_campaign USING (recall_id)
             WHERE campaign_name = {_q(recall.campaign_name)}) AS recalled_vehicles
    FROM flagged
)
SELECT *, affected_claims_per_1000 >= 3 * other_claims_per_1000 AND recalled_vehicles > 0 AS detected
FROM m"""

    # P2 -------------------------------------------------------------------------------
    shares = pat.p2_bev_share
    fastest = max(shares, key=lambda r: shares[r][1] - shares[r][0])
    slowest = min(shares, key=lambda r: shares[r][1] - shares[r][0])
    p2_sql = f"""
WITH s AS (
    SELECT d.region, m.powertrain = 'BEV' AS bev,
           s.sale_date < {d_start} + INTERVAL 365 DAY AS first_year,
           s.sale_date > {d_end} - INTERVAL 365 DAY AS last_year
    FROM fact_sales s
    JOIN dim_dealer d USING (dealer_id)
    JOIN fact_production p USING (vin)
    JOIN dim_vehicle_model m USING (model_id)
    WHERE NOT s.is_cancelled AND s.sale_date <= {d_end}
), r AS (
    SELECT region, avg(bev::INT) FILTER (first_year) AS share_first_year,
           avg(bev::INT) FILTER (last_year) AS share_last_year
    FROM s GROUP BY region
), m AS (
    SELECT max(share_last_year - share_first_year) FILTER (region = {_q(fastest)}) AS fastest_region_growth,
           max(share_last_year - share_first_year) FILTER (region = {_q(slowest)}) AS slowest_region_growth,
           bool_and(share_last_year > share_first_year) AS all_regions_grow
    FROM r
)
SELECT *, all_regions_grow AND fastest_region_growth > 2 * slowest_region_growth AS detected FROM m"""

    # P3 -------------------------------------------------------------------------------
    p3 = pat.p3_seasonality
    p3_sql = f"""
WITH daily AS (
    SELECT sale_date, count(*) AS n FROM fact_sales WHERE NOT is_cancelled GROUP BY sale_date
), d AS (
    SELECT dd.month, coalesce(daily.n, 0) AS n,
           dd.calendar_date > date_trunc('quarter', dd.calendar_date) + INTERVAL 3 MONTH
             - INTERVAL {p3.quarter_end_window_days + 1} DAY AS quarter_end_window
    FROM dim_date dd LEFT JOIN daily ON daily.sale_date = dd.calendar_date
    WHERE dd.calendar_date BETWEEN {d_start} + INTERVAL 180 DAY AND {d_end}
), m AS (
    SELECT avg(n) FILTER (quarter_end_window) / avg(n) FILTER (NOT quarter_end_window) AS quarter_end_uplift,
           avg(n) FILTER (month = 3) / avg(n) FILTER (month = 8) AS march_vs_august
    FROM d
)
SELECT *, quarter_end_uplift >= 1.2 AND march_vs_august >= 1.2 AS detected FROM m"""

    # P4 -------------------------------------------------------------------------------
    slow_ids = [int(i) for i in world.dealers["dealer_id"][world.dealers["slow"]]]
    slow_names = [str(n) for n in world.dealers["dealer_name"][world.dealers["slow"]]]
    ids = ", ".join(str(i) for i in slow_ids)
    p4_sql = f"""
WITH d AS (
    SELECT dealer_id, avg(sale_date - dealer_arrival_date) AS days_to_sell
    FROM fact_sales WHERE NOT is_cancelled AND sale_date <= {d_end}
    GROUP BY dealer_id HAVING count(*) >= 10
), m AS (
    SELECT avg(days_to_sell) FILTER (dealer_id IN ({ids})) AS slow_dealers_avg_days,
           median(days_to_sell) FILTER (dealer_id NOT IN ({ids})) AS other_dealers_median_days,
           count(*) FILTER (dealer_id IN ({ids})
             AND days_to_sell > (SELECT quantile_cont(days_to_sell, 0.95) FROM d
                                 WHERE dealer_id NOT IN ({ids}))) AS slow_dealers_above_p95
    FROM d
)
SELECT *, slow_dealers_avg_days >= 1.8 * other_dealers_median_days AS detected FROM m"""

    # P5 -------------------------------------------------------------------------------
    p5 = pat.p5_line_change
    change = to_date(tl.frac_to_day(p5.change_frac))
    p5_sql = f"""
WITH v AS (
    SELECT pl.plant_name, p.build_date >= DATE '{change}' AS after_change, p.passed_first_inspection
    FROM fact_production p
    JOIN dim_plant pl USING (plant_id)
    JOIN dim_vehicle_model m USING (model_id)
    WHERE m.model_name = {_q(p5.model)} AND p.build_date <= {d_end}
), m AS (
    SELECT avg(passed_first_inspection::INT) FILTER (plant_name = {_q(p5.plant)} AND NOT after_change) AS pass_rate_before,
           avg(passed_first_inspection::INT) FILTER (plant_name = {_q(p5.plant)} AND after_change) AS pass_rate_after,
           avg(passed_first_inspection::INT) FILTER (plant_name <> {_q(p5.plant)} AND after_change) AS other_plants_after
    FROM v
)
SELECT *, pass_rate_after <= pass_rate_before - 0.08 AND other_plants_after >= pass_rate_before - 0.03 AS detected
FROM m"""

    # P6 -------------------------------------------------------------------------------
    p6_sql = f"""
WITH sold AS (
    SELECT s.vin, s.sale_type, c.customer_type, s.sale_date,
           s.discount_amount / s.list_price AS discount_rate
    FROM fact_sales s JOIN dim_customer c USING (customer_id)
    WHERE NOT s.is_cancelled AND s.sale_date < {d_end} - INTERVAL 90 DAY
), visits AS (
    SELECT vin, count(*) AS n FROM fact_service_visit WHERE visit_date <= {d_end} GROUP BY vin
), m AS (
    SELECT avg(discount_rate) FILTER (sale_type = 'Fleet') AS fleet_discount_rate,
           avg(discount_rate) FILTER (sale_type = 'Retail') AS retail_discount_rate,
           sum(coalesce(n, 0)) FILTER (customer_type = 'Fleet')
             / sum(date_diff('day', sale_date, {d_end}) / 365.0) FILTER (customer_type = 'Fleet') AS fleet_visits_per_vehicle_year,
           sum(coalesce(n, 0)) FILTER (customer_type = 'Individual')
             / sum(date_diff('day', sale_date, {d_end}) / 365.0) FILTER (customer_type = 'Individual') AS individual_visits_per_vehicle_year
    FROM sold LEFT JOIN visits USING (vin)
)
SELECT *, fleet_discount_rate >= 2 * retail_discount_rate
          AND fleet_visits_per_vehicle_year >= 1.3 * individual_visits_per_vehicle_year AS detected
FROM m"""

    # P7 -------------------------------------------------------------------------------
    p7 = pat.p7_battery_wear
    threshold_days = round(p7.months_threshold * DAYS_PER_MONTH)
    p7_sql = f"""
WITH bev AS (
    SELECT p.vin, sp.supplier_name, s.sale_date
    FROM fact_production p
    JOIN dim_vehicle_model m USING (model_id)
    JOIN fact_vehicle_component vc USING (vin)
    JOIN dim_part pt ON pt.part_id = vc.part_id
    JOIN fact_part_supply ps ON ps.supply_batch_id = vc.supply_batch_id
    JOIN dim_supplier sp ON sp.supplier_id = ps.supplier_id
    JOIN fact_sales s ON s.vin = p.vin AND NOT s.is_cancelled
    WHERE m.powertrain = 'BEV' AND pt.component_category = 'Battery'
), claims AS (
    SELECT w.vin,
           count(*) FILTER (w.odometer_km >= {int(p7.km_threshold)}
                            OR w.claim_date - b.sale_date >= {threshold_days}) AS worn_claims
    FROM fact_warranty_claim w
    JOIN dim_part pt USING (part_id)
    JOIN bev b USING (vin)
    WHERE pt.component_category = 'Battery' AND w.claim_date <= {d_end}
    GROUP BY w.vin
), m AS (
    SELECT sum(coalesce(worn_claims, 0)) FILTER (supplier_name = {_q(p7.supplier)}) * 1000.0
             / count(*) FILTER (supplier_name = {_q(p7.supplier)}) AS supplier_worn_claims_per_1000,
           sum(coalesce(worn_claims, 0)) FILTER (supplier_name <> {_q(p7.supplier)}) * 1000.0
             / count(*) FILTER (supplier_name <> {_q(p7.supplier)}) AS other_worn_claims_per_1000
    FROM bev LEFT JOIN claims USING (vin)
)
SELECT *, supplier_worn_claims_per_1000 >= 3 * greatest(other_worn_claims_per_1000, 1) AS detected
FROM m"""

    # P8 -------------------------------------------------------------------------------
    p8 = pat.p8_regional_repairs
    p8_sql = f"""
WITH sold AS (
    SELECT s.vin, d.region, m.model_name, s.sale_date
    FROM fact_sales s
    JOIN dim_dealer d USING (dealer_id)
    JOIN fact_production p USING (vin)
    JOIN dim_vehicle_model m USING (model_id)
    WHERE NOT s.is_cancelled AND s.sale_date < {d_end}
), r AS (
    SELECT vin, count(*) AS n FROM fact_service_visit
    WHERE visit_type = 'Repair' AND NOT is_warranty AND component_category = {_q(p8.category)}
      AND visit_date <= {d_end}
    GROUP BY vin
), x AS (
    SELECT model_name = {_q(p8.model)} AND region = {_q(p8.region)} AS target,
           model_name = {_q(p8.model)} AS same_model, region = {_q(p8.region)} AS same_region,
           coalesce(n, 0) AS n, date_diff('day', sale_date, {d_end}) / 365.0 AS years
    FROM sold LEFT JOIN r USING (vin)
), m AS (
    SELECT sum(n) FILTER (target) / sum(years) FILTER (target) AS target_rate_per_vehicle_year,
           sum(n) FILTER (same_model AND NOT target) / sum(years) FILTER (same_model AND NOT target) AS same_model_elsewhere_rate,
           sum(n) FILTER (same_region AND NOT target) / sum(years) FILTER (same_region AND NOT target) AS same_region_other_models_rate
    FROM x
)
SELECT *, target_rate_per_vehicle_year >= 1.8 * same_model_elsewhere_rate
          AND target_rate_per_vehicle_year >= 1.8 * same_region_other_models_rate AS detected
FROM m"""

    sc, svc = cfg.sales, cfg.service
    return [
        Pattern(
            "p1_brake_batch",
            "Defective brake caliper batches at one plant, followed by a recall",
            f"Front brake calipers from {supplier} delivered to {p1.plant} during a "
            f"{p1.window_weeks}-week window are defective. Vehicles built there in that window "
            f"get many more brake claims ('{p1.failure_mode}') for up to {p1.affected_months:g} "
            f"months or until remedied. Recall '{recall.campaign_name}' was announced on "
            f"{to_date(recall.announced)}. Incoming inspection of those batches shows a higher "
            "defect rate.",
            {
                "plant": p1.plant,
                "part": p1.part,
                "supplier": supplier,
                "build_window": [str(w_start), str(w_end - timedelta(days=1))],
                "defective_supply_batch_ids": [int(b) for b in defective],
                "extra_claims_per_vehicle_year": p1.extra_claims_per_year,
                "failure_mode": p1.failure_mode,
                "recall_id": recall.recall_id,
                "recall_announced": str(to_date(recall.announced)),
            },
            p1_sql,
            "Claims per 1,000 affected vehicles >= 3x the rate for other vehicles with the same "
            "part, and the recall covers vehicles.",
        ),
        Pattern(
            "p2_bev_share",
            "BEV share of sales grows at different rates per region",
            "BEV share of vehicles sold grows along a logistic curve in every region (by dealer "
            f"region); fastest in {fastest}, slowest in {slowest}.",
            {"bev_share_start_end_by_region": {k: list(v) for k, v in shares.items()}},
            p2_sql,
            "Every region's BEV share rises from the first to the last history year, and the "
            "fastest region grows more than twice as much as the slowest.",
        ),
        Pattern(
            "p3_seasonality",
            "Seasonal sales plus fiscal quarter-end spikes",
            "Monthly demand multipliers (spring peak, August and December dips) and a push to "
            f"close deals in the last {p3.quarter_end_window_days} days of each fiscal quarter "
            "(ends of Jun, Sep, Dec, Mar), with a small extra discount on those deals.",
            {
                "monthly_demand_jan_to_dec": p3.monthly_demand,
                "quarter_end_window_days": p3.quarter_end_window_days,
                "quarter_end_pull_probability": p3.quarter_end_pull_probability,
                "quarter_end_extra_discount": p3.quarter_end_extra_discount,
            },
            p3_sql,
            "Average daily sales in the quarter-end window >= 1.2x other days, and March >= 1.2x "
            "August.",
        ),
        Pattern(
            "p4_slow_dealers",
            "A few dealers take much longer to sell",
            f"{len(slow_ids)} dealers have a mean days-to-sell (sale_date - dealer_arrival_date) "
            f"of about {pat.p4_slow_dealers.days_to_sell_mean:g} days versus about "
            f"{sc.days_to_sell_mean:g} for the rest.",
            {"dealer_ids": slow_ids, "dealer_names": slow_names},
            p4_sql,
            "Average days-to-sell of these dealers >= 1.8x the median of the other dealers.",
        ),
        Pattern(
            "p5_line_change",
            "Line change lowers first-inspection pass rate",
            f"From {change}, {p5.model} vehicles built at {p5.plant} pass first inspection "
            f"{p5.pass_rate_after:.0%} of the time instead of "
            f"{cfg.production.first_inspection_pass_rate:.0%}, with more rework hours. The same "
            "model at other plants is unaffected.",
            {
                "plant": p5.plant,
                "model": p5.model,
                "change_date": str(change),
                "pass_rate_before": cfg.production.first_inspection_pass_rate,
                "pass_rate_after": p5.pass_rate_after,
            },
            p5_sql,
            "Pass rate after the change is at least 8 points lower than before, while other "
            "plants stay within 3 points.",
        ),
        Pattern(
            "p6_fleet",
            "Fleet customers get bigger discounts but visit the workshop more",
            "Discount rate ranges by sale type are "
            + ", ".join(f"{k} {v[0]:.0%}-{v[1]:.0%}" for k, v in sc.discount_ranges.items())
            + ". Fleet vehicles drive about "
            f"{svc.annual_km['Fleet']:,.0f} km/year versus {svc.annual_km['Retail']:,.0f} for "
            "retail, so they need scheduled maintenance more often.",
            {
                "discount_ranges": {k: list(v) for k, v in sc.discount_ranges.items()},
                "annual_km": dict(svc.annual_km),
            },
            p6_sql,
            "Fleet discount rate >= 2x retail, and fleet service visits per vehicle-year >= 1.3x "
            "individual customers.",
        ),
        Pattern(
            "p7_battery_wear",
            "One supplier's BEV battery packs wear out after a mileage/age threshold",
            f"BEV battery packs from {p7.supplier} get extra '{p7.failure_mode}' claims once the "
            f"vehicle passes {p7.km_threshold:,.0f} km or {p7.months_threshold:g} months in "
            "service. Other suppliers' packs do not. Visible only by joining claims to the "
            "fitted battery batch and its supplier.",
            {
                "supplier": p7.supplier,
                "km_threshold": p7.km_threshold,
                "months_threshold": p7.months_threshold,
                "failure_mode": p7.failure_mode,
                "extra_claims_per_vehicle_year": p7.extra_claims_per_year,
            },
            p7_sql,
            "Battery claims past the threshold per 1,000 BEVs are >= 3x higher for this supplier "
            "than for the others.",
        ),
        Pattern(
            "p8_regional_repairs",
            "One model needs more suspension repairs in one region",
            f"{p8.model} vehicles sold in {p8.region} have about {p8.multiplier:g}x the "
            f"non-warranty {p8.category} repair visits of the same model elsewhere, and of other "
            "models in that region.",
            {
                "model": p8.model,
                "region": p8.region,
                "category": p8.category,
                "multiplier": p8.multiplier,
            },
            p8_sql,
            "Target rate per vehicle-year >= 1.8x the same model elsewhere and >= 1.8x other "
            "models in the region.",
        ),
    ]


def measure(con: duckdb.DuckDBPyConnection, pattern: Pattern) -> dict[str, Any]:
    """Run a pattern's detection SQL; returns the metrics row as a dict."""
    cursor = con.execute(pattern.detection_sql)
    names = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    assert row is not None
    return {
        name: round(float(value), 4) if isinstance(value, float | Decimal) else value
        for name, value in zip(names, row, strict=True)
    }
