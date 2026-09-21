"""Date helpers. Inside the generator every date is an int64 day number (days since 1970-01-01),
so date arithmetic stays vectorised."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pyarrow as pa

EPOCH = date(1970, 1, 1)
FISCAL_START_MONTH = 4  # fiscal year starts 1 April, named after the year it ends in
_MONTH_NAMES = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def to_day(value: date) -> int:
    return (value - EPOCH).days


def to_date(day: int) -> date:
    return EPOCH + timedelta(days=int(day))


def _dt(days: np.ndarray) -> np.ndarray:
    return np.asarray(days, dtype="int64").astype("datetime64[D]")


def years(days: np.ndarray) -> np.ndarray:
    return _dt(days).astype("datetime64[Y]").astype(np.int64) + 1970


def months(days: np.ndarray) -> np.ndarray:
    """Calendar month 1..12."""
    return _dt(days).astype("datetime64[M]").astype(np.int64) % 12 + 1


def weekday(days: np.ndarray) -> np.ndarray:
    """ISO day of week, 1 = Monday. 1970-01-01 was a Thursday."""
    return (np.asarray(days, dtype=np.int64) + 3) % 7 + 1


def quarter_end(days: np.ndarray) -> np.ndarray:
    """Last day of the calendar quarter containing each day (= the fiscal quarter end, since
    the fiscal year starts on a quarter boundary)."""
    month_index = _dt(days).astype("datetime64[M]").astype(np.int64)
    next_quarter = (month_index // 3 + 1) * 3
    return next_quarter.astype("datetime64[M]").astype("datetime64[D]").astype(np.int64) - 1


def model_year(days: np.ndarray) -> np.ndarray:
    """Model year: vehicles built from 1 July carry the next year's model year."""
    return years(days) + (months(days) >= 7)


def fiscal_year(days: np.ndarray) -> np.ndarray:
    return years(days) + (months(days) >= FISCAL_START_MONTH)


def fiscal_year_start(value: date) -> date:
    year = value.year if value.month >= FISCAL_START_MONTH else value.year - 1
    return date(year, FISCAL_START_MONTH, 1)


def fiscal_year_end(value: date) -> date:
    return date(fiscal_year_start(value).year + 1, FISCAL_START_MONTH, 1) - timedelta(days=1)


def date_array(days: np.ndarray, null_mask: np.ndarray | None = None) -> pa.Array:
    return pa.array(np.asarray(days, dtype=np.int32), type=pa.date32(), mask=null_mask)


def build_dim_date(first: date, last: date) -> pa.Table:
    """Whole fiscal years covering ``first`` .. ``last``."""
    start, end = to_day(fiscal_year_start(first)), to_day(fiscal_year_end(last))
    days = np.arange(start, end + 1, dtype=np.int64)
    dt = _dt(days)
    month = months(days)
    year = years(days)
    iso = [d.isocalendar() for d in dt.astype(object)]
    dow = weekday(days)
    fy = fiscal_year(days)
    return pa.table(
        {
            "calendar_date": date_array(days),
            "year": year,
            "quarter": (month - 1) // 3 + 1,
            "month": month,
            "month_name": [_MONTH_NAMES[m - 1] for m in month],
            "year_month": np.datetime_as_string(dt, unit="M"),
            "week_of_year": [i.week for i in iso],
            "day_of_week": dow,
            "day_name": [_DAY_NAMES[d - 1] for d in dow],
            "is_weekend": dow >= 6,
            "fiscal_year": fy,
            "fiscal_quarter": ((month - FISCAL_START_MONTH) % 12) // 3 + 1,
            "fiscal_year_label": [f"FY{y}" for y in fy],
        }
    )
