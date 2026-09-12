#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 SIH26002 - REAL DATA INGESTION & FUSION PIPELINE  (North Eastern Region)
================================================================================

 Turns the raw products you can actually download for the NER into the exact
 schema `hazard_prediction_engine.py` trains on:

   GSI Bhukosh / NGDR landslide inventory  ─┐
   OSM roads (fetched live, or a file)     ─┤
   SRTM / NASADEM / CartoDEM elevation     ─┼──►  data/historical_hazards_<year>.csv
   pre-computed slope rasters (opt.)       ─┤     data/ner_roads.geojson
   SoilGrids 2.0 soil properties           ─┤     outputs/ingestion_report.json
   IMD / NESAC / NEDFI district rainfall   ─┤
   ERA5-Land / SMAP soil moisture (opt.)   ─┘

 PIPELINE STAGES
 ---------------
   1. Load and normalise the landslide inventory.  CSV / TSV / GeoJSON Points,
      any of ~40 header spellings across Bhukosh, NGDR and NLSM exports.
   2. Get the road network.  Either read a GeoJSON you already have, or fetch
      one for a --state / --bbox: OSMnx when installed, otherwise the Overpass
      API through urllib.  The result is cached, so only the first run needs
      network access.
   3. Enrich every segment from terrain: elevation, slope, relief, cut faces.
      Slope comes from the DEM (Horn 3x3) or from a --slope-tif product, which
      takes precedence; the unit is detected, since degrees and percent
      gradient are both in circulation and look the same on disk.
   4. Attach soil.  ISRIC SoilGrids 2.0 for texture, bulk density and water
      retention (REST, cached to disk), plus ERA5-Land / SMAP for the moisture
      state those properties are combined with.
   5. Join the inventory onto the network and fuse one training table, with
      weather matched per observation date and history counted only backwards
      in time.
   6. Emit the CSV, the enriched graph and a provenance report that says, per
      column, whether it came from real data or from a documented fallback.

 DESIGN RULES (same as the engine)
 ---------------------------------
   * Standard library only at the core.  `.hgt` and `.asc` DEMs, CSV
     inventories, the SoilGrids REST API and the Overpass road fetch are all
     handled without numpy, rasterio, geopandas, shapely, osmnx or requests.
     Those packages are used when present, never required.
   * Offline-first.  Every network source has an on-disk cache and a CSV
     fallback, so a demo never dies because a portal is down.  `--offline`
     makes that a hard promise: the run either uses caches or stops with an
     explanation, and never dials out.
   * No label leakage.  Labels are case-control - a positive row sits ON its
     event date, so it carries the weather that actually accompanied the
     failure - and `hist_freq_per_km` for an observation on date D is built
     ONLY from events strictly before D (expanding-window history).
   * Provenance is emitted, not assumed: every output column is tagged with the
     source it came from, or marked IMPUTED with the rule used.

 WHAT THIS MODULE DOES *NOT* DO
 ------------------------------
   Roads it can fetch for you; inventories and rainfall it cannot.  Bhukosh,
   NGDR, NLSM and the NEDFI databank sit behind interactive sessions and
   registration, so there is no honest way to script them.  DATASETS.md (next to
   this file) says exactly what to fetch, in what format, and what to do when a
   portal will not give you a clean export.  Once the files are on disk this
   module does everything else.

 QUICK START
 -----------
     # 1. fabricate real-format sample inputs, fuse them AND retrain the
     #    engine on the result - offline, nothing to install
     python data_ingestion.py --demo --train

     # 1b. dry-run any single file before trusting it: reports the detected
     #     column mapping, the layout it will be read as, DEM void fraction,
     #     and - for a road network - how many connected components it has
     python data_ingestion.py --inspect data/raw/ner_roads.geojson

     # 2. let it FETCH the roads for a state instead of supplying a file.
     #    --bbox is read as S,W,N,E or W,S,E,N; the two are told apart by
     #    magnitude so the conflicting conventions cannot be mixed up silently.
     python data_ingestion.py --state Mizoram --bbox 21.9,91.5,24.5,93.5 \
         --landslides data/raw/gsi_inventory.csv --train
     python data_ingestion.py --state Assam --gsi-csv data/gsi_landslides.csv \
         --dem data/raw/srtm/ --rainfall data/raw/imd_district_daily.csv

     # 2b. fuse downloads you already have
     python data_ingestion.py \
         --roads    data/raw/ner_roads.geojson \
         --landslides data/raw/gsi_inventory.csv \
         --dem      data/raw/srtm/ \
         --slope-tif data/raw/aster_slope.tif \
         --rainfall data/raw/imd_district_daily.csv \
         --soil     online \
         --out      data/historical_hazards_2023.csv

     # 2c. re-run a previous fetch with no network at all
     python data_ingestion.py --state Mizoram --offline \
         --landslides data/raw/gsi_inventory.csv --log-level INFO

     # 3. train the engine on the fused table
     python hazard_prediction_engine.py \
         --hazards data/historical_hazards_2023.csv \
         --graph   data/ner_roads.geojson --retrain

 Author  : Team ICONIC (Team ID 119301) - Usha Martin University
 License : MIT
================================================================================
"""

from __future__ import annotations

import argparse
import array
import csv
import datetime as _dt
import gzip
import importlib.util
import io
import json
import logging
import math
import os
import random
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import (Any, Dict, Iterable, Iterator, List, Optional, Sequence,
                    Tuple)

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import hazard_prediction_engine as hpe  # noqa: E402

__version__ = "1.0.0"
DEFAULT_SEED = 26002
RAW_DIR = os.path.join(HERE, "data", "raw")
CACHE_DIR = os.path.join(RAW_DIR, "cache")

# --------------------------------------------------------------------------- #
#  optional dependencies - everything degrades gracefully
# --------------------------------------------------------------------------- #
def _probe(name: str):
    try:
        return __import__(name)
    except Exception:
        return None


np = _probe("numpy")
rasterio = _probe("rasterio")

def _available(name: str) -> bool:
    """Is a package importable?  find_spec does not execute it."""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


# osmnx is deliberately NOT imported here: it drags in geopandas, shapely,
# networkx and pyproj and costs several seconds, which would be paid on every
# run even when the roads come from a file on disk.  Presence is detected with
# find_spec (no execution) and the import happens on first use.
_osmnx_mod: Any = "unset"

HAS = {"numpy": np is not None, "rasterio": rasterio is not None,
       "osmnx": _available("osmnx")}


def osmnx():
    """Import osmnx on first use, or return None if it is absent."""
    global _osmnx_mod
    if _osmnx_mod == "unset":
        _osmnx_mod = _probe("osmnx")
        HAS["osmnx"] = _osmnx_mod is not None
    return _osmnx_mod


# --------------------------------------------------------------------------- #
#  PROGRESS OUTPUT - plain stdout by default, `logging` on request
# --------------------------------------------------------------------------- #
# The default is print() so the pipeline's output can be piped, diffed and
# grepped without a timestamp prefix on every line.  `--log-level` / `--log-file`
# switch the same messages over to the logging module, which adds timestamps and
# separates warnings from progress.
log = logging.getLogger("ingestion")
_LOGGING_ON = False


def configure_logging(level: str = "INFO",
                      logfile: Optional[str] = None) -> None:
    """Route every progress message through the `logging` module."""
    global _LOGGING_ON
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if logfile:
        d = os.path.dirname(os.path.abspath(logfile))
        if d:
            os.makedirs(d, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                            "%H:%M:%S")
    for h in handlers:
        h.setFormatter(fmt)
    log.handlers = handlers
    log.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    log.propagate = False
    _LOGGING_ON = True


def _log(msg: str, quiet: bool = False) -> None:
    if quiet:
        return
    if _LOGGING_ON:
        log.info(msg.rstrip())
    else:
        print(msg, flush=True)


def _warn(msg: str, quiet: bool = False) -> None:
    """A problem the user must see, but which does not abort the run."""
    if quiet:
        return
    if _LOGGING_ON:
        log.warning(msg.rstrip())
    else:
        print(msg, flush=True)


# --------------------------------------------------------------------------- #
#  COLUMN GUESSING - every portal exports different headers
# --------------------------------------------------------------------------- #
# Each entry maps a canonical field to the header spellings we accept.  Real
# exports from Bhukosh / NGDR / IMD / NEDFI vary in case, spacing and whether
# the unit is in the name, so matching is done on a normalised key.
COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "lat": ("lat", "latitude", "y", "lat_deg", "latitude_degree", "latdd",
            "y_coord", "point_y", "geo_lat"),
    "lon": ("lon", "long", "longitude", "x", "lon_deg", "longitude_degree",
            "londd", "x_coord", "point_x", "geo_long"),
    "date": ("date", "event_date", "occurrence_date", "landslide_date",
             "timestamp", "time", "reported_date", "disaster_date", "day",
             "obs_date", "observation_date"),
    "district": ("district", "dist", "district_name", "administrative_district",
                 "distt", "districtname"),
    "state": ("state", "state_name", "stname", "st_code"),
    "hazard_type": ("hazard_type", "landslide_type", "type", "event_type",
                    "disaster_type", "hazard", "subtype", "movement_type"),
    "severity": ("severity", "magnitude", "size", "grade", "impact",
                 "damage_intensity", "landslide_size"),
    "rainfall": ("rainfall", "rainfall_mm", "rainfallmm", "rain_mm", "rainmm",
                 "rf", "rf_mm", "rain", "rainfall_mm_hr", "hourly_rainfall",
                 "daily_rainfall", "rainfall_daily_mm", "precipitation",
                 "precip_mm", "precip", "rainfall_amount", "mm_rainfall"),
    "id": ("id", "landslide_id", "event_id", "record_id", "sl_no", "slno",
           "osm_id", "way_id", "edge_id", "segment_id", "unique_id"),
    "geometry_wkt": ("geometry", "wkt", "geom", "the_geom", "shape"),
}


def _norm_key(s: str) -> str:
    return "".join(ch for ch in str(s).strip().lower() if ch.isalnum())


def _read_json(path: str) -> Any:
    """json.load with the file handle actually closed."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def guess_columns(header: Sequence[str],
                  aliases: Dict[str, Tuple[str, ...]] = None
                  ) -> Dict[str, Optional[str]]:
    """Map canonical field names onto whatever the CSV header actually says."""
    aliases = aliases or COLUMN_ALIASES
    norm = {_norm_key(h): h for h in header}
    out: Dict[str, Optional[str]] = {}
    for canon, cands in aliases.items():
        out[canon] = None
        for c in cands:
            if _norm_key(c) in norm:
                out[canon] = norm[_norm_key(c)]
                break
    return out


def _to_float(v: Any, default: float = float("nan")) -> float:
    try:
        if v is None or str(v).strip() in ("", "-", "NA", "N/A", "null", "None",
                                           "nan", "NaN", "--"):
            return default
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


# ORDER MATTERS: %d/%m/%Y is tried before %m/%d/%Y, so an ambiguous 07/05/2023
# reads as 7 May - the Indian convention used by GSI, IMD and Bhukosh.  A US-style
# export must be normalised before ingestion or every such date will be wrong.
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y",
                "%d.%m.%Y", "%Y.%m.%d", "%d-%b-%Y", "%d %b %Y", "%d.%m.%y",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%y", "%Y%m%d")


def parse_date(s: Any) -> Optional[_dt.date]:
    if s is None:
        return None
    if isinstance(s, _dt.datetime):
        return s.date()
    if isinstance(s, _dt.date):
        return s
    txt = str(s).strip()
    if not txt:
        return None
    for fmt in DATE_FORMATS:
        try:
            return _dt.datetime.strptime(txt[:len(fmt) + 6], fmt).date()
        except ValueError:
            continue
    # last resort: first 10 chars look ISO-ish
    try:
        return _dt.date.fromisoformat(txt[:10])
    except ValueError:
        return None


def read_csv_rows(path: str, encoding: str = "utf-8-sig") -> Tuple[List[str], List[Dict[str, str]]]:
    """CSV reader tolerant of BOM, blank lines, CRLF and stray delimiters."""
    with open(path, "r", encoding=encoding, newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rdr = csv.DictReader(fh, dialect=dialect)
        header = rdr.fieldnames or []
        rows = [r for r in rdr if any((v or "").strip() for v in r.values())]
    return list(header), rows


# --------------------------------------------------------------------------- #
#  DEM READERS  (elevation + slope)
# --------------------------------------------------------------------------- #
SRTM_VOID = -32768


@dataclass
class DemGrid:
    """A regular lat/lon elevation grid with bilinear sampling and Horn slope.

    `data` is a flat row-major array indexed from the NORTH-WEST corner, so
    row 0 is the northern edge.  Works for SRTM `.hgt`, ESRI `.asc` and any
    raster we can read into this shape.
    """
    west: float
    south: float
    east: float
    north: float
    ncols: int
    nrows: int
    data: Any                      # array.array('h'|'f') or numpy 2-D
    nodata: float = SRTM_VOID
    source: str = "dem"
    _np2d: Any = field(default=None, repr=False)

    # -- pixel geometry ----------------------------------------------------
    @property
    def res_deg_x(self) -> float:
        return (self.east - self.west) / max(self.ncols - 1, 1)

    @property
    def res_deg_y(self) -> float:
        return (self.north - self.south) / max(self.nrows - 1, 1)

    def contains(self, lat: float, lon: float) -> bool:
        return (self.south - 1e-9) <= lat <= (self.north + 1e-9) and \
               (self.west - 1e-9) <= lon <= (self.east + 1e-9)

    def _indices(self, lat: float, lon: float) -> Tuple[float, float]:
        """Fractional (row, col) from the north-west corner."""
        col = (lon - self.west) / self.res_deg_x
        row = (self.north - lat) / self.res_deg_y
        return row, col

    def _cell(self, r: int, c: int) -> Optional[float]:
        if not (0 <= r < self.nrows and 0 <= c < self.ncols):
            return None
        if self._np2d is not None:
            v = float(self._np2d[r, c])
        else:
            v = float(self.data[r * self.ncols + c])
        return None if (v <= self.nodata + 1e-6 and self.nodata < -1000) else v

    # -- sampling ----------------------------------------------------------
    def elevation(self, lat: float, lon: float) -> Optional[float]:
        """Bilinear interpolation; None when outside coverage or all-void."""
        if not self.contains(lat, lon):
            return None
        r, c = self._indices(lat, lon)
        r0, c0 = int(math.floor(r)), int(math.floor(c))
        fr, fc = r - r0, c - c0
        vals = [(self._cell(r0 + dr, c0 + dc), (1 - fr if dr == 0 else fr) *
                 (1 - fc if dc == 0 else fc))
                for dr in (0, 1) for dc in (0, 1)]
        got = [(v, w) for v, w in vals if v is not None]
        wsum = sum(w for _v, w in got)
        if wsum > 0:
            return sum(v * w for v, w in got) / wsum
        # The bilinear stencil contributed nothing: either every cell is a void,
        # or the sample lands exactly on a void cell (fr=fc=0 puts all the weight
        # there).  SRTM3 has systematic voids over steep, cloud-covered NER
        # terrain, so fall back to the mean of the valid cells around it rather
        # than losing the sample - which would zero out slope and relief for the
        # whole segment.
        ring = [self._cell(r0 + dr, c0 + dc)
                for dr in (-1, 0, 1) for dc in (-1, 0, 1)]
        ring = [v for v in ring if v is not None]
        return sum(ring) / len(ring) if ring else None

    def slope_deg(self, lat: float, lon: float) -> Optional[float]:
        """Horn's (1981) 3x3 finite-difference slope, in degrees."""
        z = self.slope_window(lat, lon)
        if z is None:
            return None
        (a, b, c), (d, e, f), (g, h, i) = z
        # cell size in metres at this latitude
        m_per_deg_lat = 111132.92 - 559.82 * math.cos(2 * math.radians(lat)) \
            + 1.175 * math.cos(4 * math.radians(lat))
        cell_y = self.res_deg_y * m_per_deg_lat
        cell_x = self.res_deg_x * m_per_deg_lat * math.cos(math.radians(lat))
        if cell_x <= 0 or cell_y <= 0:
            return None
        dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8.0 * cell_x)
        dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8.0 * cell_y)
        return math.degrees(math.atan(math.hypot(dzdx, dzdy)))

    def slope_window(self, lat: float, lon: float
                     ) -> Optional[Tuple[Tuple[float, ...], ...]]:
        r, c = self._indices(lat, lon)
        ri, ci = int(round(r)), int(round(c))
        win = [[self._cell(ri + dr, ci + dc) for dc in (-1, 0, 1)]
               for dr in (-1, 0, 1)]
        flat = [v for row in win for v in row]
        if any(v is None for v in flat):
            return None
        return tuple(tuple(row) for row in win)

    def relief_m(self, lat: float, lon: float, radius_px: int = 4
                 ) -> Optional[float]:
        """Peak-to-valley relief inside a window - drives cut-slope inference."""
        r, c = self._indices(lat, lon)
        ri, ci = int(round(r)), int(round(c))
        vals = [self._cell(ri + dr, ci + dc)
                for dr in range(-radius_px, radius_px + 1)
                for dc in range(-radius_px, radius_px + 1)]
        vals = [v for v in vals if v is not None]
        return (max(vals) - min(vals)) if len(vals) > 3 else None


class DemMosaic:
    """A set of 1x1 degree tiles addressed like SRTM (`N26E091.hgt`)."""

    def __init__(self, grids: Optional[Sequence[DemGrid]] = None):
        self.grids: List[DemGrid] = list(grids or [])

    def add(self, g: DemGrid) -> None:
        self.grids.append(g)

    def __len__(self) -> int:
        return len(self.grids)

    def bbox(self) -> Tuple[float, float, float, float]:
        if not self.grids:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(g.west for g in self.grids), min(g.south for g in self.grids),
                max(g.east for g in self.grids), max(g.north for g in self.grids))

    def _grid_for(self, lat: float, lon: float) -> Optional[DemGrid]:
        for g in self.grids:
            if g.contains(lat, lon):
                return g
        return None

    def elevation(self, lat: float, lon: float) -> Optional[float]:
        g = self._grid_for(lat, lon)
        return g.elevation(lat, lon) if g else None

    def slope_deg(self, lat: float, lon: float) -> Optional[float]:
        g = self._grid_for(lat, lon)
        return g.slope_deg(lat, lon) if g else None

    def relief_m(self, lat: float, lon: float, radius_px: int = 4
                 ) -> Optional[float]:
        g = self._grid_for(lat, lon)
        return g.relief_m(lat, lon, radius_px) if g else None

    def summary(self) -> Dict[str, Any]:
        w, s, e, n = self.bbox()
        return {"tiles": len(self.grids), "bbox": [w, s, e, n],
                "sources": sorted({g.source for g in self.grids})}


