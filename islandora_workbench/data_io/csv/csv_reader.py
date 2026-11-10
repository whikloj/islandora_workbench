import csv
from typing import Union, Any

from islandora_workbench.data_io.csv.csv_cache import get_source_file_path, get_csv_cache_path, read_meta, \
    write_meta_atomic, fingerprint
from islandora_workbench.data_io.csv.csv_process import write_preprocessed_csv


class CsvReader:
    """A thin wrapper CSV reader with potential caching to avoid re-reading from disk each time."""

    path: str  # Path to the preprocessed CSV file to read.
    meta: dict  # Metadata about the CSV file, including field names and row count.
    _rows: Union[list[Any], None]  # Cached rows of the CSV file, if they have been read into memory.
    _limit: int  # Maximum number of rows to cache in memory.

    def __init__(self, path, meta, max_cached_rows: int=200000):
        self.path = path
        self.meta = meta
        self._rows = None
        self._limit = max_cached_rows

    @classmethod
    def open(cls, config):
        """Open a CSV file and return a CsvReader instance, loading or creating a sidecar file as needed."""
        source_path = get_source_file_path(config)
        path, meta = load_or_create(config, source_path)
        if "max_cached_rows" in config:
            return cls(path, meta, max_cached_rows=config["max_cached_rows"])
        return cls(path, meta)

    @property
    def field_names(self):
        return list(self.meta["field_names"])

    @property
    def row_count(self):
        return self.meta["row_count"]

    @property
    def has_commented_out_rows(self):
        return self.meta.get("has_commented_out_rows", False)

    def rows(self):
        """Return an iterator over the rows of the CSV file, either from a cached list or by streaming from the file."""
        if self._rows is not None:
            # If we have cached the rows, return an iterator over them.
            return iter(self._rows)
        if self.row_count <= self._limit:
            # If we have a small enough CSV but haven't cached the rows yet, read them into memory and cache them.
            with open(self.path, newline="", encoding="utf-8") as fh:
                self._rows = list(csv.DictReader(fh))
            return iter(self._rows)
        # Otherwise, just return a generator that reads the rows from the CSV file on demand.
        return self._stream()

    def _stream(self):
        """Return a generator that reads the rows from the CSV file on demand."""
        with open(self.path, newline="", encoding="utf-8") as fh:
            yield from csv.DictReader(fh)


def load_or_create(ctx: dict, source_file_path: str) -> tuple[str, dict]:
    """Load or create a preprocessed CSV and its sidecar file for the given source file.

    :param ctx: dict - The context dictionary.
    :param source_file_path: str - The path to the source file.
    :return: tuple[str, dict] - A tuple containing the path to the sidecar file and its contents.
    :raise FileNotFoundError: If the source file does not exist or is unreadable or the file specified in the "csv_rows_to_process" config setting cannot be found.
    """
    cache_path = get_csv_cache_path(ctx['config'])
    sidecar_file_path = cache_path + ".sidecar.json"

    meta = read_meta(sidecar_file_path)
    if meta and meta["fingerprint"] == fingerprint(ctx['config'], source_file_path):
        return cache_path, meta  # cache hit

    meta = write_preprocessed_csv(ctx, source_file_path, cache_path)
    write_meta_atomic(sidecar_file_path, meta)
    return cache_path, meta
