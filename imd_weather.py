#!/usr/bin/env python3
"""
===============================================================================
  IMD Weather Module (Python Port & Extension of imdR)
  India Meteorological Department (IMD) Gridded Weather Data Processing
===============================================================================

Handles downloading, reading, spatial extraction, and climate/hazard analysis
of IMD high-resolution gridded meteorological datasets:
  - Daily Rainfall (0.25° x 0.25°, 1901–present)
  - Daily Max/Min Temperature (1.0° x 1.0°, 1951–present)
  - Real-time provisional daily grids
  - Point and multi-point time series extraction with cell deduplication
  - Bounding box extraction and export
  - 11 ETCCDI/IMD climate indices (dr, d64, d115, rx1day, rx5day, etc.)
  - Antecedent Precipitation Index (API_3, API_N) for landslide & hazard models
  - Synthetic IMD grid generation for offline testing & simulation
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import math
import os
import ssl
import struct
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd


# ═══════════════════════════════════════════════════════════════════════════════
# IMD GRID SPECIFICATIONS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class IMDVariableConfig:
    """Metadata specification for an IMD gridded variable."""
    variable: str
    ncols: int
    nrows: int
    xmin: float        # Longitude min (grid center of column 0)
    ymin: float        # Latitude min (grid center of row 0)
    res: float         # Spatial resolution in decimal degrees
    na_val: float      # Sentinel value for missing / outside-India data
    units: str
    archive_url: str
    archive_referer: str
    archive_field: str
    realtime_url: str
    realtime_field: str
    realtime_prefix: str

    @property
    def xmax(self) -> float:
        """Longitude max (grid center of last column)."""
        return self.xmin + (self.ncols - 1) * self.res

    @property
    def ymax(self) -> float:
        """Latitude max (grid center of last row)."""
        return self.ymin + (self.nrows - 1) * self.res

    @property
    def extent_wgs84(self) -> Tuple[float, float, float, float]:
        """Bounding box (lon_min, lon_max, lat_min, lat_max) including cell bounds."""
        half = self.res / 2.0
        return (self.xmin - half, self.xmax + half, self.ymin - half, self.ymax + half)

    @property
    def vals_per_day(self) -> int:
        return self.ncols * self.nrows

    def lons(self) -> np.ndarray:
        """Array of longitude coordinates for each column."""
        return np.linspace(self.xmin, self.xmax, self.ncols, dtype=np.float64)

    def lats(self) -> np.ndarray:
        """Array of latitude coordinates for each row."""
        return np.linspace(self.ymin, self.ymax, self.nrows, dtype=np.float64)


IMD_CONFIGS: Dict[str, IMDVariableConfig] = {
    "rain": IMDVariableConfig(
        variable="rain",
        ncols=135,
        nrows=129,
        xmin=66.5,
        ymin=6.5,
        res=0.25,
        na_val=-999.0,
        units="mm/day",
        archive_url="https://imdpune.gov.in/cmpg/Griddata/rainfall.php",
        archive_referer="https://imdpune.gov.in/cmpg/Griddata/Rainfall_25_Bin.html",
        archive_field="rain",
        realtime_url="https://imdpune.gov.in/cmpg/Realtimedata/Rainfall/rain.php",
        realtime_field="rain",
        realtime_prefix="rain_ind0.25_",
    ),
    "tmax": IMDVariableConfig(
        variable="tmax",
        ncols=31,
        nrows=31,
        xmin=67.5,
        ymin=7.5,
        res=1.0,
        na_val=99.9,
        units="deg_C",
        archive_url="https://imdpune.gov.in/cmpg/Griddata/maxtemp.php",
        archive_referer="https://imdpune.gov.in/cmpg/Griddata/Max_1_Bin.html",
        archive_field="maxtemp",
        realtime_url="https://imdpune.gov.in/cmpg/Realtimedata/max/max.php",
        realtime_field="max",
        realtime_prefix="max",
    ),
    "tmin": IMDVariableConfig(
        variable="tmin",
        ncols=31,
        nrows=31,
        xmin=67.5,
        ymin=7.5,
        res=1.0,
        na_val=99.9,
        units="deg_C",
        archive_url="https://imdpune.gov.in/cmpg/Griddata/mintemp.php",
        archive_referer="https://imdpune.gov.in/cmpg/Griddata/Min_1_Bin.html",
        archive_field="mintemp",
        realtime_url="https://imdpune.gov.in/cmpg/Realtimedata/min/min.php",
        realtime_field="min",
        realtime_prefix="min",
    ),
}

# Regional Bounding Boxes across India
REGIONAL_BBOXES: Dict[str, Tuple[float, float, float, float]] = {
    # lat_min, lat_max, lon_min, lon_max
    "northeast": (21.5, 29.5, 88.0, 97.5),
    "assam": (24.0, 28.2, 89.6, 96.0),
    "meghalaya": (25.0, 26.2, 89.8, 92.8),
    "sikkim": (27.0, 28.2, 88.0, 88.9),
    "western_ghats": (8.0, 21.0, 73.0, 78.0),
    "himalayas": (28.0, 35.5, 74.0, 88.0),
    "all_india": (6.5, 38.5, 66.5, 100.0),
}


# ═══════════════════════════════════════════════════════════════════════════════
# IMD DATASET CONTAINER
# ═══════════════════════════════════════════════════════════════════════════════

class IMDGridDataset:
    """
    In-memory representation of IMD Gridded Meteorological data.
    Stores values as a NumPy 3D array of shape: (n_days, n_lats, n_lons).
    """

    def __init__(
        self,
        data: np.ndarray,
        dates: List[datetime.date],
        config: IMDVariableConfig,
        year: Optional[int] = None,
    ):
        if data.ndim != 3:
            raise ValueError(f"Expected 3D array (days, lats, lons), got ndim={data.ndim}")
        if data.shape[0] != len(dates):
            raise ValueError(f"Days mismatch: data has {data.shape[0]}, dates has {len(dates)}")
        if data.shape[1] != config.nrows or data.shape[2] != config.ncols:
            raise ValueError(
                f"Grid dimension mismatch: expected ({config.nrows}, {config.ncols}), "
                f"got ({data.shape[1]}, {data.shape[2]})"
            )

        self.data = data.astype(np.float32)
        self.dates = dates
        self.config = config
        self.year = year or dates[0].year
        self.lons = config.lons()
        self.lats = config.lats()
        self._date_to_idx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(dates)}

    @property
    def n_days(self) -> int:
        return len(self.dates)

    def get_day_index(self, date: Union[str, datetime.date]) -> int:
        d_str = date.strftime("%Y-%m-%d") if isinstance(date, (datetime.date, datetime.datetime)) else str(date)
        if d_str not in self._date_to_idx:
            raise KeyError(f"Date {d_str} not in dataset. Available range: {self.dates[0]} to {self.dates[-1]}")
        return self._date_to_idx[d_str]

    def coord_to_indices(self, lat: float, lon: float) -> Tuple[int, int]:
        """Convert latitude and longitude into (row_idx, col_idx)."""
        half = self.config.res / 2.0
        lon_min, lon_max, lat_min, lat_max = self.config.extent_wgs84

        if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
            raise ValueError(
                f"Coordinates ({lat:.4f}, {lon:.4f}) outside IMD extent "
                f"[lat: {lat_min:.2f}..{lat_max:.2f}, lon: {lon_min:.2f}..{lon_max:.2f}]"
            )

        # Compute nearest cell index
        col = int(round((lon - self.config.xmin) / self.config.res))
        row = int(round((lat - self.config.ymin) / self.config.res))

        col = max(0, min(self.config.ncols - 1, col))
        row = max(0, min(self.config.nrows - 1, row))
        return row, col

    def get_point_series(self, lat: float, lon: float) -> pd.DataFrame:
        """Extract full daily time series at a coordinate."""
        row, col = self.coord_to_indices(lat, lon)
        vals = self.data[:, row, col]
        return pd.DataFrame({
            "date": self.dates,
            "lat": lat,
            "lon": lon,
            "grid_lat": self.lats[row],
            "grid_lon": self.lons[col],
            self.config.variable: vals,
        })

    def get_points(
        self,
        coords: Sequence[Tuple[float, float]],
        date: Optional[Union[str, datetime.date]] = None,
    ) -> np.ndarray:
        """
        Fast vectorized multi-point extraction with grid-cell deduplication.
        Ideal for querying weather on thousands of road network segments.
        Returns a 1D array of values corresponding to each coordinate pair.
        """
        if date is not None:
            day_idx = self.get_day_index(date)
            grid_slice = self.data[day_idx]  # shape: (nrows, ncols)
        else:
            # Mean across all days
            grid_slice = np.nanmean(self.data, axis=0)

        results = np.full(len(coords), np.nan, dtype=np.float32)
        cell_cache: Dict[Tuple[int, int], float] = {}

        for i, (lat, lon) in enumerate(coords):
            try:
                row, col = self.coord_to_indices(lat, lon)
            except ValueError:
                continue

            cell_key = (row, col)
            if cell_key not in cell_cache:
                cell_cache[cell_key] = float(grid_slice[row, col])
            results[i] = cell_cache[cell_key]

        return results

    def compute_api(
        self,
        k: float = 0.85,
        window: int = 3,
    ) -> np.ndarray:
        """
        Calculate the Antecedent Precipitation Index (API) for each day and cell.
        Formula: API_t = sum_{i=0}^{window-1} (k^i * R_{t-i})
        Returns 3D array of same shape as self.data.
        """
        if self.config.variable != "rain":
            raise ValueError("API calculation is only valid for rainfall data.")

        # Fill NaNs with 0 for antecedent sum
        clean_rain = np.where(np.isnan(self.data), 0.0, self.data)
        api_arr = np.zeros_like(clean_rain)

        weights = [k ** i for i in range(window)]
        for t in range(self.n_days):
            accum = np.zeros((self.config.nrows, self.config.ncols), dtype=np.float32)
            for w_idx, w in enumerate(weights):
                src_t = t - w_idx
                if src_t >= 0:
                    accum += w * clean_rain[src_t]
            api_arr[t] = accum

        # Restore NaNs where mask is outside India
        all_nan_mask = np.all(np.isnan(self.data), axis=0)
        api_arr[:, all_nan_mask] = np.nan
        return api_arr

    def compute_rainfall_indices(self) -> pd.DataFrame:
        """
        Compute the 11 ETCCDI / IMD climate indices for every grid cell across the dataset:
          1.  dr: wet days (>= 2.5 mm/day)
          2.  d64: heavy rain days (>= 64.5 mm/day)
          3.  d115: very heavy rain days (>= 115.6 mm/day)
          4.  rx1day: maximum 1-day rainfall (mm)
          5.  rx5day: maximum 5-day consecutive rainfall (mm)
          6.  rtwd: total rainfall on wet days (mm)
          7.  sdii: simple daily intensity index (rtwd / dr)
          8.  total: total annual rainfall (mm)
          9.  cwd: maximum consecutive wet days
          10. cdd: maximum consecutive dry days
          11. pci: precipitation concentration index
        """
        if self.config.variable != "rain":
            raise ValueError("Rainfall indices only applicable to 'rain' variable.")

        rows, cols = self.config.nrows, self.config.ncols
        n_cells = rows * cols
        
        # Flatten spatial dimensions: (n_days, n_cells)
        flat_data = self.data.reshape(self.n_days, n_cells).T  # (n_cells, n_days)

        valid = ~np.all(np.isnan(flat_data), axis=1)

        dr = np.full(n_cells, np.nan, dtype=np.float32)
        d64 = np.full(n_cells, np.nan, dtype=np.float32)
        d115 = np.full(n_cells, np.nan, dtype=np.float32)
        rx1day = np.full(n_cells, np.nan, dtype=np.float32)
        rx5day = np.full(n_cells, np.nan, dtype=np.float32)
        rtwd = np.full(n_cells, np.nan, dtype=np.float32)
        sdii = np.full(n_cells, np.nan, dtype=np.float32)
        total = np.full(n_cells, np.nan, dtype=np.float32)
        cwd = np.full(n_cells, np.nan, dtype=np.float32)
        cdd = np.full(n_cells, np.nan, dtype=np.float32)
        pci = np.full(n_cells, np.nan, dtype=np.float32)

        # Compute for valid cells
        for idx in np.where(valid)[0]:
            series = flat_data[idx]
            clean = np.nan_to_num(series, nan=0.0)

            # Wet / Heavy day counts
            wet_mask = clean >= 2.5
            dr[idx] = np.sum(wet_mask)
            d64[idx] = np.sum(clean >= 64.5)
            d115[idx] = np.sum(clean >= 115.6)

            # Maximums
            rx1day[idx] = np.nanmax(series) if np.any(~np.isnan(series)) else np.nan
            
            # 5-day rolling sum max
            if len(clean) >= 5:
                roll5 = np.convolve(clean, np.ones(5), mode="valid")
                rx5day[idx] = np.max(roll5)
            else:
                rx5day[idx] = np.sum(clean)

            # Wet-day total & SDII
            wet_total = np.sum(clean[wet_mask])
            rtwd[idx] = wet_total
            total[idx] = np.sum(clean)
            sdii[idx] = (wet_total / dr[idx]) if dr[idx] > 0 else 0.0

            # Consecutive wet / dry streaks
            max_c_wet, cur_c_wet = 0, 0
            max_c_dry, cur_c_dry = 0, 0
            for v in clean:
                if v >= 2.5:
                    cur_c_wet += 1
                    cur_c_dry = 0
                else:
                    cur_c_dry += 1
                    cur_c_wet = 0
                if cur_c_wet > max_c_wet: max_c_wet = cur_c_wet
                if cur_c_dry > max_c_dry: max_c_dry = cur_c_dry
            cwd[idx] = max_c_wet
            cdd[idx] = max_c_dry

            # Precipitation Concentration Index (PCI)
            months = [d.month for d in self.dates]
            monthly_sums = np.zeros(12)
            for m_i, m in enumerate(months):
                monthly_sums[m - 1] += clean[m_i]
            ann_total = np.sum(monthly_sums)
            if ann_total > 0:
                pci[idx] = 100.0 * np.sum((monthly_sums / ann_total) ** 2)

        # Coordinate grid mapping
        lat_grid, lon_grid = np.meshgrid(self.lats, self.lons, indexing="ij")
        
        df = pd.DataFrame({
            "cell": np.arange(n_cells),
            "lat": lat_grid.flatten(),
            "lon": lon_grid.flatten(),
            "year": self.year,
            "dr": np.round(dr, 1),
            "d64": np.round(d64, 1),
            "d115": np.round(d115, 1),
            "rx1day": np.round(rx1day, 1),
            "rx5day": np.round(rx5day, 1),
            "rtwd": np.round(rtwd, 1),
            "sdii": np.round(sdii, 2),
            "total": np.round(total, 1),
            "cwd": cwd,
            "cdd": cdd,
            "pci": np.round(pci, 2),
        })
        return df[valid].reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════════
# BINARY FILE READERS (.GRD)
# ═══════════════════════════════════════════════════════════════════════════════

def read_imd_binary(
    filepath: Union[str, Path],
    variable: str = "rain",
    year: Optional[int] = None,
) -> IMDGridDataset:
    """
    Read an IMD binary archive .grd file into an IMDGridDataset.

    Archive binary format:
      - 32-bit single precision little-endian floats (<f4).
      - For each day: nrows * ncols floats.
      - Total length = nrows * ncols * n_days * 4 bytes.
    """
    cfg = IMD_CONFIGS.get(variable)
    if not cfg:
        raise ValueError(f"Unknown variable '{variable}'. Must be one of: {list(IMD_CONFIGS.keys())}")

    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    file_size = filepath.stat().st_size

    # Infer year if not supplied
    if year is None:
        name = filepath.stem
        # Extract 4-digit year from filename if present
        import re
        m = re.search(r"(19\d\d|20\d\d)", name)
        year = int(m.group(1)) if m else 2023

    is_leap = calendar.isleap(year)
    n_days = 366 if is_leap else 365
    expected_size = cfg.nrows * cfg.ncols * n_days * 4

    # Validate or adapt to actual file size
    actual_vals = file_size // 4
    vals_per_day = cfg.nrows * cfg.ncols

    if file_size != expected_size:
        # Check if it contains a partial year or non-leap/leap discrepancy
        possible_days = actual_vals // vals_per_day
        if possible_days > 0 and (actual_vals % vals_per_day == 0):
            n_days = possible_days
        else:
            raise ValueError(
                f"File size mismatch for {filepath.name}: expected {expected_size} bytes ({n_days} days), "
                f"got {file_size} bytes."
            )

    # Read binary floats
    raw_vals = np.fromfile(filepath, dtype="<f4")

    # Reshape: (n_days, nrows, ncols)
    # Order in IMD .grd:
    # Outer: Day -> Middle: Latitude row (south to north) -> Inner: Longitude col (west to east)
    data = raw_vals.reshape((n_days, cfg.nrows, cfg.ncols))

    # Mask out missing values
    data = np.where(np.abs(data - cfg.na_val) < 0.01, np.nan, data)

    # Generate dates
    start_date = datetime.date(year, 1, 1)
    dates = [start_date + datetime.timedelta(days=i) for i in range(n_days)]

    return IMDGridDataset(data=data, dates=dates, config=cfg, year=year)


def read_realtime_grd(
    filepath: Union[str, Path],
    variable: str = "rain",
    date: Optional[Union[str, datetime.date]] = None,
) -> IMDGridDataset:
    """
    Read an IMD provisional real-time single-day .grd file.
    """
    cfg = IMD_CONFIGS.get(variable)
    if not cfg:
        raise ValueError(f"Unknown variable '{variable}'")

    filepath = Path(filepath)
    raw_vals = np.fromfile(filepath, dtype="<f4")
    expected_vals = cfg.nrows * cfg.ncols

    # Handle occasional 1-value header in real-time files
    if len(raw_vals) == expected_vals + 1:
        raw_vals = raw_vals[1:]

    if len(raw_vals) != expected_vals:
        raise ValueError(
            f"Size mismatch in realtime grd {filepath.name}: expected {expected_vals} floats, got {len(raw_vals)}"
        )

    grid = raw_vals.reshape((1, cfg.nrows, cfg.ncols))
    grid = np.where(np.abs(grid - cfg.na_val) < 0.01, np.nan, grid)

    if date is None:
        date = datetime.date.today()
    elif isinstance(date, str):
        date = datetime.datetime.strptime(date, "%Y-%m-%d").date()

    return IMDGridDataset(data=grid, dates=[date], config=cfg, year=date.year)


# ═══════════════════════════════════════════════════════════════════════════════
# SYNTHETIC IMD GRID GENERATOR (FOR OFFLINE / TESTING)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_synthetic_imd_grd(
    output_path: Union[str, Path],
    variable: str = "rain",
    year: int = 2023,
    n_days: Optional[int] = None,
    scenario: str = "monsoon",
    random_seed: int = 42,
) -> Path:
    """
    Generate a synthetic binary .grd file matching the exact official IMD format.
    Simulates realistic Indian monsoon patterns:
      - Heavy precipitation along Western Ghats and North-East India (Assam, Meghalaya).
      - Low precipitation in Thar Desert (Rajasthan) and rain-shadow regions.
    """
    cfg = IMD_CONFIGS[variable]
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if n_days is None:
        n_days = 366 if calendar.isleap(year) else 365

    rng = np.random.RandomState(random_seed)
    lats = cfg.lats()
    lons = cfg.lons()
    lat_grid, lon_grid = np.meshgrid(lats, lons, indexing="ij")

    # Geographic mask approximating India's landmass
    inside_india = (
        (lat_grid >= 8.0) & (lat_grid <= 36.0) &
        (lon_grid >= 68.0) & (lon_grid <= 97.0)
    )

    data = np.full((n_days, cfg.nrows, cfg.ncols), cfg.na_val, dtype="<f4")

    # Realistic base meteorological field
    if variable == "rain":
        # Centers of high rainfall (Western Ghats: ~14N, 74E; North-East: ~26N, 92E)
        dist_ne = np.sqrt((lat_grid - 26.0) ** 2 + (lon_grid - 92.0) ** 2)
        dist_wg = np.sqrt((lat_grid - 14.5) ** 2 + (lon_grid - 74.5) ** 2)
        ne_factor = np.exp(-dist_ne / 4.5) * 45.0
        wg_factor = np.exp(-dist_wg / 3.0) * 40.0
        spatial_pattern = ne_factor + wg_factor + 5.0

        for d in range(n_days):
            # Day of year seasonality (Peak monsoon: days 150 to 270 / June-September)
            doy = d + 1
            monsoon_bell = np.exp(-((doy - 205) ** 2) / (2 * (40 ** 2)))
            noise = rng.exponential(scale=1.0, size=(cfg.nrows, cfg.ncols)) * 8.0
            
            day_rain = (spatial_pattern * monsoon_bell) + noise
            if scenario == "heavy":
                day_rain *= 1.8
            elif scenario == "extreme":
                day_rain *= 2.8

            day_rain = np.where(inside_india, np.clip(day_rain, 0.0, 350.0), cfg.na_val)
            data[d] = day_rain.astype("<f4")

    else:
        # Temperature: cooler in north/Himalayas, hotter in central/south
        base_temp = 38.0 - (lat_grid - 8.0) * 0.7
        for d in range(n_days):
            seasonal = 6.0 * np.sin(2 * np.pi * (d - 80) / 365.0)
            noise = rng.normal(0, 1.5, size=(cfg.nrows, cfg.ncols))
            t = base_temp + seasonal + noise
            if variable == "tmin":
                t -= rng.uniform(8.0, 14.0)
            t = np.where(inside_india, np.clip(t, -5.0, 48.0), cfg.na_val)
            data[d] = t.astype("<f4")

    # Write binary file
    with open(output_path, "wb") as f:
        data.tofile(f)

    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
# NETWORK DOWNLOADER (IMD PUNE)
# ═══════════════════════════════════════════════════════════════════════════════

def download_imd_archive(
    variable: str,
    year: int,
    output_dir: Union[str, Path],
    timeout: int = 60,
    retries: int = 2,
) -> Optional[Path]:
    """
    Download a full year archive .grd file from IMD Pune.
    Returns path on success, or None if server is unavailable/timed out.
    """
    cfg = IMD_CONFIGS.get(variable)
    if not cfg:
        raise ValueError(f"Unknown variable '{variable}'")

    output_dir = Path(output_dir) / variable
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / f"{year}.grd"

    if dest.exists() and dest.stat().st_size > 1024:
        return dest

    is_leap = calendar.isleap(year)
    expected_bytes = cfg.nrows * cfg.ncols * (366 if is_leap else 365) * 4

    post_data = urllib.parse.urlencode({cfg.archive_field: str(year)}).encode("utf-8")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": cfg.archive_referer,
        "Origin": "https://imdpune.gov.in",
    }

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(cfg.archive_url, data=post_data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                body = resp.read()

            if len(body) == expected_bytes:
                with open(dest, "wb") as f:
                    f.write(body)
                return dest
        except Exception:
            if attempt < retries:
                time.sleep(2)

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# HIGH-LEVEL CONVENIENCE APIS (PORTED FROM IMDR)
# ═══════════════════════════════════════════════════════════════════════════════

def get_data(
    variable: str,
    start_yr: int,
    end_yr: int,
    file_dir: Union[str, Path],
    allow_synthetic_fallback: bool = True,
) -> List[IMDGridDataset]:
    """
    Load IMD data for a range of years from cached files or download from IMD Pune.
    If offline or server unavailable, gracefully falls back to synthetic dataset.
    """
    file_dir = Path(file_dir)
    datasets = []

    for yr in range(start_yr, end_yr + 1):
        grd_path = file_dir / variable / f"{yr}.grd"
        if not grd_path.exists():
            downloaded = download_imd_archive(variable, yr, file_dir)
            if downloaded:
                grd_path = downloaded
            elif allow_synthetic_fallback:
                # Generate matching realistic synthetic file
                grd_path = generate_synthetic_imd_grd(grd_path, variable=variable, year=yr)
            else:
                raise FileNotFoundError(f"Could not find or download IMD grid for {variable} {yr}")

        ds = read_imd_binary(grd_path, variable=variable, year=yr)
        datasets.append(ds)

    return datasets


def get_point(
    lat: float,
    lon: float,
    variable: str,
    start_yr: int,
    end_yr: int,
    file_dir: Union[str, Path],
) -> pd.DataFrame:
    """Extract daily time series at a point location (lat, lon)."""
    datasets = get_data(variable, start_yr, end_yr, file_dir)
    dfs = [ds.get_point_series(lat, lon) for ds in datasets]
    combined = pd.concat(dfs, ignore_index=True)
    return combined


def get_bbox(
    dataset: IMDGridDataset,
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
) -> Dict[str, Any]:
    """
    Crop IMD dataset to a bounding box (e.g. North East India).
    """
    lat_mask = (dataset.lats >= lat_min) & (dataset.lats <= lat_max)
    lon_mask = (dataset.lons >= lon_min) & (dataset.lons <= lon_max)

    cropped_data = dataset.data[:, lat_mask, :][:, :, lon_mask]
    return {
        "data": cropped_data,
        "lats": dataset.lats[lat_mask],
        "lons": dataset.lons[lon_mask],
        "dates": dataset.dates,
        "variable": dataset.config.variable,
        "units": dataset.config.units,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLI INTERFACE
# ═══════════════════════════════════════════════════════════════════════════════

def build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="IMD Gridded Weather Processor (Python Port of imdR)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--grd", type=str, help="Path to an IMD binary .grd file")
    p.add_argument("--var", choices=["rain", "tmax", "tmin"], default="rain", help="Variable type")
    p.add_argument("--year", type=int, default=2023, help="Year of the dataset")
    p.add_argument("--date", type=str, help="Specific date (YYYY-MM-DD) to query")
    p.add_argument("--point", nargs=2, type=float, metavar=("LAT", "LON"), help="Query point: lat lon")
    p.add_argument("--region", choices=list(REGIONAL_BBOXES.keys()), help="Crop to predefined Indian region")
    p.add_argument("--indices", action="store_true", help="Compute 11 climate indices for the grid")
    p.add_argument("--generate-sample", type=str, help="Generate a sample IMD .grd file at path")
    p.add_argument("--output", type=str, help="Output CSV path for exported series/indices")
    return p


def main():
    parser = build_cli()
    args = parser.parse_args()

    if args.generate_sample:
        out = generate_synthetic_imd_grd(args.generate_sample, variable=args.var, year=args.year)
        print(f"[OK] Generated sample IMD {args.var} grid: {out} ({out.stat().st_size / (1024*1024):.2f} MB)")
        return

    if not args.grd:
        print("[INFO] No --grd file provided. Use --generate-sample to create a test grid, or --help.")
        return

    ds = read_imd_binary(args.grd, variable=args.var, year=args.year)
    print(f"[IMD] Loaded {ds.config.variable.upper()} | Year: {ds.year} | Days: {ds.n_days} | Grid: {ds.config.ncols}x{ds.config.nrows}")

    if args.point:
        lat, lon = args.point
        df = ds.get_point_series(lat, lon)
        print(f"\n[Point Query: {lat}N, {lon}E]")
        print(df.head(10).to_string(index=False))
        if args.output:
            df.to_csv(args.output, index=False)
            print(f"[Saved] {args.output}")

    if args.indices:
        print("\n[Computing 11 Climate Indices...]")
        t0 = time.perf_counter()
        df_idx = ds.compute_rainfall_indices()
        print(f"[Done in {time.perf_counter()-t0:.2f}s] Non-empty cells: {len(df_idx)}")
        print(df_idx.head(5).to_string(index=False))
        if args.output:
            df_idx.to_csv(args.output, index=False)
            print(f"[Saved] {args.output}")


if __name__ == "__main__":
    main()