def load_hgt(path: str) -> DemGrid:
    """Read an SRTM/NASADEM `.hgt` tile (optionally .gz) - pure stdlib.

    Format: big-endian int16, row-major from the NORTH-WEST corner.  1201x1201
    for 3 arc-second (SRTM3) and 3601x3601 for 1 arc-second (SRTM1/NASADEM).
    Tile name encodes its south-west corner: `N26E091.hgt`.
    """
    opener = gzip.open if path.lower().endswith(".gz") else open
    with opener(path, "rb") as fh:
        raw = fh.read()
    n = len(raw) // 2
    side = int(round(math.sqrt(n)))
    if side * side != n:
        raise ValueError(f"{path}: {n} samples is not a square grid")
    vals = array.array("h")
    vals.frombytes(raw[:side * side * 2])
    if sys.byteorder == "little":
        vals.byteswap()                      # .hgt is big-endian on disk
    base = os.path.basename(path).split(".")[0].upper()
    sw = _hgt_tile_corner(base)
    if sw is None:
        raise ValueError(
            f"{path}: cannot infer the tile corner from the file name. SRTM and "
            f"NASADEM tiles are named after their south-west corner "
            f"(N26E091.hgt); rename it, or export an ESRI .asc grid instead.")
    lat0, lon0 = sw
    return DemGrid(west=lon0, south=lat0, east=lon0 + 1.0, north=lat0 + 1.0,
                   ncols=side, nrows=side, data=vals, nodata=SRTM_VOID,
                   source=os.path.basename(path))


def _hgt_tile_corner(name: str) -> Optional[Tuple[float, float]]:
    """`N26E091` -> (26.0, 91.0), the tile's SOUTH-WEST corner."""
    n = name.upper().replace(".HGT", "").replace("_", "")
    try:
        if len(n) >= 7 and n[0] in "NS" and n[3] in "EW":
            lat = float(n[1:3]) * (1 if n[0] == "N" else -1)
            lon = float(n[4:7]) * (1 if n[3] == "E" else -1)
            return (lat, lon)
    except ValueError:
        return None
    return None


def load_asc(path: str) -> DemGrid:
    """Read an ESRI ASCII grid (.asc) - the format Bhuvan/QGIS export to."""
    hdr: Dict[str, float] = {}
    with open(path, "r") as fh:
        lines = fh.read().splitlines()
    i = 0
    while i < len(lines):
        parts = lines[i].split()
        if len(parts) == 2 and parts[0].lower() in ("ncols", "nrows", "xllcorner",
                                                    "yllcorner", "xllcenter",
                                                    "yllcenter", "cellsize",
                                                    "nodata_value", "dx", "dy"):
            hdr[parts[0].lower()] = float(parts[1])
            i += 1
        else:
            break
    body = "\n".join(lines[i:]).split()
    ncols = int(hdr.get("ncols", 0))
    nrows = int(hdr.get("nrows", 0))
    if not ncols or not nrows or len(body) < ncols * nrows:
        raise ValueError(f"{path}: bad header or truncated body "
                         f"({len(body)} values for {ncols}x{nrows})")
    cell = hdr.get("cellsize", hdr.get("dx", 0.000833333))
    xll = hdr.get("xllcorner", hdr.get("xllcenter", 0.0) - cell / 2)
    yll = hdr.get("yllcorner", hdr.get("yllcenter", 0.0) - cell / 2)
    vals = array.array("f", [float(v) for v in body[:ncols * nrows]])
    return DemGrid(west=xll, south=yll, east=xll + cell * (ncols - 1),
                   north=yll + cell * (nrows - 1), ncols=ncols, nrows=nrows,
                   data=vals, nodata=hdr.get("nodata_value", -9999.0),
                   source=os.path.basename(path))


def load_geotiff(path: str, band: int = 1) -> DemGrid:
    """Read a GeoTIFF via rasterio (CartoDEM, Copernicus GLO-30, ASTER GDEM v3)."""
    if rasterio is None:
        raise RuntimeError(
            "reading GeoTIFF needs rasterio (`pip install rasterio`); "
            "or convert with `gdal_translate -of AAIGrid in.tif out.asc` "
            "(GDAL ships with QGIS) and pass the .asc instead")
    with rasterio.open(path) as src:
        a = src.read(band)
        b = src.bounds
        nodata = src.nodata if src.nodata is not None else -9999.0
        grid = DemGrid(west=b.left, south=b.bottom, east=b.right, north=b.top,
                       ncols=a.shape[1], nrows=a.shape[0], data=None,
                       nodata=float(nodata), source=os.path.basename(path))
        grid._np2d = a
        return grid


def load_dem(path: str, quiet: bool = False) -> DemMosaic:
    """Load a DEM from a file or a directory of tiles into a mosaic."""
    mos = DemMosaic()
    if os.path.isdir(path):
        files = sorted(os.path.join(path, f) for f in os.listdir(path)
                       if f.lower().endswith((".hgt", ".hgt.gz", ".asc", ".tif",
                                              ".tiff", ".vrt")))
        if not files:
            raise FileNotFoundError(f"no DEM tiles found in {path}")
    elif os.path.isfile(path):
        files = [path]
    else:
        raise FileNotFoundError(path)
    for f in files:
        low = f.lower()
        try:
            if low.endswith(".asc"):
                mos.add(load_asc(f))
            elif low.endswith((".tif", ".tiff")):
                mos.add(load_geotiff(f))
            elif ".hgt" in low:
                mos.add(load_hgt(f))
            else:
                continue
            _log(f"    + DEM tile {os.path.basename(f)} "
                 f"({mos.grids[-1].ncols}x{mos.grids[-1].nrows})", quiet)
        except Exception as exc:                      # noqa: BLE001
            _log(f"    ! skipped {os.path.basename(f)}: {exc}", quiet)
    if not len(mos):
        raise RuntimeError(f"could not read any DEM from {path}")
    return mos


# --------------------------------------------------------------------------- #
#  SOILGRIDS 2.0  (static soil properties, 250 m)
# --------------------------------------------------------------------------- #
SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"
SOILGRIDS_FILES = "https://files.isric.org/soilgrids/latest/data/"
# SoilGrids returns INTEGERS; these divisors give conventional units.
SOILGRIDS_DIVISOR = {
    "bdod": 100.0,      # cg/cm3  -> g/cm3 (kg/dm3)
    "cec": 10.0,        # mmol(c)/kg -> cmol(c)/kg
    "cfvo": 10.0,       # cm3/dm3 -> vol%
    "clay": 10.0,       # g/kg -> %
    "sand": 10.0,       # g/kg -> %
    "silt": 10.0,       # g/kg -> %
    "soc": 10.0,        # dg/kg -> %
    "nitrogen": 100.0,  # cg/kg -> g/kg
    "phh2o": 10.0,      # pH*10 -> pH
    "ocd": 10.0,        # hg/m3 -> kg/m3
    "wv0010": 10.0,     # 0.1 v% -> vol%  (water content at -10 kPa)
    "wv0033": 10.0,     # 0.1 v% -> vol%  (field capacity, -33 kPa)
    "wv1500": 10.0,     # 0.1 v% -> vol%  (wilting point, -1500 kPa)
}
DEFAULT_SOIL_PROPS = ("clay", "sand", "silt", "bdod", "soc", "wv0033", "wv1500")


