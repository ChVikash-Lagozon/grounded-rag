"""Synthetic data generator: a pluggable data source that conforms to the schema contract."""

from carquery.generator.config import load_generator_config, load_reference
from carquery.generator.runner import (
    GROUND_TRUTH_FILE,
    GenerationError,
    GenerationResult,
    generate_day,
    generate_history,
)

__all__ = [
    "GROUND_TRUTH_FILE",
    "GenerationError",
    "GenerationResult",
    "generate_day",
    "generate_history",
    "load_generator_config",
    "load_reference",
]
