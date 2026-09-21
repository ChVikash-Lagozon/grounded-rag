"""carquery: natural-language questions over parquet data, answered with DuckDB SQL."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("carquery")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0+unknown"