class SoilGridsClient:
    """Point queries against the ISRIC REST API with an on-disk JSON cache.

    IMPORTANT: SoilGrids has NO live soil-moisture product.  `wv0033` and
    `wv1500` are water-retention curve points (field capacity and wilting
    point).  They are exactly what a bucket model needs to convert rainfall
    into a saturation fraction - which is how `soil_saturation` is derived
    here.  For observed moisture use ERA5-Land or SMAP L4 (see load_moisture).
    """

    def __init__(self, cache_path: Optional[str] = None, timeout: float = 25.0,
                 depth: str = "0-5cm", props: Sequence[str] = DEFAULT_SOIL_PROPS,
                 quiet: bool = False, offline: bool = False,
                 nearest_km: float = 1.0):
        self.cache_path = cache_path or os.path.join(CACHE_DIR, "soilgrids.json")
        self.offline = offline      # True => serve the cache only, never dial out
        self.nearest_km = nearest_km
        self.timeout = timeout
        self.depth = depth
        self.props = tuple(props)
        self.quiet = quiet
        self.cache: Dict[str, Dict[str, float]] = {}
        self._index: Optional[Dict[Tuple[int, int], List[str]]] = None
        self.hits = self.near_hits = self.misses = self.errors = 0
        if os.path.exists(self.cache_path):
            try:
                self.cache = _read_json(self.cache_path)
            except Exception:
                self.cache = {}

    @staticmethod
    def key(lat: float, lon: float) -> str:
        # SoilGrids is a 250 m product: 3 decimals (~110 m) is finer than needed
        return f"{lat:.3f},{lon:.3f}"

    def flush(self) -> None:
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w") as fh:
            json.dump(self.cache, fh, indent=1, sort_keys=True)

    # -- nearest-neighbour cache lookup ----------------------------------
    # SoilGrids is a 250 m product, so an exact 3-decimal key match is an
    # arbitrary test: two points 40 m apart belong to the same cell, while a
    # 1 km segment centroid can fall outside the cell of either endpoint.  When
    # offline (or when the API fails) a cached point within `nearest_km` is
    # served instead of collapsing the segment to a texture default.
    CELL = 0.25                                    # degrees, ~28 km index cells

    def _build_index(self) -> None:
        idx: Dict[Tuple[int, int], List[str]] = {}
        for k in self.cache:
            try:
                la, lo = (float(x) for x in k.split(","))
            except ValueError:
                continue
            idx.setdefault((int(la / self.CELL), int(lo / self.CELL)), []).append(k)
        self._index = idx

    def _nearest_cached(self, lat: float, lon: float
                        ) -> Optional[Dict[str, float]]:
        if self.nearest_km <= 0 or not self.cache:
            return None
        if self._index is None:
            self._build_index()
        reach = int(math.ceil(self.nearest_km / (111.0 * self.CELL))) + 1
        r0, c0 = int(lat / self.CELL), int(lon / self.CELL)
        kmlon = 111.0 * max(0.2, math.cos(math.radians(lat)))
        best, bd = None, self.nearest_km ** 2
        for dr in range(-reach, reach + 1):
            for dc in range(-reach, reach + 1):
                for k in self._index.get((r0 + dr, c0 + dc), ()):
                    la, lo = (float(x) for x in k.split(","))
                    d2 = ((la - lat) * 111.0) ** 2 + ((lo - lon) * kmlon) ** 2
                    if d2 < bd:
                        bd, best = d2, k
        return self.cache[best] if best else None

    def query(self, lat: float, lon: float) -> Optional[Dict[str, float]]:
        k = self.key(lat, lon)
        if k in self.cache:
            self.hits += 1
            return self.cache[k]
        if self.offline:
            near = self._nearest_cached(lat, lon)
            if near is not None:
                self.near_hits += 1
                return near
            self.misses += 1
            return None
        q = urllib.parse.urlencode(
            [("lon", f"{lon:.6f}"), ("lat", f"{lat:.6f}"),
             ("depth", self.depth), ("value", "mean")]
            + [("property", p) for p in self.props])
        url = f"{SOILGRIDS_URL}?{q}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent":
                                                       "SIH26002-hazard-engine/1.0"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
            out = self._parse(doc)
            self.cache[k] = out
            self.misses += 1
            return out
        except Exception as exc:                              # noqa: BLE001
            self.errors += 1
            near = self._nearest_cached(lat, lon)
            if near is not None:
                self.near_hits += 1
                if self.errors == 1:
                    _log(f"    ! SoilGrids unreachable ({type(exc).__name__}) - "
                         f"serving cached values within {self.nearest_km:g} km",
                         self.quiet)
                return near
            if self.errors in (1, 10, 100):
                _log(f"    ! SoilGrids unavailable ({type(exc).__name__}: {exc}) "
                     f"- falling back to texture defaults", self.quiet)
            return None

    @staticmethod
    def _parse(doc: Dict[str, Any]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        layers = (((doc.get("properties") or {}).get("layers")) or [])
        for layer in layers:
            name = layer.get("name")
            depths = layer.get("depths") or []
            if not depths:
                continue
            vals = depths[0].get("values") or {}
            raw = vals.get("mean", vals.get("Q0.5"))
            if raw is None:
                continue
            out[name] = float(raw) / SOILGRIDS_DIVISOR.get(name, 1.0)
        return out

    def stats(self) -> Dict[str, Any]:
        return {"cached_points": len(self.cache), "cache_hits": self.hits,
                "cache_nearest_hits": self.near_hits,
                "nearest_km": self.nearest_km, "offline": self.offline,
                "unresolved_points": self.misses, "api_errors": self.errors}


def soil_type_from_texture(clay_pct: float, sand_pct: float, silt_pct: float
                           ) -> str:
    """USDA textural triangle -> the engine's soil_type vocabulary."""
    if clay_pct >= 40:
        return "clay_loam" if sand_pct >= 20 else "clay"
    if sand_pct >= 70:
        return "sandy_loam"
    if silt_pct >= 50:
        return "silt_loam"
    if clay_pct >= 27 and sand_pct < 45:
        return "clay_loam"
    if clay_pct < 15 and sand_pct >= 40:
        return "sandy_loam"
    return "loam"


def derive_soil_features(props: Optional[Dict[str, float]]
                         ) -> Dict[str, Any]:
    """Map SoilGrids properties onto the engine's soil columns.

    `drainage` is a heuristic proxy: SoilGrids 2.0 does not publish saturated
    hydraulic conductivity, so it is estimated from texture, bulk density and
    the retention span.  Flagged as DERIVED in the provenance report.
    """
    if not props:
        return {"soil_type": "laterite", "drainage": 0.5, "awc_pct": None,
                "porosity_pct": 48.0,
                "field_capacity_pct": None, "wilting_point_pct": None,
                "soc_pct": None, "bdod_g_cm3": None, "source": "imputed"}
    clay = props.get("clay")
    sand = props.get("sand")
    silt = props.get("silt")
    bdod = props.get("bdod")
    fc = props.get("wv0033")
    wp = props.get("wv1500")
    soc = props.get("soc")
    st = soil_type_from_texture(clay if clay is not None else 25.0,
                                sand if sand is not None else 35.0,
                                silt if silt is not None else 40.0)
    # drainage proxy: sandy + low bulk density + wide FC-WP spread drains fast
    d = 0.5
    if sand is not None and clay is not None:
        d += 0.0045 * (sand - clay)
    if bdod is not None:
        d -= 0.35 * (bdod - 1.35)
    if fc is not None and wp is not None:
        d += 0.01 * ((fc - wp) - 15.0)
    awc = (fc - wp) if (fc is not None and wp is not None) else None
    return {"soil_type": st,
            "porosity_pct": round(porosity_from_bdod(bdod), 2),
            "drainage": round(hpe.clamp(d, 0.05, 0.95), 3),
            "awc_pct": round(awc, 2) if awc is not None else None,
            "field_capacity_pct": round(fc, 2) if fc is not None else None,
            "wilting_point_pct": round(wp, 2) if wp is not None else None,
            "soc_pct": round(soc, 2) if soc is not None else None,
            "bdod_g_cm3": round(bdod, 3) if bdod is not None else None,
            "source": "soilgrids"}


# --------------------------------------------------------------------------- #
#  LANDSLIDE / DISRUPTION INVENTORY  (GSI Bhukosh, NGDR, NDEM, ASDMA)
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    lat: float
    lon: float
    date: Optional[_dt.date]
    district: str = ""
    state: str = ""
    hazard_type: str = "landslide"
    severity: str = ""
    rainfall_mm: Optional[float] = None
    source_id: str = ""
    raw: Dict[str, str] = field(default_factory=dict)


HAZARD_TYPE_MAP = {
    "landslide": "landslide", "land slide": "landslide", "ls": "landslide",
    "rockslide": "landslide", "rock slide": "landslide",
    "debris flow": "debris_flow", "debrisflow": "debris_flow",
    "mud flow": "debris_flow", "mudflow": "debris_flow",
    "slope failure": "slope_failure", "slopefailure": "slope_failure",
    "earth slip": "slope_failure", "slump": "slope_failure",
    "boulder fall": "boulder_fall", "rockfall": "boulder_fall",
    "rock fall": "boulder_fall",
    "road collapse": "road_collapse", "roaddamage": "road_collapse",
    "road damage": "road_collapse", "pavement failure": "road_collapse",
    "flash flood": "flash_flood", "flashflood": "flash_flood",
    "flood": "flash_flood", "river erosion": "flash_flood",
    "waterlogging": "waterlogging", "water logging": "waterlogging",
    "inundation": "waterlogging",
    "embankment washout": "embankment_washout",
    "embankment failure": "embankment_washout", "breach": "embankment_washout",
}


def normalise_hazard_type(v: Any) -> str:
    key = _norm_key(v)
    if not key:
        return "landslide"
    if key in HAZARD_TYPE_MAP:
        return HAZARD_TYPE_MAP[key]
    for k, mapped in HAZARD_TYPE_MAP.items():
        if _norm_key(k) in key:
            return mapped
    return "landslide"


def load_events(path: str, quiet: bool = False) -> Tuple[List[Event], Dict[str, Any]]:
    """Load a landslide/disruption inventory from CSV or GeoJSON."""
    events: List[Event] = []
    info: Dict[str, Any] = {"path": path, "rows_read": 0, "kept": 0,
                            "no_coordinates": 0, "no_date": 0,
                            "column_map": {}, "format": ""}
    if path.lower().endswith((".geojson", ".json")):
        info["format"] = "geojson"
        doc = _read_json(path)
        feats = doc.get("features", doc if isinstance(doc, list) else [])
        info["rows_read"] = len(feats)
        for f in feats:
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates")
            if not coords:
                info["no_coordinates"] += 1
                continue
            while isinstance(coords[0], list):
                coords = coords[0]                     # polygon -> first ring pt
            pr = f.get("properties") or {}
            d = None
            for k in ("date", "event_date", "timestamp", "occurrence_date"):
                if pr.get(k):
                    d = parse_date(pr[k])
                    break
            events.append(Event(
                lat=float(coords[1]), lon=float(coords[0]), date=d,
                district=str(pr.get("district", "") or ""),
                state=str(pr.get("state", "") or ""),
                hazard_type=normalise_hazard_type(
                    pr.get("hazard_type") or pr.get("type") or pr.get("landslide_type")),
                severity=str(pr.get("severity", "") or ""),
                rainfall_mm=(None if pr.get("rainfall") is None
                             else _to_float(pr.get("rainfall"))),
                source_id=str(pr.get("id") or pr.get("landslide_id") or ""),
                raw={k: str(v) for k, v in pr.items()}))
    else:
        info["format"] = "csv"
        header, rows = read_csv_rows(path)
        cmap = guess_columns(header)
        info["column_map"] = {k: v for k, v in cmap.items() if v}
        info["rows_read"] = len(rows)
        if not cmap["lat"] or not cmap["lon"]:
            raise ValueError(
                f"{path}: cannot find latitude/longitude columns in {header}. "
                f"Pass a CSV with lat/lon, or a GeoJSON point file.")
        for r in rows:
            lat = _to_float(r.get(cmap["lat"]))
            lon = _to_float(r.get(cmap["lon"]))
            if math.isnan(lat) or math.isnan(lon) or lat == 0.0 and lon == 0.0:
                info["no_coordinates"] += 1
                continue
            if not (15.0 <= lat <= 35.0 and 80.0 <= lon <= 100.0):
                info["no_coordinates"] += 1        # outside a generous NER box
                continue
            d = parse_date(r.get(cmap["date"])) if cmap["date"] else None
            if d is None:
                info["no_date"] += 1
            rf = None
            if cmap["rainfall"]:
                rf = _to_float(r.get(cmap["rainfall"]))
                rf = None if math.isnan(rf) else rf
            events.append(Event(
                lat=lat, lon=lon, date=d,
                district=str(r.get(cmap["district"], "") or "").strip(),
                state=str(r.get(cmap["state"], "") or "").strip(),
                hazard_type=normalise_hazard_type(
                    r.get(cmap["hazard_type"], "") if cmap["hazard_type"] else ""),
                severity=str(r.get(cmap["severity"], "") or "").strip(),
                rainfall_mm=rf,
                source_id=str(r.get(cmap["id"], "") or "").strip(),
                raw=r))
    info["kept"] = len(events)
    info["with_date"] = sum(1 for e in events if e.date)
    info["date_range"] = ([min(e.date for e in events if e.date).isoformat(),
                           max(e.date for e in events if e.date).isoformat()]
                          if info.get("with_date") else None)
    info["type_mix"] = {}
    for e in events:
        info["type_mix"][e.hazard_type] = info["type_mix"].get(e.hazard_type, 0) + 1
    _log(f"  landslide inventory : {info['kept']}/{info['rows_read']} events "
         f"({info['format']}), {info.get('with_date', 0)} dated", quiet)
    return events, info


# --------------------------------------------------------------------------- #
#  ROAD NETWORK  (OSM / OSMnx / Overpass / NESAC / Bhuvan)
# --------------------------------------------------------------------------- #
NER_STATE_BOXES = {
    "Assam":              (89.5, 24.0, 96.1, 28.0),
    "Meghalaya":          (89.5, 24.9, 92.9, 26.2),
    "Arunachal Pradesh":  (91.5, 26.5, 97.4, 29.5),
    "Nagaland":           (93.3, 25.2, 95.4, 27.1),
    "Manipur":            (92.9, 23.8, 94.8, 25.7),
    "Mizoram":            (92.1, 21.9, 93.5, 24.6),
    "Tripura":            (91.1, 22.9, 92.4, 24.4),
    "Sikkim":             (88.0, 27.0, 88.9, 28.2),
}


def state_of(lat: float, lon: float) -> str:
    for st, (w, s, e, n) in NER_STATE_BOXES.items():
        if s <= lat <= n and w <= lon <= e:
            return st
    return "North East"


# Accepted spellings for --state, normalised through _norm_key.  The canonical
# names above are what lands in the fused CSV's `state` column; these are what
# people actually type on a command line.
STATE_ALIASES: Dict[str, str] = {
    "arunachal": "Arunachal Pradesh",
    "arunachalpradesh": "Arunachal Pradesh",
    "ap": "Arunachal Pradesh",
    "ar": "Arunachal Pradesh",
    "assam": "Assam",
    "as": "Assam",
    "meghalaya": "Meghalaya",
    "ml": "Meghalaya",
    "nagaland": "Nagaland",
    "nl": "Nagaland",
    "manipur": "Manipur",
    "mn": "Manipur",
    "mizoram": "Mizoram",
    "mz": "Mizoram",
    "tripura": "Tripura",
    "tr": "Tripura",
    "sikkim": "Sikkim",
    "sk": "Sikkim",
}


def resolve_state(name: Any) -> Optional[str]:
    """Map any spelling of a NER state onto its canonical name, or None."""
    if name is None:
        return None
    key = _norm_key(name)
    for st in NER_STATE_BOXES:
        if _norm_key(st) == key:
            return st
    return STATE_ALIASES.get(key)


def parse_bbox(spec: Any) -> Tuple[float, float, float, float]:
    """Parse a `--bbox` string into (west, south, east, north).

    The two orderings in circulation disagree: Overpass and most GIS tools use
    S,W,N,E while OSMnx's `graph_from_bbox` uses W,S,E,N.  Rather than pick one
    and silently mis-read the other, the ordering is detected from the values -
    in the NER longitudes are 88-98 and latitudes 21-30, so any magnitude above
    60 can only be a longitude.  Outside that range the documented S,W,N,E
    order is assumed.
    """
    if isinstance(spec, (tuple, list)):
        vals = [float(v) for v in spec]
    else:
        vals = [float(x) for x in str(spec).replace(";", ",").split(",")
                if str(x).strip()]
    if len(vals) != 4:
        raise ValueError(f"expected 4 numbers, got {len(vals)}: {spec!r}")
    a, b, c, d = vals

    if a > 60.0 and c > 60.0:          # W,S,E,N  (OSMnx order)
        order, w, s_, e, nn = "W,S,E,N", a, b, c, d
    elif b > 60.0 and d > 60.0:        # S,W,N,E  (Overpass / GIS order)
        order, w, s_, e, nn = "S,W,N,E", b, a, d, c
    else:
        order, w, s_, e, nn = "S,W,N,E", b, a, d, c
    for v in vals:
        if not (-180.0 <= v <= 180.0):
            raise ValueError(f"{v} is not a valid latitude or longitude")
    if not (-90.0 <= s_ <= 90.0 and -90.0 <= nn <= 90.0):
        raise ValueError(f"latitudes {s_},{nn} outside -90..90 - did you swap "
                         f"the order? read as {order}")
    if not (-180.0 <= w <= 180.0 and -180.0 <= e <= 180.0):
        raise ValueError(f"longitudes {w},{e} outside -180..180")
    if w >= e or s_ >= nn:
        raise ValueError(f"empty box: west {w} must be < east {e} and south "
                         f"{s_} < north {nn} (read as {order})")
    if nn - s_ > 6.0 or e - w > 8.0:
        raise ValueError(f"box spans {nn - s_:.1f} deg of latitude and "
                         f"{e - w:.1f} deg of longitude - that is far larger "
                         f"than any NER state and will pull hundreds of "
                         f"thousands of OSM ways. Narrow it, or raise "
                         f"--max-edges knowingly.")
    return (w, s_, e, nn)


def inventory_centroid(events: Sequence["Event"]
                       ) -> Optional[Tuple[float, float]]:
    """Median lat/lon of an inventory - where to centre a cropped road fetch.

    A state box can be 500 km across (Assam) while the landslides sit in one
    district.  Cropping a fetch around the box centre would return roads with no
    events anywhere near them; cropping around the inventory returns the roads
    the labels actually describe.  The median, not the mean, so a handful of
    mistyped coordinates cannot drag the window off the region.
    """
    pts = [(ev.lat, ev.lon) for ev in events
           if ev.lat is not None and ev.lon is not None
           and -90.0 <= ev.lat <= 90.0 and -180.0 <= ev.lon <= 180.0]
    if not pts:
        return None
    lats = sorted(p[0] for p in pts)
    lons = sorted(p[1] for p in pts)
    m = len(lats) // 2
    mid = (lambda v: v[m] if len(v) % 2 else 0.5 * (v[m - 1] + v[m]))
    return (mid(lats), mid(lons))


# --------------------------------------------------------------------------- #
#  LIVE ROAD FETCH  (OSMnx if installed, else Overpass through urllib)
# --------------------------------------------------------------------------- #
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

# drivable ways only - matches OSMnx network_type="drive" closely enough that
# the two fetch paths produce comparable networks
DRIVE_HIGHWAYS = ("motorway", "motorway_link", "trunk", "trunk_link",
                  "primary", "primary_link", "secondary", "secondary_link",
                  "tertiary", "tertiary_link", "unclassified", "residential")
ALL_HIGHWAYS = DRIVE_HIGHWAYS + ("service", "track", "living_street",
                                 "pedestrian", "road")

# OSM tags worth carrying into the GeoJSON; everything else is dropped so the
# cached file stays small and the loader's tag handling stays the single place
# that interprets them.
KEEP_OSM_TAGS = ("highway", "surface", "lanes", "maxspeed", "name", "ref",
                 "cutting", "embankment", "bridge", "tunnel", "width", "lit",
                 "tracktype", "sac_scale", "oneway", "access", "service")


def overpass_to_geojson(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Turn an Overpass `out geom;` reply into a LineString FeatureCollection.

    `out geom;` inlines the full vertex list on each way, so this needs no node
    lookup pass and no second query - which matters because Overpass rate-limits
    hard.
    """
    feats: List[Dict[str, Any]] = []
    for el in doc.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        coords = [[float(p["lon"]), float(p["lat"])] for p in geom
                  if "lat" in p and "lon" in p]
        if len(coords) < 2:
            continue
        tags = el.get("tags") or {}
        props = {k: tags[k] for k in KEEP_OSM_TAGS if k in tags}
        props["osm_id"] = el.get("id")
        feats.append({"type": "Feature", "id": f"way/{el.get('id')}",
                      "properties": props,
                      "geometry": {"type": "LineString", "coordinates": coords}})
    return {"type": "FeatureCollection", "features": feats}


def fetch_roads_overpass(bbox: Tuple[float, float, float, float],
                         network_type: str = "drive", timeout: int = 180,
                         quiet: bool = False) -> Tuple[Dict[str, Any], str]:
    """Fetch drivable OSM ways for a bbox using only the standard library."""
    w, s_, e, nn = bbox
    hw = "|".join(DRIVE_HIGHWAYS if network_type == "drive" else ALL_HIGHWAYS)
    query = (f'[out:json][timeout:{int(timeout)}];'
             f'way["highway"~"^({hw})$"]({s_},{w},{nn},{e});'
             f'out geom;')
    last: Optional[Exception] = None
    for url in OVERPASS_ENDPOINTS:
        try:
            req = urllib.request.Request(
                url, data=query.encode("utf-8"),
                headers={"User-Agent": "SIH26002-ingestion/1.0 (research)",
                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
            gj = overpass_to_geojson(doc)
            _log(f"    Overpass            : {url} -> {len(gj['features'])} ways",
                 quiet)
            return gj, url
        except Exception as exc:                       # noqa: BLE001
            last = exc
            _warn(f"    Overpass {url.split('/')[2]} failed: {exc}", quiet)
    raise RuntimeError(f"all Overpass endpoints failed (last: {last})")


def _flat(v: Any) -> Any:
    """OSMnx stores multi-valued tags as lists; the loader wants one scalar."""
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def osmnx_to_geojson(G: Any) -> Dict[str, Any]:
    """Convert an OSMnx MultiDiGraph to a FeatureCollection WITHOUT geopandas.

    `ox.graph_to_geojson` does not exist in any released version, and the
    documented route (`ox.convert.graph_to_gdfs` then `gdf.to_file`) needs
    geopandas and pyogrio.  Reading the graph directly needs neither, keeps the
    `key` of parallel edges, and preserves OSM connectivity - two ways that meet
    at a node keep that node's OSM id on both ends, which is what stops the
    loader from splitting them into separate components.
    """
    feats: List[Dict[str, Any]] = []
    for u, v, _k, d in G.edges(keys=True, data=True):
        un, vn = G.nodes[u], G.nodes[v]
        geom = d.get("geometry")
        if geom is None:
            coords = [[float(un["x"]), float(un["y"])],
                      [float(vn["x"]), float(vn["y"])]]
        else:
            coords = [[float(c[0]), float(c[1])] for c in geom.coords]
        if len(coords) < 2:
            continue
        props = {t: _flat(d[t]) for t in KEEP_OSM_TAGS if t in d}
        props["osm_id"] = _flat(d.get("osmid"))
        props["length"] = float(d.get("length") or 0.0)
        feats.append({"type": "Feature", "id": f"way/{props['osm_id']}",
                      "properties": props,
                      "geometry": {"type": "LineString", "coordinates": coords}})
    return {"type": "FeatureCollection", "features": feats}


def fetch_roads_osmnx(bbox: Tuple[float, float, float, float],
                      network_type: str = "drive", simplify: bool = True,
                      quiet: bool = False) -> Dict[str, Any]:
    """Fetch a road graph with OSMnx.  Raises ImportError when it is absent."""
    ox = osmnx()
    if ox is None:
        raise ImportError("osmnx is not installed")
    w, s_, e, nn = bbox
    _log(f"    OSMnx               : fetching {network_type} network for "
         f"bbox ({w},{s_},{e},{nn}) ...", quiet)
    try:
        # OSMnx >= 1.9: bbox=(west, south, east, north)
        G = ox.graph_from_bbox(bbox=(w, s_, e, nn), network_type=network_type,
                               simplify=simplify)
    except TypeError:
        # OSMnx <= 1.8: positional north, south, east, west
        G = ox.graph_from_bbox(nn, s_, e, w, network_type=network_type,
                               simplify=simplify)
    gj = osmnx_to_geojson(G)
    _log(f"    OSMnx               : {G.number_of_nodes()} nodes / "
         f"{G.number_of_edges()} edges -> {len(gj['features'])} ways", quiet)
    return gj


def crop_to_budget(gj: Dict[str, Any], bbox: Tuple[float, float, float, float],
                   max_edges: int,
                   centre: Optional[Tuple[float, float]] = None
                   ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Shrink the fetch window until the way count fits `max_edges`.

    Cropping geometrically rather than sampling rows.  `edges.sample(n)` - the
    obvious way to cap a network - picks ways at random, so almost every
    surviving way loses its neighbours and the "network" becomes thousands of
    two-node islands: routing then reports NO PATH for every corridor while
    nothing anywhere raises an error.  Keeping a smaller *window* keeps the ways
    inside it connected to each other.
    """
    feats = gj.get("features", [])
    info: Dict[str, Any] = {"cropped": False, "ways_before": len(feats),
                            "ways_after": len(feats), "window": list(bbox)}
    if not max_edges or max_edges <= 0 or len(feats) <= max_edges:
        return gj, info
    w, s_, e, nn = bbox
    clat, clon = centre if centre else (0.5 * (s_ + nn), 0.5 * (w + e))
    clat = min(max(clat, s_), nn)
    clon = min(max(clon, w), e)
    # area scales with the square of the linear shrink, so f = sqrt(budget/n)
    f = math.sqrt(float(max_edges) / float(len(feats)))
    hw, hh = 0.5 * (e - w) * f, 0.5 * (nn - s_) * f
    w2, e2 = max(w, clon - hw), min(e, clon + hw)
    s2, n2 = max(s_, clat - hh), min(nn, clat + hh)
    kept = []
    for ft in feats:
        coords = (ft.get("geometry") or {}).get("coordinates") or []
        if not coords:
            continue
        lats = [c[1] for c in coords]
        lons = [c[0] for c in coords]
        if min(lats) >= s2 and max(lats) <= n2 and min(lons) >= w2 \
                and max(lons) <= e2:
            kept.append(ft)
    info.update({"cropped": True, "ways_after": len(kept),
                 "window": [round(v, 4) for v in (w2, s2, e2, n2)],
                 "centre": [round(clat, 4), round(clon, 4)],
                 "note": "window shrunk around the inventory centroid; ways "
                         "crossing the new boundary are dropped whole"})
    return {"type": "FeatureCollection", "features": kept}, info


def acquire_roads(bbox: Tuple[float, float, float, float],
                  state: Optional[str] = None, network_type: str = "drive",
                  max_edges: int = 4000, cache_path: Optional[str] = None,
                  offline: bool = False, quiet: bool = False,
                  overpass_timeout: int = 180,
                  centre: Optional[Tuple[float, float]] = None
                  ) -> Tuple[str, Dict[str, Any]]:
    """Get a road network GeoJSON for a bbox: cache, else OSMnx, else Overpass.

    Returns (path, info).  The result is always written to `cache_path` so the
    next run is offline - an OSM fetch of a whole state takes minutes and is
    rate-limited, and a demo must not die because a mirror is down.
    """
    info: Dict[str, Any] = {"bbox": [round(v, 4) for v in bbox],
                            "state": state, "network_type": network_type,
                            "source": None, "cache": cache_path,
                            "max_edges": max_edges}
    if cache_path and os.path.exists(cache_path) and not os.environ.get(
            "SIH_IGNORE_ROAD_CACHE"):
        doc = _read_json(cache_path)
        cached_bbox = (doc.get("metadata") or {}).get("bbox")
        n_feat = len(doc.get("features", []))
        matches = (isinstance(cached_bbox, (list, tuple))
                   and len(cached_bbox) == 4
                   and all(abs(float(a) - float(b)) < 1e-6
                           for a, b in zip(cached_bbox, bbox)))
        if matches or offline:
            info.update({"source": "cache", "ways": n_feat,
                         "cached_bbox": cached_bbox,
                         "cache_matches_request": matches})
            _log(f"    road cache          : {cache_path} ({n_feat} ways, "
                 f"offline)", quiet)
            if not matches:
                # --offline forbids a re-fetch, so say loudly that the roads are
                # from a different window: routing over another state's network
                # produces plausible numbers that no metric would flag
                _warn(f"    ! that cache was fetched for bbox {cached_bbox}, not "
                      f"the requested {[round(v, 3) for v in bbox]}. --offline "
                      f"forces its use; drop --offline or delete the file to "
                      f"re-fetch.", quiet)
            return cache_path, info
        _warn(f"    road cache          : {cache_path} was fetched for bbox "
              f"{cached_bbox}, not {[round(v, 3) for v in bbox]} - re-fetching",
              quiet)
        info["stale_cache_refreshed_from"] = cached_bbox
    if offline:
        raise RuntimeError(
            f"--offline was given but there is no cached road network at "
            f"{cache_path}. Fetch it once with network access, or pass --roads.")

    gj: Optional[Dict[str, Any]] = None
    try:
        gj = fetch_roads_osmnx(bbox, network_type=network_type, quiet=quiet)
        info["source"] = "osmnx"
    except ImportError:
        _log("    OSMnx               : not installed - using the Overpass API "
             "instead (stdlib only)", quiet)
    except Exception as exc:                           # noqa: BLE001
        _warn(f"    OSMnx               : failed ({exc}) - falling back to "
              f"Overpass", quiet)
        info["osmnx_error"] = str(exc)
    if gj is None:
        gj, url = fetch_roads_overpass(bbox, network_type=network_type,
                                       timeout=overpass_timeout, quiet=quiet)
        info["source"] = info.get("source") or "overpass"
        info["endpoint"] = url

    if not gj.get("features"):
        raise RuntimeError(
            f"the fetch returned 0 ways for bbox {info['bbox']}. Either the box "
            f"is empty (check that --bbox was not given as W,S,E,N when you "
            f"meant S,W,N,E - this loader accepts both) or the highway filter "
            f"matched nothing there.")

    gj, crop = crop_to_budget(gj, bbox, max_edges, centre=centre)
    info["crop"] = crop
    info["ways"] = len(gj["features"])
    if crop.get("cropped"):
        _warn(f"    crop                : {crop['ways_before']} ways exceeds "
              f"--max-edges {max_edges}; window shrunk to "
              f"{crop['window']} around {crop.get('centre')} keeping "
              f"{crop['ways_after']} ways (raise --max-edges for more, "
              f"0 = unlimited)", quiet)

    out = cache_path or os.path.join(CACHE_DIR, "osm_roads.geojson")
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    gj["metadata"] = {"crs": "EPSG:4326", "source": info["source"],
                      "fetched": _dt.datetime.now().isoformat(timespec="seconds"),
                      "bbox": [round(v, 6) for v in bbox],
                      "window": crop.get("window"), "state": state,
                      "network_type": network_type,
                      "generator": f"data_ingestion {__version__}"}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(gj, fh)
    _log(f"    roads fetched       : {info['ways']} ways -> {out}", quiet)
    return out, info


HIGHWAY_SPEED = {"motorway": 80.0, "trunk": 70.0, "primary": 55.0,
                 "secondary": 45.0, "tertiary": 35.0, "unclassified": 30.0,
                 "residential": 25.0, "service": 20.0, "track": 18.0}


def _feature_parts(f: Dict[str, Any]) -> Iterator[Tuple[List[List[float]],
                                                        Dict[str, Any]]]:
    """Yield (coordinates, properties) for every line part of a GeoJSON feature.

    OSMnx/Overpass exports routinely use MultiLineString (one way, several
    parts), and dropping those silently discards real road.
    """
    geom = f.get("geometry") or {}
    pr = f.get("properties") or {}
    kind = geom.get("type")
    if kind == "LineString":
        cs = geom.get("coordinates") or []
        if len(cs) >= 2:
            yield cs, pr
    elif kind == "MultiLineString":
        for cs in geom.get("coordinates") or []:
            if len(cs) >= 2:
                yield cs, pr


def roads_from_geojson(path: str, min_length_m: float = 50.0,
                       max_length_m: float = 12000.0,
                       quiet: bool = False) -> Tuple[hpe.RoadGraph, Dict[str, Any]]:
    """Build an engine RoadGraph from any EPSG:4326 LineString FeatureCollection.

    Accepts raw Overpass/OSMnx output (with `highway`, `surface`, `lanes`,
    `maxspeed`, `cutting`, `embankment`, `name`, `ref` tags) as well as the
    engine's own format, in LineString or MultiLineString.  Long ways are split
    into routable segments of at most `max_length_m` so that per-segment weather
    and DEM sampling stay meaningful - but ALWAYS at a junction, so the split
    cannot cut a T-junction out of the topology (see the pre-pass below).
    """
    doc = _read_json(path)
    feats = doc.get("features", [])
    g = hpe.RoadGraph()
    info: Dict[str, Any] = {"path": path, "features": len(feats), "edges": 0,
                            "skipped_no_geom": 0, "skipped_short": 0,
                            "split_count": 0, "junction_splits": 0,
                            "highway_mix": {},
                            "osm_tags_used": set()}
    node_seq = 0
    seg_seq = 0

    # -- junction pre-pass ---------------------------------------------------
    # In real OSM, connectivity lives in shared NODE IDs.  Rebuilt from
    # coordinates, any vertex appearing in two ways is a junction and must
    # become a graph node; otherwise a feeder road that meets a trunk mid-way
    # looks connected in the source file yet ends up a separate component after
    # length-based splitting.  Endpoints are counted double so a way ending on
    # another way's interior vertex still registers as a junction.
    vcount: Dict[str, int] = {}
    parts: List[Tuple[List[List[float]], Dict[str, Any]]] = []
    for f in feats:
        got = list(_feature_parts(f))
        if not got:
            info["skipped_no_geom"] += 1
        for cs, pr in got:
            parts.append((cs, pr))
            last = len(cs) - 1
            for i, c in enumerate(cs):
                k = f"{c[1]:.6f}|{c[0]:.6f}"
                vcount[k] = vcount.get(k, 0) + (2 if i in (0, last) else 1)
    junctions = {k for k, v in vcount.items() if v >= 2}
    info["junction_vertices"] = len(junctions)
    info["line_parts"] = len(parts)

    def ensure_node(lat: float, lon: float) -> str:
        nonlocal node_seq
        key = f"{lat:.6f}|{lon:.6f}"
        if key in g.metadata.setdefault("_node_keys", {}):
            return g.metadata["_node_keys"][key]
        nid = f"N{node_seq:06d}"
        node_seq += 1
        g.add_node(nid, lat, lon, 0.0)
        g.metadata["_node_keys"][key] = nid
        return nid

    for coords, pr in parts:
        for k in pr:
            info["osm_tags_used"].add(k)
        hw = str(pr.get("highway") or pr.get("road_class") or "unclassified")
        info["highway_mix"][hw] = info["highway_mix"].get(hw, 0) + 1
        name = str(pr.get("name") or pr.get("ref") or "")
        surface = str(pr.get("surface") or "asphalt")
        lanes = int(_to_float(pr.get("lanes"), 2) or 2)
        speed = _to_float(pr.get("maxspeed"), float("nan"))
        if math.isnan(speed):
            speed = HIGHWAY_SPEED.get(hw, 35.0)
        speed = hpe.clamp(speed, 12.0, 100.0)
        cutting = str(pr.get("cutting", "")).lower() in ("yes", "true", "1")
        embank = str(pr.get("embankment", "")).lower() in ("yes", "true", "1")
        st = str(pr.get("state") or "") or None

        # Split long ways - but never THROUGH a junction.  Two different cuts:
        #   length cut  - the previous vertex `a` is shared by both chunks;
        #   junction cut- the junction vertex `b` ENDS one chunk and STARTS the
        #                 next, which is what makes it a real graph node.  Ending
        #                 a chunk at `b` without also starting the next one there
        #                 leaves `b` as an interior vertex and silently detaches
        #                 every road that joins at it.
        chunks: List[List[List[float]]] = [[coords[0]]]
        run = 0.0
        last_i = len(coords) - 1
        for i, (a, b) in enumerate(zip(coords[:-1], coords[1:]), start=1):
            d = hpe.haversine_m(a[1], a[0], b[1], b[0])
            if run + d > max_length_m and len(chunks[-1]) > 1:
                chunks.append([a])
                run = 0.0
                info["split_count"] += 1
            chunks[-1].append(b)
            run += d
            if f"{b[1]:.6f}|{b[0]:.6f}" in junctions and i < last_i:
                chunks.append([b])
                run = 0.0
                info["split_count"] += 1
                info["junction_splits"] += 1
        for chunk in chunks:
            if len(chunk) < 2:
                continue
            length = hpe.polyline_length_m(chunk)
            if length < min_length_m:
                info["skipped_short"] += 1
                continue
            u = ensure_node(chunk[0][1], chunk[0][0])
            v = ensure_node(chunk[-1][1], chunk[-1][0])
            seg_seq += 1
            sid = f"SEG-{seg_seq:06d}"
            mid = chunk[len(chunk) // 2]
            lat0, lon0 = chunk[0][1], chunk[0][0]     # state lookup only:
            # RoadGraph.add_edge calls resolve_geometry(), which overwrites
            # lat/lon/elevation_m with the length-weighted centroid, so setting
            # them here would be dead code.  Everything downstream must treat
            # e.lat/e.lon as the centroid - which is why the soil lookup below
            # is nearest-neighbour rather than an exact key match.
            elev_mid = mid[2] if len(mid) > 2 else 0.0
            chord = hpe.haversine_m(chunk[0][1], chunk[0][0],
                                    chunk[-1][1], chunk[-1][0])
            sinuosity = round(length / chord, 4) if chord > 1 else 1.0
            e = hpe.RoadEdge(
                segment_id=sid, u=u, v=v,
                coords=[[round(c[0], 6), round(c[1], 6)] for c in chunk],
                length_m=round(length, 2), elev_from=float(elev_mid),
                elev_to=float(elev_mid), slope_deg=0.0,
                highway=name.split()[0] if name and name.split()[0].upper().startswith(("NH", "SH")) else hw,
                road_class=hw, surface=surface, lanes=lanes,
                speed_kmh=round(speed, 1), soil_type="laterite", drainage=0.5,
                ndvi=0.5, cut_slope=1 if cutting else 0,
                sinuosity=sinuosity, state=st or state_of(lat0, lon0),
                district=str(pr.get("district") or ""), name=name,
                hist_freq=0.0)
            e.grade_pct = 0.0
            if embank:
                e.soil_type = "alluvial"
            g.add_edge(e)
            info["edges"] += 1
    g.metadata.pop("_node_keys", None)
    g.metadata.update({"problem_id": "SIH26002", "crs": "EPSG:4326",
                       "source": os.path.basename(path),
                       "generator": f"data_ingestion {__version__}",
                       "seed": DEFAULT_SEED, "n_edges": len(g.edges),
                       "n_nodes": len(g.nodes),
                       "bbox": list(g.bbox())})
    info["osm_tags_used"] = sorted(info["osm_tags_used"])
    _log(f"  road network        : {info['edges']} segments / {len(g.nodes)} nodes "
         f"({info['split_count']} long ways split)", quiet)
    return g, info


# --------------------------------------------------------------------------- #
#  RAINFALL  (IMD / NESAC / NEDFI databank district-wise)
# --------------------------------------------------------------------------- #
@dataclass
class RainfallIndex:
    """date -> {key: mm}, where key is a district name or 'lon,lat' grid cell."""
    by_date: Dict[_dt.date, Dict[str, float]] = field(default_factory=dict)
    fallbacks: int = 0
    key_kind: str = "district"
    source: str = ""
    info: Dict[str, Any] = field(default_factory=dict)

    def mm(self, d: _dt.date, district: str = "", lat: float = 0.0,
           lon: float = 0.0) -> float:
        """Rainfall for a (date, place); grid keys are matched nearest-neighbour.

        A district key absent from the table falls back to that day's mean over
        all reported districts rather than to 0.0 - a silent zero looks exactly
        like a genuine dry day and would quietly poison the training set.  Every
        fallback is counted and surfaced in the provenance report.
        """
        day = self.by_date.get(d)
        if not day:
            return 0.0
        if self.key_kind == "grid":
            best, bd = 0.0, 1e18
            for k, v in day.items():
                glat, glon = (float(x) for x in k.split(","))
                dist = (glat - lat) ** 2 + (glon - lon) ** 2
                if dist < bd:
                    bd, best = dist, v
            return best
        key = district.strip().lower()
        if key and key in day:
            return day[key]
        self.fallbacks += 1
        return sum(day.values()) / len(day)

    def api(self, d: _dt.date, days: int = 3, k: float = 0.85,
            district: str = "", lat: float = 0.0, lon: float = 0.0) -> float:
        """Antecedent Precipitation Index: exponentially decayed trailing sum.

        Day 0 IS the observation date, so `api(d, days=3)` covers d, d-1 and d-2
        with weights 1, k, k^2.  That matches the engine's own convention, where
        api_3d is generated as a multiple of the same day's rain_24h plus an
        antecedent term; excluding today would desynchronise the ingested table
        from anything the engine scored at serve time.
        """
        tot = 0.0
        for i in range(days):
            dd = d - _dt.timedelta(days=i)
            tot += self.mm(dd, district, lat, lon) * (k ** i)
        return round(tot, 1)

    def dates(self) -> List[_dt.date]:
        return sorted(self.by_date)


def load_rainfall(path: str, quiet: bool = False) -> RainfallIndex:
    """Read a district-wise or grid rainfall CSV.

    Accepted shapes (auto-detected):
      long   : district, date, rainfall_mm
      wide   : district, 2023-06-01, 2023-06-02, ...      (dates as headers)
      grid   : lat, lon, date, rainfall_mm
      monthly: district, year, month, rainfall_mm
    """
    header, rows = read_csv_rows(path)
    cmap = guess_columns(header)
    idx = RainfallIndex(source=os.path.basename(path))
    dates_in_header = [h for h in header if parse_date(h) is not None]

    if dates_in_header and (cmap["district"] or cmap["lat"]):
        # WIDE format
        idx.key_kind = "district"
        for r in rows:
            key = str(r.get(cmap["district"], "") or "").strip().lower()
            if not key:
                continue
            for h in dates_in_header:
                v = _to_float(r.get(h))
                if not math.isnan(v):
                    idx.by_date.setdefault(parse_date(h), {})[key] = max(v, 0.0)
    elif cmap["date"] and (cmap["district"] or cmap["lat"]):
        # LONG / GRID format
        grid = bool(cmap["lat"] and cmap["lon"] and not cmap["district"])
        idx.key_kind = "grid" if grid else "district"
        for r in rows:
            d = parse_date(r.get(cmap["date"]))
            if d is None:
                continue
            v = _to_float(r.get(cmap["rainfall"]), 0.0)
            if math.isnan(v):
                v = 0.0
            if grid:
                key = f"{_to_float(r.get(cmap['lat'])):.3f}," \
                      f"{_to_float(r.get(cmap['lon'])):.3f}"
            else:
                key = str(r.get(cmap["district"], "") or "").strip().lower()
                if not key:
                    continue
            bucket = idx.by_date.setdefault(d, {})
            bucket[key] = bucket.get(key, 0.0) + max(v, 0.0)
    else:
        raise ValueError(
            f"{path}: unrecognised rainfall layout. Expected either "
            f"'district,date,rainfall_mm' rows, dates-as-columns with a district "
            f"column, or 'lat,lon,date,rainfall_mm'. Header was: {header}")

    idx.info = {"path": path, "rows_read": len(rows), "format": idx.key_kind,
                "n_dates": len(idx.by_date), "column_map":
                {k: v for k, v in cmap.items() if v}}
    if idx.by_date:
        ds = idx.dates()
        idx.info["date_range"] = [ds[0].isoformat(), ds[-1].isoformat()]
        idx.info["total_mm"] = round(sum(sum(v.values())
                                         for v in idx.by_date.values()), 1)
        idx.info["keys"] = len({k for v in idx.by_date.values() for k in v})
        idx.info["district_fallbacks"] = idx.fallbacks
    _log(f"  rainfall            : {idx.info['n_dates']} days x "
         f"{idx.info.get('keys', 0)} {idx.key_kind} keys "
         f"({idx.info.get('total_mm', 0)} mm total)", quiet)
    return idx


def load_moisture(path: Optional[str], quiet: bool = False
                  ) -> Tuple[Optional[RainfallIndex], Dict[str, Any]]:
    """Optional live soil-moisture table (ERA5-Land `volumetric_soil_water_layer_1`
    or SMAP L4 surface moisture), exported as CSV: date, lat, lon, sm_m3m3.

    Returns a RainfallIndex-like object whose 'mm' accessor yields volumetric
    water content in m3/m3 (0-1), so the same lookup code serves both.
    """
    info: Dict[str, Any] = {"source": None}
    if not path:
        return None, info
    header, rows = read_csv_rows(path)
    cmap = guess_columns(header)
    sm_col = None
    tokens = ("soilmoisture", "volumetricsoilwater", "vswl", "smm3m3",
              "surfacestoragesoil", "theta", "ssm", "smsurface", "soilwater")
    for h in header:
        nk = _norm_key(h)
        if any(t in nk for t in tokens):
            sm_col = h
            break
    if sm_col is None:
        for h in header:                      # last resort: a bare "sm" column
            if _norm_key(h) == "sm":
                sm_col = h
                break
    if not (cmap["date"] and cmap["lat"] and cmap["lon"] and sm_col):
        raise ValueError(f"{path}: moisture CSV needs date,lat,lon,<sm column>; "
                         f"got {header}")
    mi = RainfallIndex(source=os.path.basename(path), key_kind="grid")
    for r in rows:
        d = parse_date(r.get(cmap["date"]))
        if d is None:
            continue
        v = _to_float(r.get(sm_col))
        if math.isnan(v):
            continue
        if v > 1.5:                       # some products report vol% not m3/m3
            v /= 100.0
        key = f"{_to_float(r.get(cmap['lat'])):.3f},{_to_float(r.get(cmap['lon'])):.3f}"
        mi.by_date.setdefault(d, {})[key] = v
    info = {"source": os.path.basename(path), "rows_read": len(rows),
            "n_dates": len(mi.by_date), "sm_column": sm_col}
    _log(f"  soil moisture       : {info['n_dates']} days from {info['source']} "
         f"(column '{sm_col}')", quiet)
    return mi, info


# --------------------------------------------------------------------------- #
#  SOIL SATURATION: bucket model parameterised by SoilGrids retention points
# --------------------------------------------------------------------------- #
class SoilBucket:
    """One-layer rooting-zone water balance driven by daily rainfall.

        theta(t) = theta(t-1) + rain/Z - drainage(theta) - ET(theta)

    `theta` is a VOLUMETRIC water content in %, so rainfall in mm is divided by
    the rooting depth `Z` (also expressed in mm of soil) before being added:
    30 mm of rain over a 300 mm root zone raises theta by 10 vol%.  Adding
    millimetres straight to a percentage - the usual shortcut - saturates the
    soil on any heavy day and throws away the dynamic range the classifier needs.

    Drainage begins above field capacity and scales with the soil's drainage
    proxy; ET scales with how wet the soil already is.  The saturation reported
    to the model is the degree of saturation

        (theta - wilting_point) / (porosity - wilting_point)

    which is exactly the scale `saturation_from_theta` uses for observed
    ERA5-Land / SMAP moisture.  Both paths must agree: a segment whose moisture
    comes from the bucket on days the satellite is missing and from the satellite
    otherwise would feed the model two different definitions of one feature.
    """

    def __init__(self, fc_pct: float = 32.0, wp_pct: float = 13.0,
                 porosity_pct: float = 48.0, drainage: float = 0.5,
                 init_frac: float = 0.35, et_mm_day: float = 2.5,
                 root_depth_mm: float = 300.0):
        self.fc = fc_pct
        self.wp = wp_pct
        self.porosity = max(porosity_pct, wp_pct + 1.0)
        self.drainage = drainage
        self.et = et_mm_day
        self.root_depth_mm = max(root_depth_mm, 1.0)
        self.awc = max(fc_pct - wp_pct, 1.0)       # plant-available water
        self.span = self.porosity - wp_pct         # saturation denominator
        self.theta = wp_pct + init_frac * self.span

    @property
    def saturation(self) -> float:
        return hpe.clamp((self.theta - self.wp) / self.span, 0.0, 1.0)

    def step(self, rain_mm: float, days: int = 1) -> float:
        """Advance `days` days of `rain_mm` each; return the saturation fraction."""
        n = max(int(days), 1)
        for _ in range(n):
            self.theta += 100.0 * max(rain_mm, 0.0) / self.root_depth_mm
            excess = max(self.theta - self.fc, 0.0)
            self.theta -= excess * hpe.clamp(0.25 + 0.6 * self.drainage, 0.0, 1.0)
            sat = self.saturation
            self.theta -= 100.0 * self.et * (0.35 + 0.65 * sat) / self.root_depth_mm
            self.theta = hpe.clamp(self.theta, self.wp * 0.6, self.porosity)
        return self.saturation


def porosity_from_bdod(bdod_g_cm3: Optional[float]) -> float:
    """Saturated water content from bulk density: n = 1 - bd/2.65, where 2.65
    g/cm3 is the particle density of quartz-dominated mineral soil."""
    if not bdod_g_cm3 or bdod_g_cm3 <= 0.2:
        return 48.0
    return hpe.clamp(100.0 * (1.0 - bdod_g_cm3 / 2.65), 25.0, 75.0)


def saturation_from_theta(theta_m3m3: float, wp_pct: float,
                          porosity_pct: float) -> float:
    """Degree of saturation: (theta - theta_wp) / (theta_s - theta_wp).

    Porosity, not field capacity, is the upper bound.  Against field capacity a
    monsoon soil sits at or above 1.0 almost always and the feature collapses to
    a constant; against porosity it keeps real dynamic range.
    """
    theta_pct = theta_m3m3 * 100.0
    span = max(porosity_pct - wp_pct, 1.0)
    return hpe.clamp((theta_pct - wp_pct) / span, 0.0, 1.0)


# --------------------------------------------------------------------------- #
#  SPATIAL JOIN: events -> road segments
# --------------------------------------------------------------------------- #
def _grid_index(items: Sequence[Tuple[float, float]], cell_deg: float = 0.05
                ) -> Dict[Tuple[int, int], List[int]]:
    idx: Dict[Tuple[int, int], List[int]] = {}
    for i, (lat, lon) in enumerate(items):
        idx.setdefault((int(lat / cell_deg), int(lon / cell_deg)), []).append(i)
    return idx


def join_events_to_segments(events: Sequence[Event], graph: hpe.RoadGraph,
                            buffer_m: float = 100.0,
                            quiet: bool = False
                            ) -> Tuple[Dict[str, List[Tuple[int, float]]], Dict[str, Any]]:
    """Attach each event to every segment whose polyline passes within buffer_m.

    Uses a coarse lat/lon bucket grid so cost is ~O(n) rather than O(n*m).
    Returns {segment_id: [(event_index, distance_m), ...]}.
    """
    # index EVERY vertex of every polyline: a 12 km segment whose first node is
    # far from an event can still pass within metres of it
    cell_deg = 0.05
    grid: Dict[Tuple[int, int], set] = {}
    for si, e in enumerate(graph.edges):
        for c in e.coords:
            grid.setdefault((int(c[1] / cell_deg), int(c[0] / cell_deg)),
                            set()).add(si)
    reach = max(buffer_m, 1.0) / 111000.0
    span = int(math.ceil(reach / cell_deg)) + 1
    matches: Dict[str, List[Tuple[int, float]]] = {}
    for ei, ev in enumerate(events):
        elat, elon = ev.lat, ev.lon
        r0, c0 = int(elat / cell_deg), int(elon / cell_deg)
        cand: set = set()
        for dr in range(-span, span + 1):
            for dc in range(-span, span + 1):
                cand |= grid.get((r0 + dr, c0 + dc), set())
        for si in cand:
            e = graph.edges[si]
            d = _point_to_polyline_m(elat, elon, e.coords)
            if d <= buffer_m:
                matches.setdefault(e.segment_id, []).append((ei, round(d, 1)))
    info = {"buffer_m": buffer_m, "events_matched":
            len({ei for v in matches.values() for ei, _d in v}),
            "events_total": len(events), "segments_hit": len(matches),
            "closest_m": (min(d for v in matches.values() for _ei, d in v)
                          if matches else None)}
    info["match_rate_pct"] = round(100.0 * info["events_matched"]
                                   / max(info["events_total"], 1), 1)
    _log(f"  spatial join          : {info['events_matched']}/{len(events)} events "
         f"within {buffer_m:.0f} m of {info['segments_hit']} segments "
         f"({info['match_rate_pct']}%)", quiet)
    return matches, info


def _point_to_polyline_m(plat: float, plon: float,
                         coords: Sequence[Sequence[float]]) -> float:
    """Distance from a point to the nearest vertex-or-segment of a polyline."""
    best = float("inf")
    pts = [(c[1], c[0]) for c in coords]
    for i, (lat, lon) in enumerate(pts):
        d = hpe.haversine_m(plat, plon, lat, lon)
        best = min(best, d)
        if i + 1 < len(pts):
            best = min(best, _point_to_segment_m(plat, plon, lat, lon,
                                                 pts[i + 1][0], pts[i + 1][1]))
    return best


def _point_to_segment_m(plat: float, plon: float, alat: float, alon: float,
                        blat: float, blon: float) -> float:
    """Cheap planar projection (fine at these scales), then true distance."""
    ax, ay = alon, alat * math.cos(math.radians(plat))
    bx, by = blon, blat * math.cos(math.radians(plat))
    px, py = plon, plat * math.cos(math.radians(plat))
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        t = 0.0
    else:
        t = hpe.clamp(((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy), 0, 1)
    return hpe.haversine_m(plat, plon, alat + t * (blat - alat),
                           alon + t * (blon - alon))


# --------------------------------------------------------------------------- #
#  FEATURE ENRICHMENT
# --------------------------------------------------------------------------- #
def classify_slope_raster(raster: DemMosaic, samples: int = 24) -> Dict[str, Any]:
    """Decide whether a slope product is in degrees or percent gradient.

    Both are in circulation and they are not distinguishable from the filename:
    GDAL's `gdaldem slope` defaults to degrees but `-p` gives percent, QGIS's
    raster terrain analysis offers both, and ASTER-derived products vary by
    agency.  Reading percent as degrees inflates every slope roughly tenfold and
    the model still trains happily on it - it just learns a feature nobody can
    interpret, and any threshold calibrated in degrees (the `slope_deg >= 12`
    cut-slope rule) fires everywhere.  Slope in degrees cannot exceed 90, so
    anything above that is percent.
    """
    w, s_, e, nn = raster.bbox()
    vals: List[float] = []
    for i in range(samples):
        for j in range(samples):
            v = raster.elevation(s_ + (nn - s_) * (i + 0.5) / samples,
                                 w + (e - w) * (j + 0.5) / samples)
            if v is not None:
                vals.append(float(v))
    if not vals:
        return {"samples": 0, "unit": "degrees", "min": None, "max": None,
                "mean": None, "note": "raster returned no readable values"}
    vs = sorted(vals)
    vmax, vmin = vs[-1], vs[0]
    mean = sum(vs) / len(vs)
    p99 = vs[min(int(0.99 * len(vs)), len(vs) - 1)]
    unit = "percent" if max(vmax, p99) > 90.0 else "degrees"
    out: Dict[str, Any] = {"samples": len(vs), "unit": unit,
                           "min": round(vmin, 2), "max": round(vmax, 2),
                           "mean": round(mean, 2), "p99": round(p99, 2)}
    if vmin < -1.0:
        out["note"] = (f"minimum {vmin} looks like a nodata sentinel, not a "
                       f"slope; those cells are skipped per sample, but check "
                       f"the product's nodata value")
    return out


def enrich_graph_with_dem(graph: hpe.RoadGraph, dem: Optional[DemMosaic],
                          ndvi: Optional[DemMosaic] = None,
                          slope_raster: Optional[DemMosaic] = None,
                          quiet: bool = False) -> Dict[str, Any]:
    """Sample elevation, effective slope, relief, cut-slope and NDVI per segment.

    `slope_raster` is an optional PRE-COMPUTED slope surface (GDAL/QGIS/ASTER
    derivative) in degrees or percent.  When supplied it takes precedence over
    slope derived from the DEM here, because an agency slope product was usually
    computed on a hydrologically conditioned DEM at full resolution, whereas
    deriving it in-process resamples the same DEM at the road's own vertices.
    """
    slope_pct = False
    slope_raster_info: Dict[str, Any] = {}
    if slope_raster is not None:
        slope_raster_info = classify_slope_raster(slope_raster)
        slope_pct = slope_raster_info.get("unit") == "percent"
        _log(f"  slope raster          : {slope_raster_info.get('samples')} "
             f"samples, range {slope_raster_info.get('min')}.."
             f"{slope_raster_info.get('max')} -> read as "
             f"{slope_raster_info.get('unit')}", quiet)
    info = {"dem_hits": 0, "dem_misses": 0, "ndvi_hits": 0,
            "slope_deg_mean": None, "slope_deg_p90": None,
            "cut_slope_pct": None, "relief_mean_m": None,
            "slope_raster_hits": 0, "slope_raster_segments": 0,
            "slope_raster": slope_raster_info or None}
    slopes: List[float] = []
    cut = 0
    reliefs: List[float] = []
    for e in graph.edges:
        lat, lon = e.lat, e.lon
        # sample along the polyline, not just at the centroid
        pts = [(c[1], c[0]) for c in e.coords]
        if len(pts) < 2:
            pts = [(lat, lon)]
        elevs, sl, rel = [], [], []
        for plat, plon in pts:
            if slope_raster is not None:
                sv = slope_raster.elevation(plat, plon)
                if sv is not None and float(sv) >= 0.0:
                    # percent gradient converts through atan, it is not scaled:
                    # 100% is 45 deg, so dividing by 100 would be wrong by 45x
                    sl.append(math.degrees(math.atan(float(sv) / 100.0))
                              if slope_pct else float(sv))
                    info["slope_raster_hits"] += 1
            if dem is not None:
                hv = dem.elevation(plat, plon)
                rv = dem.relief_m(plat, plon, radius_px=3)
                if hv is not None:
                    elevs.append(hv)
                if rv is not None:
                    rel.append(rv)
                if slope_raster is None:
                    sv = dem.slope_deg(plat, plon)
                    if sv is not None:
                        sl.append(sv)
        if elevs:
            info["dem_hits"] += 1
            e.elev_from = round(elevs[0], 1)
            e.elev_to = round(elevs[-1], 1)
            e.elevation_m = round(sum(elevs) / len(elevs), 1)
            e.grade_pct = round(100.0 * abs(elevs[-1] - elevs[0])
                                / max(e.length_m, 1.0), 3)
            if rel:
                e.relief_m = round(sum(rel) / len(rel), 1)
                reliefs.append(e.relief_m)
        else:
            info["dem_misses"] += 1
        # effective slope: 85th percentile of the sampled terrain gradient,
        # which captures the steepest bit a convoy must actually cross.  A
        # --slope-tif product supplies this on its own - no DEM required - so
        # the three cases are kept separate rather than nested under `elevs`.
        if sl:
            sl_sorted = sorted(sl)
            e.slope_deg = round(sl_sorted[min(int(0.85 * len(sl_sorted)),
                                              len(sl_sorted) - 1)], 2)
            slopes.append(e.slope_deg)
            if slope_raster is not None:
                info["slope_raster_segments"] += 1
        elif elevs:
            rise = abs(elevs[-1] - elevs[0])
            e.slope_deg = round(math.degrees(math.atan(
                rise / max(e.length_m, 1.0))), 2)
            slopes.append(e.slope_deg)
        elif not e.slope_deg:
            e.slope_deg = round(math.degrees(math.atan(
                abs(e.elev_to - e.elev_from) / max(e.length_m, 1.0))), 2)
            slopes.append(e.slope_deg)
        # a cut face is likely where the road traverses steep ground with
        # high local relief, or where OSM already says cutting=yes.  Relief
        # needs a DEM, so this stays gated on one.
        if elevs and not e.cut_slope:
            if e.slope_deg >= 12.0 and e.relief_m >= 25.0:
                e.cut_slope = 1
                cut += 1
        if ndvi is not None:
            n = ndvi.elevation(lat, lon)
            if n is not None:
                # NDVI rasters are often scaled x100 or x1000
                if n > 10:
                    n /= 100.0
                if n > 1.0:
                    n /= 10.0
                e.ndvi = round(hpe.clamp(n, 0.0, 1.0), 3)
                info["ndvi_hits"] += 1
    if slopes:
        ss = sorted(slopes)
        info["slope_deg_mean"] = round(sum(ss) / len(ss), 2)
        info["slope_deg_p90"] = round(ss[min(int(0.9 * len(ss)), len(ss) - 1)], 2)
    if reliefs:
        info["relief_mean_m"] = round(sum(reliefs) / len(reliefs), 1)
    info["cut_slope_pct"] = round(100.0 * sum(e.cut_slope for e in graph.edges)
                                  / max(len(graph.edges), 1), 1)
    _log(f"  terrain enrichment    : {info['dem_hits']}/{len(graph.edges)} segments "
         f"hit the DEM, mean slope {info['slope_deg_mean']} deg "
         f"(p90 {info['slope_deg_p90']}), cuts {info['cut_slope_pct']}%"
         + (f"; slope from --slope-tif on {info['slope_raster_segments']}/"
            f"{len(graph.edges)} segments" if slope_raster is not None else ""),
         quiet)
    if slope_raster is not None and not info["slope_raster_hits"]:
        _warn("  ! the --slope-tif raster covered NONE of the segments, so "
              "slope fell back to the DEM or to segment endpoints. Check that "
              "it is in EPSG:4326 and overlaps the road network - a UTM slope "
              "product will silently miss every point.", quiet)
    if slope_raster is not None and dem is None:
        _warn("  ! --slope-tif without --dem: elevation_m, relief_m, grade_pct "
              "and cut_slope stay IMPUTED. Add --dem for the full terrain set.",
              quiet)
    return info


def attach_city_names(graph: hpe.RoadGraph, radius_km: float = 10.0,
                      cities: Optional[Dict[str, Any]] = None,
                      quiet: bool = False) -> Dict[str, Any]:
    """Snap known NER city anchors onto the nearest junction and name it.

    OSM road *ways* carry no place names, so on an ingested network
    `RoadGraph.resolve_endpoint("Guwahati")` finds nothing and every named
    corridor reports NO PATH even when the road is there.  `resolve_endpoint`
    tries node id, then node name, then "lat,lon" - it does not consult the
    engine's own city table.  Naming the junctions closes that gap without
    inventing geometry: the node stays exactly where the road put it, and only
    cities whose nearest junction is within `radius_km` are attached, so a road
    network covering one district cannot silently claim cities it never reached.
    """
    cities = cities if cities is not None else getattr(hpe, "NER_CITIES", {})
    attached: Dict[str, Any] = {}
    out_of_range: List[str] = []
    limit_m = radius_km * 1000.0
    for name, anchor in cities.items():
        clat, clon, celev, cstate = anchor[0], anchor[1], anchor[2], anchor[3]
        best, bd = None, limit_m
        for nid, nd in graph.nodes.items():
            d = hpe.haversine_m(clat, clon, nd["lat"], nd["lon"])
            if d < bd:
                bd, best = d, nid
        if best is None:
            out_of_range.append(name)
            continue
        nd = graph.nodes[best]
        nd["name"] = name
        nd["state"] = nd.get("state") or cstate
        if not nd.get("elev_m"):
            nd["elev_m"] = celev
        attached[name] = {"node": best, "snap_m": round(bd, 1)}
    info = {"cities_attached": len(attached), "cities_total": len(cities),
            "radius_km": radius_km, "attachment": attached,
            "cities_out_of_range": out_of_range}
    _log(f"  city anchors          : {len(attached)}/{len(cities)} named "
         f"(snap radius {radius_km:g} km"
         f"{', max snap ' + str(round(max(v['snap_m'] for v in attached.values()), 1)) + ' m' if attached else ''}"
         f")", quiet)
    if out_of_range and quiet is False:
        _log(f"    not reachable from this network: {', '.join(sorted(out_of_range)[:8])}"
             f"{' ...' if len(out_of_range) > 8 else ''}", quiet)
    return info


def enrich_graph_with_soil(graph: hpe.RoadGraph, soil: Optional[SoilGridsClient],
                           local_csv: Optional[str] = None,
                           quiet: bool = False) -> Dict[str, Any]:
    """Attach soil_type / drainage / retention points per segment centroid."""
    local: Dict[str, Dict[str, float]] = {}
    if local_csv and os.path.exists(local_csv):
        header, rows = read_csv_rows(local_csv)
        cm = guess_columns(header)
        for r in rows:
            k = SoilGridsClient.key(_to_float(r.get(cm["lat"], 0)),
                                    _to_float(r.get(cm["lon"], 0)))
            local[k] = {h: _to_float(r.get(h)) for h in header
                        if _norm_key(h) in SOILGRIDS_DIVISOR}
    info = {"soilgrids_points": 0, "local_csv_points": len(local),
            "imputed": 0, "soil_type_mix": {}, "drainage_mean": None}
    drains: List[float] = []
    retention: Dict[str, Dict[str, Optional[float]]] = {}
    for e in graph.edges:
        k = SoilGridsClient.key(e.lat, e.lon)
        props = local.get(k)
        if props is None and soil is not None:
            props = soil.query(e.lat, e.lon)
            if props:
                info["soilgrids_points"] += 1
        if props is None:
            info["imputed"] += 1
        feats = derive_soil_features(props)
        e.soil_type = feats["soil_type"]
        e.drainage = feats["drainage"]
        drains.append(feats["drainage"])
        info["soil_type_mix"][feats["soil_type"]] = \
            info["soil_type_mix"].get(feats["soil_type"], 0) + 1
        retention[e.segment_id] = {"fc": feats["field_capacity_pct"],
                                   "wp": feats["wilting_point_pct"],
                                   "porosity": feats["porosity_pct"],
                                   "drainage": feats["drainage"]}
    if drains:
        info["drainage_mean"] = round(sum(drains) / len(drains), 3)
    info["retention"] = retention
    if soil is not None:
        info.update(soil.stats())
    _log(f"  soil properties       : {info['soilgrids_points']} live/cached points, "
         f"{info['imputed']} imputed | drainage {info['drainage_mean']} "
         f"(0=pores 1=bedrock) | "
         f"{', '.join(f'{k}:{v}' for k, v in sorted(info['soil_type_mix'].items()))}",
         quiet)
    return info


# --------------------------------------------------------------------------- #
#  FUSION -> training rows
# --------------------------------------------------------------------------- #
def monsoon_season(d: _dt.date) -> str:
    return ("pre_monsoon" if d.month in (3, 4, 5) else
            "onset" if d.month == 6 else
            "peak" if d.month in (7, 8) else
            "late" if d.month in (9, 10) else "post")


def season_of_year(year: int) -> Tuple[_dt.date, _dt.date]:
    return _dt.date(year, 6, 1), _dt.date(year, 10, 15)


def build_training_rows(graph: hpe.RoadGraph,
                        events: Sequence[Event],
                        matches: Dict[str, List[Tuple[int, float]]],
                        rain: Optional[RainfallIndex],
                        moisture: Optional[RainfallIndex],
                        retention: Dict[str, Dict[str, Any]],
                        year: int = 2023,
                        obs_per_segment: int = 4,
                        positive_window_days: int = 3,
                        max_positives_per_segment: int = 6,
                        seed: int = DEFAULT_SEED,
                        quiet: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fuse every source into one row per (segment, observation date).

    CASE-CONTROL DESIGN.  Sampling dates and then hoping an event lands inside
    the window produces almost no positives.  Instead:

      * a POSITIVE row is created *at* each matched event date, so the weather
        used is the weather that actually accompanied the failure;
      * NEGATIVE rows are sampled from the season, excluding any date within
        `positive_window_days` of a known event on that segment;
      * negative dates preferentially come from wet days, which are the
        informative ones (a dry day with no landslide teaches the model little).

    LEAKAGE CONTROL.  `hist_freq_per_km` counts only events strictly BEFORE the
    row's date, normalised by segment length and by the number of seasons of
    inventory available at that point.  The usual
    `"hist_freq": len(events_on_segment)` sketch leaks the label directly.
    Undated events cannot be placed in time, so they are attributed to the
    start of the inventory period - conservative, and counted separately.
    """
    rng = random.Random(seed ^ 0xDA7A)
    s0, s1 = season_of_year(year)
    rows: List[Dict[str, Any]] = []

    dated_all = sorted({e.date for e in events if e.date})
    inv_start = dated_all[0] if dated_all else s0
    seasons_available = max((s1 - inv_start).days / 122.0, 1.0)

    # per-segment event bookkeeping
    ev_by_seg: Dict[str, List[int]] = {}
    for sid, lst in matches.items():
        ev_by_seg[sid] = sorted({ei for ei, _d in lst})

    info: Dict[str, Any] = {
        "year": year, "design": "case-control (positives at event dates)",
        "obs_per_segment": obs_per_segment,
        "positive_window_days": positive_window_days,
        "max_positives_per_segment": max_positives_per_segment,
        "season": [s0.isoformat(), s1.isoformat()],
        "inventory_span_seasons": round(seasons_available, 2),
        "undated_events": sum(1 for e in events if e.date is None),
        "rows": 0, "positive": 0, "negative": 0, "positive_rate": 0.0,
        "rainfall_days_in_season": 0, "rainfall_climatology_used": False,
        "district_fallbacks": 0, "hist_freq_source":
        "expanding-window inventory, strictly before the observation date",
    }

    rain_dates = rain.dates() if rain else []
    in_season = [d for d in rain_dates if s0 <= d <= s1]
    info["rainfall_days_in_season"] = len(in_season)
    if rain is None or not in_season:
        # either no rainfall source was supplied at all, or it covers a different
        # period than the inventory - both fall back to monsoon climatology
        info["rainfall_climatology_used"] = True
        if rain is None:
            _log(f"  ! no rainfall source given; using monsoon climatology for "
                 f"{s0}..{s1} instead of observed IMD/NESAC values", quiet)
        elif not rain_dates:
            _log("  ! the rainfall table parsed but held no usable dates; using "
                 "monsoon climatology instead", quiet)
        else:
            _log(f"  ! rainfall covers {rain_dates[0]}..{rain_dates[-1]}, which "
                 f"does not overlap the {s0}..{s1} season; using monsoon "
                 f"climatology instead", quiet)

    season_days = [(s0 + _dt.timedelta(days=i))
                   for i in range((s1 - s0).days + 1)]
    pool = in_season or season_days

    for e in graph.edges:
        ret = retention.get(e.segment_id, {}) or {}
        fc = ret.get("fc") or 32.0
        wp = ret.get("wp") or 13.0
        por = ret.get("porosity") or 48.0
        bucket = SoilBucket(fc_pct=fc, wp_pct=wp, porosity_pct=por,
                            drainage=e.drainage)
        km = max(e.length_m / 1000.0, 0.05)
        district = (e.district or "").strip().lower()
        seg_ev = ev_by_seg.get(e.segment_id, [])

        # events that label THIS segment, split dated / undated
        pos_dates = sorted({events[ei].date for ei in seg_ev
                            if events[ei].date is not None})[:max_positives_per_segment]
        undated = [ei for ei in seg_ev if events[ei].date is None]
        # undated events are real history: pin them to the inventory start so
        # they raise hist_freq for every in-season observation without labelling
        hist_anchor = [(inv_start, ei) for ei in undated] + \
                      [(events[ei].date, ei) for ei in seg_ev
                       if events[ei].date is not None]
        hist_anchor = sorted((d, ei) for d, ei in hist_anchor if d)

        # wet-day-ordered candidate dates for the negatives
        if rain is not None and in_season:
            ranked = sorted(in_season,
                            key=lambda d: -rain.mm(d, district, e.lat, e.lon))
            wet = ranked[:max(1, obs_per_segment)]
            rest = [d for d in in_season if d not in wet]
        else:
            wet, rest = [], list(pool)
        rng.shuffle(rest)

        def blocked(d: _dt.date) -> bool:
            return any(abs((d - pd).days) <= positive_window_days
                       for pd in pos_dates)

        neg_dates = [d for d in (wet + rest) if not blocked(d)]
        neg_dates = sorted(set(neg_dates))[:max(obs_per_segment, 1)]

        for d, lab in ([(pd, 1) for pd in pos_dates]
                       + [(nd, 0) for nd in neg_dates]):
            # ---- weather at (segment, date) ------------------------------
            if rain is not None and (rain.mm(d, district, e.lat, e.lon) > 0
                                     or in_season):
                r24 = rain.mm(d, district, e.lat, e.lon)
                api = rain.api(d, days=3, k=0.85, district=district,
                               lat=e.lat, lon=e.lon)
                rain_hr = round(r24 / 24.0 * 3.2, 2)
            else:
                base = {"onset": 12.0, "peak": 22.0, "late": 14.0,
                        "pre_monsoon": 6.0, "post": 2.0}[monsoon_season(d)]
                r24 = round(base * rng.lognormvariate(0, 0.9), 1)
                api = round(r24 * 2.4 * rng.uniform(0.7, 1.4), 1)
                rain_hr = round(r24 / 24.0 * 3.2, 2)
            if moisture is not None:
                theta = moisture.mm(d, "", e.lat, e.lon)
                sat = (saturation_from_theta(theta, wp, por) if theta > 0
                       else bucket.step(r24))
            else:
                sat = bucket.step(r24)

            # ---- label payload -------------------------------------------
            if lab:
                near = [ei for ei in seg_ev if events[ei].date == d] or seg_ev
                ev = events[near[0]]
                htype = ev.hazard_type
                sev = {"low": 0.7, "moderate": 1.0, "high": 1.5,
                       "severe": 2.2}.get(ev.severity.strip().lower(), 1.0)
                closure = round(max(2.0, rng.gammavariate(2.1, 6.0) * sev
                                    * (1.0 + e.slope_deg / 22.0)), 1)
                debris = round(rng.lognormvariate(3.4, 1.1) * sev
                               if htype in ("landslide", "debris_flow",
                                            "slope_failure", "boulder_fall",
                                            "road_collapse") else 0.0, 1)
                src = ev.source_id or "GSI_Bhukosh"
                notes = (f"{ev.hazard_type} reported {d.isoformat()} within "
                         f"{info.get('buffer_m', 100)} m of this segment")
            else:
                htype, closure, debris = "none", 0.0, 0.0
                src = "imputed_negative"
                notes = (f"{monsoon_season(d)} observation, no inventory event "
                         f"within +/-{positive_window_days} d")

            # ---- leakage-safe expanding-window history --------------------
            prior = [1 for (ed, _ei) in hist_anchor if ed < d]
            span_seasons = max((d - inv_start).days / 122.0, 0.5)
            hist_freq = round(len(prior) / km / span_seasons, 4)

            rows.append({
                "record_id": f"R-{len(rows):07d}",
                "timestamp": f"{d.isoformat()}T12:00:00+05:30",
                "season_phase": monsoon_season(d),
                "segment_id": e.segment_id,
                "state": e.state, "district": e.district or "",
                "highway": e.highway, "lat": e.lat, "lon": e.lon,
                "elevation_m": round(e.elevation_m, 1),
                "length_m": e.length_m, "slope_deg": e.slope_deg,
                "soil_type": e.soil_type, "drainage": e.drainage,
                "ndvi": e.ndvi, "cut_slope": e.cut_slope,
                "sinuosity": e.sinuosity, "surface": e.surface,
                "rain_mm_hr": rain_hr, "rain_24h_mm": round(r24, 1),
                "api_3d_mm": api, "soil_saturation": round(sat, 4),
                "hist_freq_per_km": hist_freq,
                "_p_true": None,
                "disrupted": lab, "hazard_type": htype,
                "closure_hours": closure, "debris_tonnes": debris,
                "source": src, "notes": notes,
            })

    info["district_fallbacks"] = getattr(rain, "fallbacks", 0) if rain else 0
    info["rows"] = len(rows)
    info["positive"] = sum(r["disrupted"] for r in rows)
    info["negative"] = info["rows"] - info["positive"]
    info["positive_rate"] = round(info["positive"] / max(len(rows), 1), 4)
    _log(f"  fused training rows   : {info['rows']} rows = {info['positive']} "
         f"positive + {info['negative']} negative "
         f"({100 * info['positive_rate']:.1f}% positive)", quiet)
    if info["positive"] == 0:
        _log("  ! WARNING: no positive labels survived the join. Likely causes: "
             "inventory dates outside the season, --buffer too small for the "
             "positional accuracy of the inventory, or roads and inventory "
             "covering different areas.", quiet)
    if info["district_fallbacks"]:
        _log(f"  ! {info['district_fallbacks']} rainfall lookups used the daily "
             f"all-district mean because the segment's district name was not in "
             f"the rainfall table - check spelling or pass district on the roads",
             quiet)
    return rows, info


def provenance_report(dem_info: Optional[Dict[str, Any]],
                      soil_info: Dict[str, Any],
                      rain_idx: Optional[RainfallIndex],
                      moist_info: Dict[str, Any],
                      ev_info: Dict[str, Any],
                      road_info: Dict[str, Any],
                      join_info: Dict[str, Any],
                      fuse_info: Dict[str, Any],
                      city_info: Optional[Dict[str, Any]] = None,
                      fetch_info: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, Any]:
    """Column-by-column: where each model feature actually came from."""
    rain_ok = bool(rain_idx and rain_idx.by_date)
    dem_ok = bool(dem_info and dem_info.get("dem_hits", 0) > 0)
    soil_ok = (soil_info.get("soilgrids_points", 0)
               + soil_info.get("local_csv_points", 0)) > 0
    moist_ok = bool(moist_info.get("source"))
    cols = {
        "rain_mm_hr":       ("IMD/NESAC/NEDFI rainfall" if rain_ok
                             else "IMPUTED monsoon climatology", rain_ok),
        "rain_24h_mm":      ("IMD/NESAC/NEDFI rainfall" if rain_ok
                             else "IMPUTED monsoon climatology", rain_ok),
        "api_3d_mm":        ("derived: 3-day exponentially decayed rainfall sum (k=0.85)"
                             if rain_ok else "IMPUTED from climatology", rain_ok),
        "soil_saturation":  (f"observed moisture ({moist_info['source']}) scaled by "
                             f"SoilGrids field capacity/wilting point" if moist_ok
                             else ("bucket model driven by rainfall, parameterised by "
                                   "SoilGrids wv0033/wv1500" if soil_ok and rain_ok
                                   else "IMPUTED bucket model with texture defaults")),
        "slope_deg":        (("pre-computed slope raster (--slope-tif), read as "
                              + str(((dem_info or {}).get("slope_raster")
                                     or {}).get("unit")))
                             if (dem_info or {}).get("slope_raster_hits")
                             else ("DEM (Horn 3x3, p85 along segment)" if dem_ok
                                   else "IMPUTED from segment endpoints"),
                             bool((dem_info or {}).get("slope_raster_hits")) or dem_ok),
        "elevation_m":      ("DEM bilinear sample" if dem_ok else "IMPUTED 0", dem_ok),
        "relief_m":         ("DEM peak-to-valley in 3px window" if dem_ok
                             else "IMPUTED 0", dem_ok),
        "cut_slope":        ("OSM cutting=yes tag, else inferred from slope>=12 & relief>=25",
                             True),
        "soil_type":        ("SoilGrids texture triangle" if soil_ok
                             else "IMPUTED laterite", soil_ok),
        "drainage":         ("DERIVED proxy from SoilGrids texture+bdod+retention"
                             if soil_ok else "IMPUTED 0.5", soil_ok),
        "ndvi":             ("NDVI raster" if dem_info and dem_info.get("ndvi_hits")
                             else "IMPUTED 0.5", bool(dem_info and dem_info.get("ndvi_hits"))),
        "hist_freq_per_km": ("expanding-window GSI/NGDR inventory (events/km/season, "
                             "strictly before the observation date)"
                             if ev_info.get("kept") else "IMPUTED 0",
                             bool(ev_info.get("kept"))),
        "disrupted":        (f"label: {fuse_info.get('positive', 0)} inventory "
                             f"events matched to segments and used as positives"
                             if fuse_info.get("positive")
                             else "NO POSITIVE LABELS - every row is negative; "
                                  "widen --buffer or check inventory dates",
                             bool(fuse_info.get("positive"))),
        "sinuosity":        ("derived from polyline vs chord length", True),
        "length_m":         ("OSM geometry", True),
        "state":            ("OSM tag, else NER bounding-box lookup", True),
    }
    return {
        "generated_utc": hpe.utcnow_iso(),
        "ingestion_version": __version__,
        "column_provenance": {k: {"source": v[0], "real_data": bool(v[1])}
                              for k, v in cols.items()},
        "columns_from_real_data": sorted(k for k, v in cols.items() if v[1]),
        "columns_imputed": sorted(k for k, v in cols.items() if not v[1]),
        "sources": {
            "roads": road_info, "landslide_inventory": ev_info,
            "terrain_dem": dem_info, "soil": {k: v for k, v in soil_info.items()
                                              if k != "retention"},
            "rainfall": (rain_idx.info if rain_idx else None),
            "soil_moisture": moist_info, "spatial_join": join_info,
            "fusion": fuse_info,
            "city_anchors": {k: v for k, v in (city_info or {}).items()
                             if k != "attachment"},
            "road_fetch": fetch_info,
        },
        "optional_packages": dict(HAS),
    }


# --------------------------------------------------------------------------- #
#  DEMO INPUT FABRICATION - real formats, offline
# --------------------------------------------------------------------------- #
def densify(coords: Sequence[Sequence[float]], every_m: float = 400.0
            ) -> List[List[float]]:
    """Insert interpolated vertices so consecutive points are <= `every_m` apart.

    Used for sampling, not for rewriting geometry: a soil or NDVI point lookup
    wants a dense set of positions along a way, while the persisted GeoJSON
    should stay exactly as the source had it.
    """
    out = [[float(coords[0][0]), float(coords[0][1])]]
    for a, b in zip(coords[:-1], coords[1:]):
        d = hpe.haversine_m(a[1], a[0], b[1], b[0])
        k = max(1, int(math.ceil(d / max(every_m, 1.0))))
        for i in range(1, k + 1):
            t = i / k
            out.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
    return out


def _demo_wiggle(t: float) -> Tuple[float, float]:
    """Deterministic lateral offset along a demo corridor, in degrees.

    Shared by the road geometry and the inventory jitter so fabricated events
    fall on the carriageway they are meant to have blocked.  Keeping the two
    apart is the single easiest way to make a synthetic join rate look broken.
    """
    # the sin(t*pi) envelope forces the offset to zero at both ends: corridors
    # that share a city must share the EXACT vertex, or node identity (an exact
    # lat/lon key) leaves them as separate connected components
    w = 0.010 * math.sin(t * math.pi * 2.3) * math.sin(t * math.pi)
    return w, w * 0.6


def fabricate_demo_inputs(out_dir: str, seed: int = DEFAULT_SEED,
                          quiet: bool = False) -> Dict[str, str]:
    """Write sample files in the *exact* formats the real portals produce.

    Nothing here is engine input: these are stand-ins for downloads, in the same
    containers and with the same headers, so the whole fusion path - .hgt/.asc
    parsing, OSM tag mapping, GSI-style CSV sniffing, IMD district joins and
    ERA5-Land moisture - is exercised without a network connection.
    """
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "srtm"), exist_ok=True)
    rng = random.Random(seed)
    paths: Dict[str, str] = {}

    corridors = [
        ("Guwahati", 26.1445, 91.7362, "Kamrup Metropolitan", "Assam",
         "Shillong", 25.5788, 91.8933, "East Khasi Hills", "Meghalaya", "NH-10"),
        ("Guwahati", 26.1445, 91.7362, "Kamrup Metropolitan", "Assam",
         "Silchar", 24.8333, 92.7789, "Cachar", "Assam", "NH-27"),
        ("Silchar", 24.8333, 92.7789, "Cachar", "Assam",
         "Aizawl", 23.7271, 92.7176, "Aizawl", "Mizoram", "NH-306"),
        ("Imphal", 24.8170, 93.9368, "Imphal West", "Manipur",
         "Kohima", 25.6751, 94.1083, "Kohima", "Nagaland", "NH-2"),
        ("Guwahati", 26.1445, 91.7362, "Kamrup Metropolitan", "Assam",
         "Tezpur", 26.6333, 92.8000, "Sonitpur", "Assam", "NH-27"),
        # NH-37 ties the Barak valley to Manipur; without it the Imphal-Kohima
        # corridor is a separate connected component and every route through it
        # reports NO PATH
        ("Silchar", 24.8333, 92.7789, "Cachar", "Assam",
         "Imphal", 24.8170, 93.9368, "Imphal West", "Manipur", "NH-37"),
    ]

    # ---- 1. DEM: an ESRI ASCII grid over the corridors + one SRTM .hgt -----
    west, south, east, north = 91.50, 23.45, 94.55, 26.95
    cell = 0.005
    ncols = int(round((east - west) / cell)) + 1
    nrows = int(round((north - south) / cell)) + 1
    # Brahmaputra valley floor in the north-west, Shillong/Khasi plateau in the
    # south, Barail and Naga ridges in the east - the real NER relief pattern
    anchors = ((25.55, 91.90, 1520.0, 0.42), (26.10, 92.90, 780.0, 0.55),
               (25.10, 93.30, 1150.0, 0.48), (25.80, 94.00, 900.0, 0.40),
               (23.75, 92.70, 1010.0, 0.45), (24.40, 93.60, 1240.0, 0.50))
    asc = os.path.join(out_dir, "srtm", "ner_dem.asc")
    with open(asc, "w") as fh:
        fh.write(f"ncols        {ncols}\n")
        fh.write(f"nrows        {nrows}\n")
        fh.write(f"xllcorner    {west}\n")
        fh.write(f"yllcorner    {south}\n")
        fh.write(f"cellsize     {cell}\n")
        fh.write("NODATA_value  -9999\n")
        lines = []
        for r in range(nrows):
            lat = north - r * cell
            base = 45.0 + 20.0 * math.sin(lat * 90.0)
            row = []
            for c in range(ncols):
                lon = west + c * cell
                h = base
                for (al, ao, amp, sc) in anchors:
                    d2 = ((lat - al) ** 2 + (lon - ao) ** 2) / (sc * sc)
                    if d2 < 12.0:
                        h += amp * math.exp(-d2)
                h += 120.0 * math.sin(lon * 210.0) * math.cos(lat * 180.0)
                h += 70.0 * math.sin(lon * 640.0 + lat * 520.0)
                h += 34.0 * math.cos(lon * 1450.0 - lat * 1180.0)
                row.append(f"{max(h, 6.0):.1f}")
            lines.append(" ".join(row))
        fh.write("\n".join(lines))
    paths["dem"] = os.path.join(out_dir, "srtm")
    _log(f"  fabricated DEM (.asc) : {asc} ({ncols}x{nrows} @ {cell * 111000:.0f} m)",
         quiet)

    # one genuine SRTM-format tile, north of the corridors, to exercise the
    # big-endian int16 .hgt reader as well
    side = 301
    hgt = os.path.join(out_dir, "srtm", "N27E092.hgt")
    buf = array.array("h", bytes(side * side * 2))
    for r in range(side):
        lat = 28.0 - r / (side - 1)
        for c in range(side):
            lon = 92.0 + c / (side - 1)
            h = 900.0 + 420.0 * math.exp(-((lat - 27.5) ** 2
                                           + (lon - 92.4) ** 2) / 0.09)
            h += 60.0 * math.sin(lon * 300.0) * math.cos(lat * 260.0)
            buf[r * side + c] = int(hpe.clamp(round(h), 6, 8000))
    if sys.byteorder == "little":
        buf.byteswap()                        # .hgt is big-endian on disk
    with open(hgt, "wb") as fh:
        fh.write(buf.tobytes())
    _log(f"  fabricated SRTM tile  : {hgt} ({side}x{side} big-endian int16, "
         f"1x1 deg at N27E092)", quiet)

    # ---- 2. roads: OSM-style GeoJSON with real tag names -------------------
    roads = os.path.join(out_dir, "ner_roads.geojson")
    feats = []
    soil_points: List[Tuple[float, float, str]] = []   # (lat, lon, soil family)
    families = {
        # raw ISRIC integers, decoded by SOILGRIDS_DIVISOR: clay/sand/silt are
        # g/kg (/10 -> %), soc dg/kg (/10 -> %), bdod cg/cm3 (/100 -> g/cm3),
        # wv0033/wv1500 cm3/cm3 x100 (/10 -> vol%), phh2o x10, cec mmol(c)/kg x10
        "plateau_laterite":  {"clay": 340, "sand": 300, "silt": 360, "soc": 14,
                              "bdod": 138, "wv0033": 310, "wv1500": 140,
                              "phh2o": 52, "cec": 950},
        "brahmaputra_alluvium": {"clay": 420, "sand": 180, "silt": 400, "soc": 11,
                                 "bdod": 152, "wv0033": 360, "wv1500": 180,
                                 "phh2o": 61, "cec": 1420},
        "barail_sandy_loam":  {"clay": 160, "sand": 610, "silt": 230, "soc": 7,
                               "bdod": 161, "wv0033": 230, "wv1500": 90,
                               "phh2o": 47, "cec": 480},
    }
    names = sorted(families)
    fam_of = {(c[0], c[5]): names[hpe._stable_hash(c[0] + c[5] + c[10])
                                  % len(names)] for c in corridors}
    for (n1, la1, lo1, d1, s1, n2, la2, lo2, d2, s2, ref) in corridors:
        key = hpe._stable_hash(ref + n1 + n2)
        # real OSM trunk ways carry a vertex every few hundred metres; a
        # 26-vertex 65 km chord would be 2.5 km per vertex, which starves both
        # the DEM sampler and the soil cache of points to look up
        chord_km = hpe.haversine_m(la1, lo1, la2, lo2) / 1000.0
        steps = max(24, int(chord_km * 1000.0 / 450.0))
        coords = []
        for i in range(steps + 1):
            t = i / steps
            wl, wt = _demo_wiggle(t)
            coords.append([round(lo1 + (lo2 - lo1) * t + wl, 6),
                           round(la1 + (la2 - la1) * t + wt, 6)])
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"osm_id": f"way-{key % 10 ** 8}", "highway": "trunk",
                           "ref": ref, "name": f"{ref} {n1}-{n2}",
                           "surface": "asphalt", "lanes": 2, "maxspeed": 60,
                           "cutting": "yes" if (key % 100) < 35 else "no",
                           "embankment": "no", "state": s1, "district": d1}})
        fam = fam_of[(n1, n2)]
        soil_points.extend((c[1], c[0], fam) for c in densify(coords, 400.0))
        for k in range(4):                     # feeder roads so the graph joins up
            # attach at an EXISTING trunk vertex.  Recomputing the position from
            # the corridor chord instead would land metres away from the wiggled
            # centreline, and since node identity is an exact lat/lon key the
            # feeder would silently form its own connected component.
            vi = min(max(int(round(steps * (0.20 + 0.20 * k))), 1), len(coords) - 2)
            blon, blat = coords[vi][0], coords[vi][1]
            t = vi / steps
            lkey = hpe._stable_hash(f"{ref}-link-{k}")
            feats.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [
                    [round(blon, 6), round(blat, 6)],
                    [round(blon + 0.085 * (0.6 + (lkey % 40) / 50.0), 6),
                     round(blat + 0.065 * ((lkey % 240) / 100.0 - 1.2), 6)],
                    [round(blon + 0.170 * (0.6 + (lkey % 37) / 50.0), 6),
                     round(blat + 0.020 * ((lkey % 200) / 100.0 - 1.0), 6)]]},
                "properties": {"osm_id": f"way-link-{k}-{lkey % 10 ** 6}",
                               "highway": "secondary", "surface": "asphalt",
                               "lanes": 1, "name": f"link {k} off {ref}",
                               "cutting": "no",
                               "state": s1 if t < 0.5 else s2,
                               "district": d1 if t < 0.5 else d2}})
            soil_points.extend((c[1], c[0], fam) for c in
                               densify(feats[-1]["geometry"]["coordinates"], 400.0))
    with open(roads, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)
    paths["roads"] = roads
    _log(f"  fabricated road net   : {roads} ({len(feats)} OSM ways)", quiet)

    # ---- 3. GSI-style landslide inventory CSV ------------------------------
    inv = os.path.join(out_dir, "gsi_landslide_inventory.csv")
    with open(inv, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Sl_No", "Landslide_ID", "Latitude", "Longitude", "Event_Date",
                    "District", "State", "Landslide_Type", "Severity",
                    "Rainfall_mm"])
        n = 0
        for (n1, la1, lo1, d1, s1, n2, la2, lo2, d2, s2, ref) in corridors:
            for j in range(30):
                ekey = hpe._stable_hash(f"{ref}-{j}")
                r2 = random.Random(ekey)
                t = r2.random()
                # GSI points are landslide locations, not road-km markers:
                # most sit within ~50 m of the carriageway, some further out
                off = 0.0004 if (j % 5) else 0.0022
                wl, wt = _demo_wiggle(t)
                lat = la1 + (la2 - la1) * t + wt + r2.gauss(0, off)
                lon = lo1 + (lo2 - lo1) * t + wl + r2.gauss(0, off)
                month = r2.choices([6, 7, 8, 9], weights=[22, 30, 28, 20])[0]
                d = _dt.date(2023, month, r2.randint(1, 28))
                htype = r2.choice(["Landslide", "Debris Flow", "Slope Failure",
                                   "Boulder Fall", "Road Collapse", "Flash Flood"])
                sev = r2.choice(["Low", "Moderate", "High", "Severe"])
                n += 1
                w.writerow([n, f"GSI-LS-2023-{n:05d}", f"{lat:.6f}", f"{lon:.6f}",
                            d.strftime("%d/%m/%Y"), d1 if t < 0.5 else d2,
                            s1 if t < 0.5 else s2, htype, sev,
                            round(r2.gammavariate(2.2, 32.0), 1)])
    paths["landslides"] = inv
    _log(f"  fabricated inventory  : {inv} ({n} events, GSI-style header)", quiet)

    # ---- 4. IMD-style district daily rainfall CSV --------------------------
    rf = os.path.join(out_dir, "imd_district_daily_rainfall.csv")
    districts = [("Kamrup Metropolitan", "Assam"), ("East Khasi Hills", "Meghalaya"),
                 ("Cachar", "Assam"), ("Aizawl", "Mizoram"),
                 ("Imphal West", "Manipur"), ("Kohima", "Nagaland"),
                 ("Sonitpur", "Assam"), ("Hailakandi", "Assam")]
    with open(rf, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "district", "state", "rainfall_mm"])
        d = _dt.date(2023, 5, 20)
        while d <= _dt.date(2023, 10, 31):
            for (dn, st) in districts:
                dr = random.Random(hpe._stable_hash(f"{dn}{d.isoformat()}"))
                seas = 1.0 if d.month in (6, 7, 8, 9) else 0.25
                mm = dr.gammavariate(0.55, 26.0 * seas)
                w.writerow([d.isoformat(), dn, st, round(max(mm, 0.0), 1)])
            d += _dt.timedelta(days=1)
    paths["rainfall"] = rf
    _log(f"  fabricated rainfall   : {rf} (IMD district-daily layout)", quiet)

    # ---- 5. ERA5-Land style soil-moisture CSV ------------------------------
    sm = os.path.join(out_dir, "era5land_soil_moisture.csv")
    with open(sm, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "lat", "lon", "volumetric_soil_water_layer_1"])
        d = _dt.date(2023, 6, 1)
        while d <= _dt.date(2023, 10, 15):
            for lat in (24.9, 25.3, 25.7, 26.1, 26.5):
                for lon in (91.8, 92.4, 93.0, 93.6, 94.1):
                    mr = random.Random(hpe._stable_hash(
                        f"sm{lat}{lon}{d.isoformat()}"))
                    wet = 1.0 if d.month in (7, 8) else 0.62
                    w.writerow([d.isoformat(), f"{lat:.3f}", f"{lon:.3f}",
                                round(hpe.clamp(
                                    mr.gauss(0.33 * wet + 0.10, 0.045),
                                    0.05, 0.50), 4)])
            d += _dt.timedelta(days=3)
    paths["moisture"] = sm
    _log(f"  fabricated moisture   : {sm} (ERA5-Land vswl1 layout)", quiet)

    # ---- 6. SoilGrids response cache ---------------------------------------
    # Bodies are built in the exact integer-encoded shape rest.isric.org
    # returns, then pushed through SoilGridsClient._parse - so the unit
    # decoding (wv/10 -> vol%, clay/10 -> %, bdod/100 -> g/cm3, phh2o/10) is
    # exercised offline rather than bypassed.  Keys are the road VERTICES,
    # because e.lat/e.lon is the first vertex of each split segment: caching
    # corridor midpoints instead misses almost every lookup.  Three NER soil
    # families: plateau laterite, Brahmaputra alluvium, Barail sandy loam.
    cache_dir = os.path.join(out_dir, "soilgrids_cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache: Dict[str, Dict[str, float]] = {}
    raw_doc = None
    for (lat, lon, fam) in soil_points:
        raw_doc = {"properties": {"layers": [
            {"name": prop, "depths": [{"label": "0-5cm",
                                       "values": {"mean": iv}}]}
            for prop, iv in families[fam].items()]}}
        cache[SoilGridsClient.key(lat, lon)] = SoilGridsClient._parse(raw_doc)
    with open(os.path.join(cache_dir, "raw_response_sample.json"), "w") as fh:
        json.dump(raw_doc, fh, indent=1)
    cache_fp = os.path.join(cache_dir, "soilgrids.json")
    with open(cache_fp, "w") as fh:
        json.dump(cache, fh, indent=1, sort_keys=True)
    paths["soilgrids_cache"] = cache_fp
    _log(f"  fabricated soil cache : {cache_fp} ({len(cache)} decoded points "
         f"from integer-encoded ISRIC bodies)", quiet)
    return paths


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="data_ingestion.py",
        description="Fuse real NER datasets (GSI landslide inventory, OSM roads, "
                    "SRTM/CartoDEM terrain, SoilGrids soils, IMD rainfall, "
                    "ERA5-Land/SMAP moisture) into the SIH26002 training schema.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python data_ingestion.py --demo
  python data_ingestion.py --roads data/raw/ner_roads.geojson \\
      --landslides data/raw/gsi_inventory.csv --dem data/raw/srtm/ \\
      --rainfall data/raw/imd_district_daily.csv --soil online
  python data_ingestion.py --demo --train          # fuse then train the engine
  python data_ingestion.py --inspect data/raw/gsi_inventory.csv

  # fetch the roads live instead of supplying a file
  python data_ingestion.py --state Mizoram --landslides data/raw/gsi_inventory.csv
  python data_ingestion.py --state Assam --bbox 24.0,89.5,28.0,96.0 \
      --gsi-csv data/gsi_landslides.csv --max-edges 4000 --train
""")
    ap.add_argument("--roads", help="OSM/GeoJSON road network on disk; omit it "
                                    "and pass --state/--bbox to fetch one")
    ap.add_argument("--state", default=None,
                    help="NER state to fetch roads for when --roads is absent "
                         "(Assam, Meghalaya, Arunachal[ Pradesh], Nagaland, "
                         "Manipur, Mizoram, Tripura, Sikkim; short codes and "
                         "case variants accepted)")
    ap.add_argument("--bbox", default=None, metavar="BBOX",
                    help="fetch window, as S,W,N,E or W,S,E,N - both are "
                         "accepted and told apart by magnitude, so the two "
                         "conventions in circulation cannot be confused "
                         "silently. Overrides the --state box")
    ap.add_argument("--network-type", default="drive",
                    choices=("drive", "all"),
                    help="which OSM highways to fetch (default drive; 'all' "
                         "adds track/service/footway, useful where a landslide "
                         "inventory references rural tracks)")
    ap.add_argument("--max-edges", type=int, default=4000,
                    help="cap on fetched ways. The window is SHRUNK around the "
                         "inventory centroid to fit, never sampled row-wise - "
                         "random sampling would leave isolated two-node islands "
                         "and every corridor would report NO PATH. 0 = "
                         "unlimited (default 4000)")
    ap.add_argument("--road-cache", default=None,
                    help="where to cache a fetched network "
                         "(default <cache-dir>/osm_<state>.geojson); a cached "
                         "network is reused, so only the first run needs network")
    ap.add_argument("--overpass-timeout", type=int, default=180,
                    help="seconds to allow an Overpass query (default 180)")
    ap.add_argument("--offline", action="store_true",
                    help="never touch the network: use caches, or fail with an "
                         "explanation instead of dialling out")
    ap.add_argument("--landslides", "--gsi-csv", dest="landslides",
                    help="GSI/NGDR landslide inventory CSV or GeoJSON")
    ap.add_argument("--dem", help="DEM file or directory (.hgt/.hgt.gz/.asc/.tif)")
    ap.add_argument("--slope-tif", default=None,
                    help="pre-computed slope raster (GDAL/QGIS/ASTER "
                         "derivative). Degrees or percent - the unit is "
                         "detected and reported, since nothing above 90 can be "
                         "degrees. Takes precedence over slope derived from "
                         "--dem")
    ap.add_argument("--ndvi", help="optional NDVI raster (.asc/.tif)")
    ap.add_argument("--rainfall", help="IMD/NESAC/NEDFI rainfall CSV")
    ap.add_argument("--moisture", help="ERA5-Land/SMAP soil moisture CSV")
    ap.add_argument("--soil", default="offline",
                    choices=("offline", "online", "csv"),
                    help="offline=texture defaults, online=SoilGrids REST, "
                         "csv=--soil-csv table")
    ap.add_argument("--soil-cache", default=None,
                    help="SoilGrids JSON cache to read/write "
                         "(default data/cache/soilgrids.json)")
    ap.add_argument("--city-radius", type=float, default=5.0,
                    help="snap NER city names onto junctions within this many km "
                         "(default 5) so name-based corridors resolve; cities "
                         "further out are reported as not reachable")
    ap.add_argument("--no-city-names", action="store_true",
                    help="leave ingested junctions unnamed (corridors must then "
                         "be given as 'lat,lon' or node ids)")
    ap.add_argument("--soil-nearest-km", type=float, default=1.0,
                    help="serve a cached SoilGrids point within this radius when "
                         "the exact key is absent or the API fails; 0 disables "
                         "(default 1.0 - SoilGrids cells are only 250 m)")
    ap.add_argument("--soil-csv", help="local soil properties CSV (lat,lon,clay,...)")
    ap.add_argument("--buffer", type=float, default=100.0,
                    help="event-to-road join buffer in metres (default 100; "
                         "try 250-500 for inventories with poor positional "
                         "accuracy)")
    ap.add_argument("--max-positives", type=int, default=6,
                    help="cap positives per segment so one hotspot cannot "
                         "dominate the fit (default 6)")
    ap.add_argument("--year", type=int, default=2023, help="monsoon year to build")
    ap.add_argument("--obs-per-segment", type=int, default=4,
                    help="observation dates sampled per segment (default 4)")
    ap.add_argument("--window", type=int, default=3,
                    help="+/- days for matching an event to an observation (default 3)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out", default=None, help="fused hazard CSV output path")
    ap.add_argument("--graph-out", default=None, help="enriched graph GeoJSON path")
    ap.add_argument("--report", default=None, help="provenance/gap report JSON path")
    ap.add_argument("--cache-dir", default=CACHE_DIR, help="SoilGrids cache dir")
    ap.add_argument("--min-length", type=float, default=50.0,
                    help="drop OSM ways shorter than this (metres)")
    ap.add_argument("--max-length", type=float, default=12000.0,
                    help="split OSM ways longer than this (metres)")
    ap.add_argument("--demo", action="store_true",
                    help="fabricate real-format sample inputs, then run the pipeline")
    ap.add_argument("--train", action="store_true",
                    help="after fusing, train the hazard engine on the result")
    ap.add_argument("--inspect", metavar="PATH",
                    help="print the detected column mapping for a CSV and exit")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--log-level", default=None,
                    choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                    help="route progress through the logging module with "
                         "timestamps instead of plain stdout")
    ap.add_argument("--log-file", default=None,
                    help="also write the log here (implies --log-level INFO)")
    ap.add_argument("--version", action="store_true")
    return ap


def _is_2d(d: Any) -> bool:
    """True for a numpy 2-D raster, False for a flat array.array."""
    try:
        iter(d[0])
        return True
    except (TypeError, IndexError):
        return False


def inspect_source(path: str, quiet: bool = False) -> int:
    """Dry-run one input file: what it is, what will be read, what is missing.

    Deliberately format-aware.  Pointing `--inspect` at a GeoJSON road network
    and parsing it as CSV produces pages of coordinate noise and hides the one
    number that actually matters - whether the network is connected.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".gz", ".hgt"):
        ext = os.path.splitext(path[:-3] if ext == ".gz" else path)[1].lower()
    print(f"\nINSPECT {path}")
    if not os.path.exists(path):
        print(f"  ! no such file")
        return 2

    # ---- directory of DEM tiles, or a single raster ----------------------
    if os.path.isdir(path) or ext in (".hgt", ".asc", ".tif", ".tiff"):
        dem = load_dem(path, quiet=True)
        if not len(dem):
            print("  ! no readable DEM tiles (.hgt / .asc / .tif) found")
            return 3
        w, so, e, no = dem.bbox()
        print(f"  type     : DEM mosaic, {len(dem)} tile(s)")
        for g in dem.grids:
            print(f"    tile   : {os.path.basename(g.source)} {g.ncols}x{g.nrows} "
                  f"@ {g.res_deg_y * 111000:.0f} m, "
                  f"[{g.west:.3f},{g.south:.3f},{g.east:.3f},{g.north:.3f}]")
        print(f"  bbox     : [{w:.3f}, {so:.3f}, {e:.3f}, {no:.3f}] "
              f"({(e - w) * 111 * max(.2, math.cos(math.radians((so + no) / 2))):.0f}"
              f" x {(no - so) * 111:.0f} km)")
        flat: List[float] = []
        for g in dem.grids:
            d = g.data
            flat.extend(v for row in (d if _is_2d(d) else [d]) for v in row)
        good = sorted(v for v in flat if v > g.nodata)
        if good:
            print(f"  elevation: min {good[0]:.0f} m, median {good[len(good) // 2]:.0f} m, "
                  f"max {good[-1]:.0f} m")
        voids = len(flat) - len(good)
        print(f"  cells    : {len(flat)} total, {voids} void/NoData "
              f"({100.0 * voids / max(len(flat), 1):.2f}%)")
        print("  tip      : SRTM/NASADEM tiles must be named after their "
              "south-west corner (N26E091.hgt) or the tile cannot be georeferenced")
        return 0

    # ---- GeoJSON / JSON --------------------------------------------------
    head = ""
    with open(path, "r", errors="replace") as fh:
        head = fh.read(2)
    if ext in (".geojson", ".json") or head.lstrip()[:1] in ("{", "["):
        doc = _read_json(path)
        feats = doc.get("features") or []
        print(f"  type     : GeoJSON FeatureCollection, {len(feats)} feature(s)")
        kinds: Dict[str, int] = {}
        props: Dict[str, Any] = {}
        hw: Dict[str, int] = {}
        lons: List[float] = []
        lats: List[float] = []
        nvert = 0
        for f in feats:
            geom = f.get("geometry") or {}
            kinds[geom.get("type") or "None"] = kinds.get(
                geom.get("type") or "None", 0) + 1
            pr = f.get("properties") or {}
            for k, v in pr.items():
                props.setdefault(k, v)
            h = str(pr.get("highway") or pr.get("road_class") or "")
            if h:
                hw[h] = hw.get(h, 0) + 1
            for cs, _pr in _feature_parts(f):
                nvert += len(cs)
                lons.extend(c[0] for c in cs)
                lats.extend(c[1] for c in cs)
        print(f"  geometry : " + ", ".join(f"{k}:{v}" for k, v in sorted(kinds.items())))
        if lons:
            print(f"  bbox     : [{min(lons):.4f}, {min(lats):.4f}, "
                  f"{max(lons):.4f}, {max(lats):.4f}]  ({nvert} vertices)")
        if hw:
            print("  highway  : " + ", ".join(f"{k}:{v}" for k, v in
                                              sorted(hw.items(), key=lambda x: -x[1])))
        print(f"  tags     : {sorted(props)}")
        recognised = [t for t in ("highway", "surface", "lanes", "maxspeed",
                                  "cutting", "embankment", "name", "ref",
                                  "state", "district") if t in props]
        print(f"  used     : {recognised or 'none - only geometry will be read'}")
        unused = [t for t in ("cutting", "embankment", "surface", "district")
                  if t not in props]
        if unused:
            print(f"  absent   : {unused} -> those features fall back to "
                  f"documented defaults")
        try:
            g, info = roads_from_geojson(path, quiet=True)
        except Exception as exc:                            # noqa: BLE001
            print(f"  ! loader failed: {type(exc).__name__}: {exc}")
            return 3
        adj: Dict[str, set] = {}
        for e in g.edges:
            adj.setdefault(e.u, set()).add(e.v)
            adj.setdefault(e.v, set()).add(e.u)
        seen: set = set()
        sizes: List[int] = []
        for nid in g.nodes:
            if nid in seen:
                continue
            stack, cnt = [nid], 0
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                cnt += 1
                stack.extend(adj.get(x, ()) - seen)
            sizes.append(cnt)
        sizes.sort(reverse=True)
        print(f"  loaded   : {info['edges']} segments / {len(g.nodes)} nodes / "
              f"{g.total_length_km():.1f} km "
              f"({info['split_count']} length splits, "
              f"{info['junction_splits']} junction splits)")
        print(f"  topology : {len(sizes)} connected component(s), largest "
              f"{sizes[0]} node(s)" + (f", sizes {sizes[:6]}" if len(sizes) > 1 else ""))
        if len(sizes) > 1:
            print("  ! the network is FRAGMENTED: corridors crossing a component "
                  "boundary will report NO PATH.  Usual cause is ways that touch "
                  "geometrically but do not share a vertex - snap them "
                  "(QGIS 'Snap geometries', or OSM 'fix junctions') before export.")
        return 0

    # ---- CSV / TSV -------------------------------------------------------
    header, rows = read_csv_rows(path)
    cmap = guess_columns(header)
    print(f"  type     : delimited table, {len(rows)} row(s), {len(header)} column(s)")
    print(f"  header   : {header}")
    print("  detected : " + (", ".join(f"{k}<-{v}" for k, v in cmap.items() if v)
                             or "nothing recognised"))
    sm = [h for h in header
          if any(t in _norm_key(h) for t in ("soilmoisture", "volumetricsoilwater",
                                             "vswl", "soilwater"))]
    # what a given layout actually needs: an IMD district rainfall table has no
    # coordinates and that is correct, not an error
    if sm or cmap.get("hazard_type") or (cmap.get("lat") and not cmap.get("rainfall")):
        required = ("lat", "lon", "date")
    elif cmap.get("rainfall") and cmap.get("district"):
        required = ("date", "rainfall", "district")
    elif cmap.get("rainfall"):
        required = ("date", "rainfall")
    else:
        required = ("lat", "lon")
    missing = [k for k in required if not cmap.get(k)]
    if missing:
        print(f"  ! required but unmapped: {missing}")
        print("    rename a column to one of the accepted aliases, or check the "
              "mapping against COLUMN_ALIASES in this file")
    if cmap.get("lat") and cmap.get("lon") and cmap.get("date") and sm:
        kind = f"soil-moisture grid (ERA5-Land / SMAP), moisture column '{sm[0]}'"
    elif cmap.get("lat") and cmap.get("lon") and cmap.get("date"):
        kind = "hazard inventory (GSI Bhukosh / NGDR / NLSM)"
    elif cmap.get("date") and cmap.get("rainfall"):
        kind = ("rainfall grid" if cmap.get("lat")
                else "rainfall by district (IMD / NESAC / NEDFI)")
    elif cmap.get("lat") and cmap.get("lon"):
        kind = "point soil table (--soil-csv)"
    else:
        kind = "unrecognised - see the aliases above"
    print(f"  reads as : {kind}")
    for r in rows[:3]:
        print(f"  sample   : {dict(list(r.items())[:8])}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"SIH26002 data ingestion v{__version__} "
              f"(numpy={HAS['numpy']}, rasterio={HAS['rasterio']}, "
              f"osmnx={HAS['osmnx']})")
        return 0
    if args.inspect:
        return inspect_source(args.inspect, quiet=args.quiet)

    quiet = args.quiet
    if args.log_level or args.log_file:
        configure_logging(args.log_level or "INFO", args.log_file)
    t0 = time.perf_counter()
    print("=" * 78)
    print(" SIH26002 DATA INGESTION & FUSION - North Eastern Region")
    print("=" * 78)

    roads_p, land_p, dem_p, rain_p, moist_p = (args.roads, args.landslides,
                                               args.dem, args.rainfall,
                                               args.moisture)
    soil_cache_p, soil_synthetic = None, False
    if args.demo:
        print("\n[0] fabricating real-format sample inputs (offline)")
        p = fabricate_demo_inputs(RAW_DIR, seed=args.seed, quiet=quiet)
        roads_p = roads_p or p["roads"]
        land_p = land_p or p["landslides"]
        dem_p = dem_p or p["dem"]
        rain_p = rain_p or p["rainfall"]
        moist_p = moist_p or p["moisture"]
        soil_cache_p = soil_cache_p or p.get("soilgrids_cache")

    # --state / --bbox decide where to fetch roads when no file was given
    fetch_info: Optional[Dict[str, Any]] = None
    state_canon = resolve_state(args.state)
    if args.state and state_canon is None:
        print(f"\nERROR: unknown --state {args.state!r}. Known states: "
              + ", ".join(sorted(NER_STATE_BOXES))
              + ". Short codes (ap, as, ml, nl, mn, mz, tr, sk) also work.")
        return 2
    bbox: Optional[Tuple[float, float, float, float]] = None
    if args.bbox:
        try:
            bbox = parse_bbox(args.bbox)
        except ValueError as exc:
            print(f"\nERROR: --bbox {args.bbox!r}: {exc}")
            return 2
        if state_canon:
            _log(f"  fetch window        : {state_canon} overridden by --bbox "
                 f"(W,S,E,N)={tuple(round(v, 3) for v in bbox)}", quiet)
    elif state_canon:
        bbox = NER_STATE_BOXES[state_canon]
        _log(f"  fetch window        : {state_canon} "
             f"(W,S,E,N)={tuple(round(v, 3) for v in bbox)}", quiet)

    missing = []
    if not land_p:
        missing.append("--landslides")
    if not roads_p and not bbox:
        missing.append("--roads (or --state/--bbox to fetch one live)")
    if missing:
        print(f"\nERROR: {' and '.join(missing)} are required (or use --demo).")
        print("See DATASETS.md for where to download each source and the exact "
              "layout this loader reads.")
        return 2

    out_csv = args.out or os.path.join(HERE, "data",
                                       f"historical_hazards_{args.year}.csv")
    graph_out = args.graph_out or os.path.join(HERE, "data", "ner_roads.geojson")
    report_out = args.report or os.path.join(HERE, "outputs",
                                             "ingestion_report.json")

    print("\n[1] loading sources")
    # the inventory is read FIRST on purpose: a live road fetch crops its window
    # around where the landslides actually are, rather than around the centre of
    # a state box that may be 500 km across and mostly event-free
    events, ev_info = load_events(land_p, quiet=quiet)
    if not roads_p and bbox:
        # the cache name must depend on the WINDOW, not only on the state: two
        # different --bbox values would otherwise share one file silently
        tag = _norm_key(state_canon) or ("bbox-" + hpe._stable_hash(
            str(tuple(round(v, 3) for v in bbox)))[:10])
        road_cache = args.road_cache or os.path.join(
            args.cache_dir, f"osm_{tag}.geojson")
        try:
            roads_p, fetch_info = acquire_roads(
                bbox, state=state_canon, network_type=args.network_type,
                max_edges=args.max_edges, cache_path=road_cache,
                offline=args.offline, quiet=quiet,
                overpass_timeout=args.overpass_timeout,
                centre=inventory_centroid(events))
        except Exception as exc:                           # noqa: BLE001
            print(f"\nERROR: could not obtain a road network: {exc}")
            print("Pass --roads <network.geojson> instead - DATASETS.md section 4 "
                  "has an Overpass query to paste into a browser and save - or "
                  "run once with network access so the result gets cached.")
            return 4
    graph, road_info = roads_from_geojson(roads_p, min_length_m=args.min_length,
                                          max_length_m=args.max_length, quiet=quiet)
    if fetch_info is not None:
        road_info["fetch"] = fetch_info
    largest = hpe._largest_component(graph)
    if graph.nodes and largest < 0.9 * len(graph.nodes):
        _warn(f"  ! connectivity        : largest component holds {largest}/"
              f"{len(graph.nodes)} nodes - this network is FRAGMENTED, so any "
              f"corridor crossing the gap reports NO PATH. Diagnose with "
              f"--inspect {os.path.basename(str(roads_p))}.", quiet)
    rain = load_rainfall(rain_p, quiet=quiet) if rain_p else None
    moisture, moist_info = load_moisture(moist_p, quiet=quiet)

    dem = None
    dem_info = None
    ndvi = None
    slope_raster = None
    if dem_p:
        dem = load_dem(dem_p, quiet=quiet)
        print(f"    DEM mosaic          : {dem.summary()['tiles']} tiles, "
              f"bbox {[round(v, 2) for v in dem.bbox()]}")
    if args.ndvi:
        ndvi = load_dem(args.ndvi, quiet=quiet)
    if args.slope_tif:
        slope_raster = load_dem(args.slope_tif, quiet=quiet)
        print(f"    slope raster        : {slope_raster.summary()['tiles']} tiles, "
              f"bbox {[round(v, 2) for v in slope_raster.bbox()]}")

    print("\n[2] enriching segments")
    dem_info = enrich_graph_with_dem(graph, dem, ndvi,
                                     slope_raster=slope_raster, quiet=quiet)
    city_info = ({"cities_attached": 0, "note": "disabled by --no-city-names"}
                 if args.no_city_names
                 else attach_city_names(graph, radius_km=args.city_radius,
                                        quiet=quiet))
    soil = None
    cache_fp = (args.soil_cache or soil_cache_p
                or os.path.join(args.cache_dir, "soilgrids.json"))
    if args.soil == "online" and args.offline:
        _warn("    --offline           : ignoring --soil online; SoilGrids would "
              "have to dial out", quiet)
    if args.soil == "online" and not args.offline:
        os.makedirs(os.path.dirname(cache_fp) or ".", exist_ok=True)
        soil = SoilGridsClient(cache_path=cache_fp, quiet=quiet,
                               nearest_km=args.soil_nearest_km)
        print(f"    SoilGrids REST      : {SOILGRIDS_URL} (cache {cache_fp})")
    elif os.path.exists(cache_fp):
        # no network needed: replay cached ISRIC responses, never dial out
        soil = SoilGridsClient(cache_path=cache_fp, quiet=quiet, offline=True,
                               nearest_km=args.soil_nearest_km)
        print(f"    SoilGrids cache     : {cache_fp} "
              f"({len(soil.cache)} points, offline)")
        if soil_cache_p:
            soil_synthetic = True
            print("    ! that cache was FABRICATED for the offline demo. Delete "
                  "it and re-run with --soil online for live ISRIC values.")
    elif args.soil_csv and os.path.exists(args.soil_csv):
        print(f"    soil source         : local table {args.soil_csv}")
    else:
        print("    soil source         : NONE - soil_type / drainage / water "
              "retention will be IMPUTED from texture defaults")
        print("                          pass --soil online (ISRIC REST, cached "
              "to disk) or --soil-csv <lat,lon,clay,...> for real values")
    soil_info = enrich_graph_with_soil(graph, soil,
                                       local_csv=(args.soil_csv
                                                  if args.soil in ("csv", "offline")
                                                  else None),
                                       quiet=quiet)
    if soil:
        soil.flush()
    if soil_synthetic:
        soil_info["cache_is_fabricated"] = True
    retention = soil_info.pop("retention", {})

    print("\n[3] joining inventory to the road network")
    matches, join_info = join_events_to_segments(events, graph,
                                                 buffer_m=args.buffer, quiet=quiet)

    print("\n[4] fusing the training table")
    rows, fuse_info = build_training_rows(
        graph, events, matches, rain, moisture, retention, year=args.year,
        obs_per_segment=args.obs_per_segment,
        positive_window_days=args.window,
        max_positives_per_segment=args.max_positives,
        seed=args.seed, quiet=quiet)
    if not rows:
        print("ERROR: fusion produced no rows.")
        return 3

    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(graph_out) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(report_out) or ".", exist_ok=True)
    hpe.write_hazard_csv(rows, out_csv)
    hpe.save_json(graph.to_geojson(), graph_out)
    prov = provenance_report(dem_info, soil_info, rain, moist_info, ev_info,
                             road_info, join_info, fuse_info, city_info,
                             fetch_info=fetch_info)
    hpe.save_json(prov, report_out)

    print("\n[5] result")
    print(f"  hazard CSV      -> {out_csv}  ({len(rows)} rows, "
          f"{fuse_info['positive']} positive)")
    print(f"  enriched graph  -> {graph_out}  ({len(graph.edges)} segments)")
    print(f"  provenance      -> {report_out}")
    real = prov["columns_from_real_data"]
    imp = prov["columns_imputed"]
    print(f"  real-data cols  : {', '.join(real) if real else 'none'}")
    if imp:
        print(f"  IMPUTED cols    : {', '.join(imp)}")
        print("  (imputed columns use documented fallbacks; replace the missing "
              "source to remove them)")
    print(f"  elapsed         : {time.perf_counter() - t0:.2f} s")

    if args.train:
        print("\n[6] training the hazard engine on the fused table")
        import subprocess
        cmd = [sys.executable, os.path.join(HERE, "hazard_prediction_engine.py"),
               "--hazards", out_csv, "--graph", graph_out, "--retrain"]
        # stdout is block-buffered when piped; without this flush the child's
        # whole report lands before our own banner and the log reads backwards
        sys.stdout.flush()
        r = subprocess.run(cmd, cwd=HERE)
        return r.returncode
    print("\nNext: python hazard_prediction_engine.py --hazards "
          f"{os.path.relpath(out_csv, HERE)} --graph "
          f"{os.path.relpath(graph_out, HERE)} --retrain")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
