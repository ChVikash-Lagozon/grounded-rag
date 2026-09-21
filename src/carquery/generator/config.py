"""Typed models for ``config/generator.yaml`` and ``config/synthetic_reference.yaml``."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from carquery.config import ConfigError, load_yaml

GENERATOR_FILE = "generator.yaml"
REFERENCE_FILE = "synthetic_reference.yaml"

Range = tuple[float, float]
SaleType = Literal["Retail", "Fleet", "Lease"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------------------
# generator.yaml
# --------------------------------------------------------------------------------------


class Preset(_Strict):
    vehicles: int = Field(gt=0)
    dealers: int = Field(gt=0)
    fleet_customers: int = Field(gt=0)
    chunk_months: int = Field(gt=0)
    row_group_size: int = Field(gt=0)
    partitioned_tables: list[str] = []


class ProductionConfig(_Strict):
    monthly_seasonality: list[float] = Field(min_length=12, max_length=12)
    powertrain_mix_non_bev: dict[str, float]
    first_inspection_pass_rate: float
    rework_hours_mean: float
    production_cost_share: Range
    colour_null_rate: float
    transit_days: tuple[int, int]


class DealersConfig(_Strict):
    tier_shares: dict[str, float]
    tier_sales_weight: dict[str, float]
    new_dealer_share: float
    closure_rate: float


class SalesConfig(_Strict):
    sale_type_shares: dict[SaleType, float]
    days_to_sell_mean: float
    options_uplift: Range
    annual_price_increase: float
    discount_ranges: dict[SaleType, Range]
    cancellation_rate: float
    resale_rate: float
    resale_days: tuple[int, int]


class CustomersConfig(_Strict):
    age_bands: dict[str, float]
    age_band_null_rate: float
    dealer_city_share: float


class ServiceConfig(_Strict):
    annual_km: dict[SaleType, float]
    annual_km_sigma: float
    scheduled_interval_km: float
    scheduled_interval_months: float
    loyal_dealer_share: float
    labour_rate_eur: float
    odometer_null_rate: float
    repair_rates_per_year: dict[str, float]


class WarrantyConfig(_Strict):
    years: float
    km: float
    battery_years: float
    failure_rates_per_year: dict[str, float]
    weibull_shape: float
    labour_hours: dict[str, float]
    rejection_rate: float
    decision_days_mean: float
    failure_mode_null_rate: float
    reporting_lag_shares: dict[Literal["prompt", "late", "very_late"], float]


class SupplyConfig(_Strict):
    secondary_share: float
    lead_days: int
    defect_rate_mean: float
    defect_rate_null_rate: float


class OtherRecall(_Strict):
    campaign_name: str
    part: str
    model: str
    build_window_frac: Range
    announce_frac: float
    remedy_description: str


class RecallsConfig(_Strict):
    remedy_days_mean: float
    non_response_rate: float
    other: list[OtherRecall] = []


class P1BrakeBatch(_Strict):
    plant: str
    part: str
    window_start_frac: float
    window_weeks: int
    batch_defect_rate: float
    extra_claims_per_year: float
    affected_months: float
    failure_mode: str
    recall_after_days: int
    campaign_name: str
    remedy_description: str


class P3Seasonality(_Strict):
    monthly_demand: list[float] = Field(min_length=12, max_length=12)
    quarter_end_window_days: int
    quarter_end_lookahead_days: int
    quarter_end_pull_probability: float
    quarter_end_extra_discount: float


class P4SlowDealers(_Strict):
    count: int
    days_to_sell_mean: float


class P5LineChange(_Strict):
    plant: str
    model: str
    change_frac: float
    pass_rate_after: float
    rework_hours_mean_after: float


class P7BatteryWear(_Strict):
    supplier: str
    km_threshold: float
    months_threshold: float
    extra_claims_per_year: float
    failure_mode: str


class P8RegionalRepairs(_Strict):
    model: str
    region: str
    category: str
    multiplier: float


class PatternsConfig(_Strict):
    p1_brake_batch: P1BrakeBatch
    p2_bev_share: dict[str, Range]
    p3_seasonality: P3Seasonality
    p4_slow_dealers: P4SlowDealers
    p5_line_change: P5LineChange
    p7_battery_wear: P7BatteryWear
    p8_regional_repairs: P8RegionalRepairs


class GeneratorConfig(_Strict):
    seed: int
    preset: str
    end_date: date
    history_years: int = Field(gt=0)
    day_horizon_days: int = Field(ge=1)
    presets: dict[str, Preset]
    production: ProductionConfig
    dealers: DealersConfig
    sales: SalesConfig
    customers: CustomersConfig
    service: ServiceConfig
    warranty: WarrantyConfig
    supply: SupplyConfig
    recalls: RecallsConfig
    patterns: PatternsConfig

    @model_validator(mode="after")
    def _preset_exists(self) -> GeneratorConfig:
        if self.preset not in self.presets:
            raise ValueError(f"preset {self.preset!r} is not one of {sorted(self.presets)}")
        return self

    @property
    def active_preset(self) -> Preset:
        return self.presets[self.preset]


# --------------------------------------------------------------------------------------
# synthetic_reference.yaml
# --------------------------------------------------------------------------------------


class Country(_Strict):
    name: str
    weight: float
    cities: list[str]


class Region(_Strict):
    name: str
    weight: float
    countries: list[Country]


class PlantRef(_Strict):
    name: str
    country: str
    city: str
    region: str
    daily_capacity: int
    opened_year: int
    models: list[str]


class TrimRef(_Strict):
    trim: str
    powertrain: Literal["ICE", "Hybrid", "PHEV", "BEV"]
    engine_displacement_l: float | None = None
    battery_kwh: float | None = None
    range_km: int | None = None
    fuel_consumption_l_per_100km: float | None = None
    base_msrp: float
    first_model_year: int
    last_model_year: int | None = None
    weight: float = 1.0


class ModelRef(_Strict):
    name: str
    body_type: str
    segment: str
    trims: list[TrimRef]


class BrandRef(_Strict):
    name: str
    wmi: str = Field(min_length=3, max_length=3)
    models: list[ModelRef]


class SupplierRef(_Strict):
    name: str
    country: str
    tier: str
    quality_rating: float
    categories: list[str]


class PartRef(_Strict):
    name: str
    category: str
    unit_cost: float
    primary_supplier: str
    secondary_supplier: str
    models: list[str] | None = None
    powertrains: list[str] | None = None
    secondary_share: float | None = None


class ReferenceData(_Strict):
    regions: list[Region]
    plants: list[PlantRef]
    brands: list[BrandRef]
    colours: dict[str, float]
    suppliers: list[SupplierRef]
    parts: list[PartRef]
    failure_modes: dict[str, list[str]]
    dealer_name_words: list[str]
    dealer_name_suffixes: list[str]

    @model_validator(mode="after")
    def _references_resolve(self) -> ReferenceData:
        suppliers = {s.name for s in self.suppliers}
        models = {m.name for b in self.brands for m in b.models}
        regions = {r.name for r in self.regions}
        for part in self.parts:
            for name in (part.primary_supplier, part.secondary_supplier):
                if name not in suppliers:
                    raise ValueError(f"part {part.name!r}: unknown supplier {name!r}")
            unknown = set(part.models or []) - models
            if unknown:
                raise ValueError(f"part {part.name!r}: unknown models {sorted(unknown)}")
        for plant in self.plants:
            if plant.region not in regions:
                raise ValueError(f"plant {plant.name!r}: unknown region {plant.region!r}")
            unknown = set(plant.models) - models
            if unknown:
                raise ValueError(f"plant {plant.name!r}: unknown models {sorted(unknown)}")
        return self


def load_generator_config(
    config_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    preset: str | None = None,
    seed: int | None = None,
) -> GeneratorConfig:
    """Load ``generator.yaml``; ``preset`` / ``seed`` (CLI flags) override the file."""
    config = load_yaml(GENERATOR_FILE, GeneratorConfig, config_dir=config_dir, env=env)
    update: dict[str, object] = {}
    if preset is not None:
        if preset not in config.presets:
            raise ConfigError(f"Unknown preset {preset!r}; choose from {sorted(config.presets)}")
        update["preset"] = preset
    if seed is not None:
        update["seed"] = seed
    return config.model_copy(update=update)


def load_reference(
    config_dir: Path | None = None, env: Mapping[str, str] | None = None
) -> ReferenceData:
    return load_yaml(REFERENCE_FILE, ReferenceData, config_dir=config_dir, env=env)
