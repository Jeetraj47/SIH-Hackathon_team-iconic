#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 SIH26002 - HAZARD PREDICTION ENGINE
 Predictive Route Optimization for Disaster-Prone Terrain
================================================================================

 A standalone machine-learning module that scores road-segment disruption risk
 in real time and re-weights OpenStreetMap graphs for monsoon-aware routing.

 CORE IDEA
 ---------
 Every road edge `e` receives a learned Segment Disruption Risk Score

     P_disruption(e) in [0, 1] = f(rain, API_3d, slope, soil_sat, hist_freq)

 which is folded into a dynamic edge cost

     C(e) = t(e) * [1 + alpha * P(e)] + beta * H(e) + gamma * S(e) * C0

 Any shortest-path engine (Dijkstra / A* / NetworkX / OSRM / pgRouting) can then
 consume the emitted GeoJSON as a drop-in re-weighted graph.

 DESIGN CONSTRAINTS (Smart India Hackathon 2026, Problem SIH26002)
 -----------------------------------------------------------------
   * SINGLE FILE, zero proprietary dependencies.
   * Runs end-to-end with ONLY the Python standard library.  If XGBoost /
     scikit-learn / PyTorch happen to be installed they are used automatically;
     otherwise a built-in pure-Python Newton-boosted tree ensemble takes over.
   * Deterministic: fixed seeds reproduce identical graphs, weather fields,
     training splits and routes.
   * Headless by design: no frontend, no web server, no GUI.  Output is JSON /
     GeoJSON on disk plus a CLI report.

 QUICK START
 -----------
     python hazard_prediction_engine.py                    # full demo pipeline
     python hazard_prediction_engine.py --scenario heavy --cargo hazmat
     python hazard_prediction_engine.py --alpha 4.0 --beta 1.2 --gamma 2.0
     python hazard_prediction_engine.py --backend pure     # zero-dependency mode
     python hazard_prediction_engine.py --benchmark
     python hazard_prediction_engine.py --selftest

 PYTHON API
 ----------
     from hazard_prediction_engine import HazardEngine, CostPolicy, route

     engine = HazardEngine.bootstrap()                     # train or load cache
     graph, weather, secs = engine.reweight(engine.graph, "heavy")
     probs = engine.score_edges(graph.edges, weather)      # -> P_disruption
     cmp_  = engine.compare_routes(graph, "Imphal", "Kohima")   # static vs hazard
     r     = route(graph, "Guwahati", "Silchar", algorithm="astar", mode="risk")
     p     = engine.score_point(25.58, 91.89, rain_mm_hr=42.0, api_3d=210.0,
                                soil_saturation=0.81, slope_deg=17.5)

 Cargo sensitivity lives on the policy, not the scoring call:
     engine = HazardEngine.bootstrap(
         policy=CostPolicy(alpha=2.5, beta=0.8, gamma=1.2, cargo="hazmat"))

 Author  : Team ICONIC (Team ID 119301) - Usha Martin University
 License : MIT
================================================================================
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import heapq
import io
import json
import math
import os
import platform
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple,
)

__version__ = "1.0.0"
__problem__ = "SIH26002"
__team__ = "Team ICONIC (119301)"
__all__ = [
    "HazardEngine", "HazardRiskModel", "RoadEdge", "WeatherState", "RoadGraph",
    "RouteResult", "CostPolicy", "SCENARIOS", "FEATURES", "RISK_BANDS",
    "build_mock_osm_graph", "build_historical_hazard_log", "load_geojson_graph",
    "load_hazard_csv", "dijkstra", "astar", "main",
]

# --------------------------------------------------------------------------- #
#  OPTIONAL DEPENDENCY DETECTION                                               #
# --------------------------------------------------------------------------- #
def _probe(module: str) -> Optional[Any]:
    """Import `module` if present, else return None.  Never raises."""
    try:
        return __import__(module)
    except Exception:                                    # pragma: no cover
        return None


_HAS = {
    "numpy":     _probe("numpy")     is not None,
    "pandas":    _probe("pandas")    is not None,
    "sklearn":   _probe("sklearn")   is not None,
    "xgboost":   _probe("xgboost")   is not None,
    "torch":     _probe("torch")     is not None,
    "networkx":  _probe("networkx")  is not None,
    "scipy":     _probe("scipy")     is not None,
}

BACKEND_ORDER = ("xgboost", "sklearn", "torch", "pure")


def available_backends() -> List[str]:
    """Backends usable in this interpreter, best-first, plus the always-on one."""
    out = [b for b in ("xgboost", "sklearn", "torch") if _HAS[b]]
    out.append("pure")
    return out


def resolve_backend(requested: str = "auto") -> str:
    """Map a user request onto a concrete backend name."""
    if requested == "auto":
        return available_backends()[0]
    if requested not in BACKEND_ORDER:
        raise ValueError(f"unknown backend '{requested}' (choose from {BACKEND_ORDER})")
    if requested != "pure" and not _HAS[requested]:
        raise RuntimeError(
            f"backend '{requested}' requested but the package is not installed. "
            f"Available: {', '.join(available_backends())}"
        )
    return requested


# --------------------------------------------------------------------------- #
#  PATHS & GLOBAL CONSTANTS                                                    #
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
OUT_DIR = os.path.join(HERE, "outputs")

DEFAULT_GRAPH_PATH = os.path.join(DATA_DIR, "mock_osm_graph.geojson")
DEFAULT_HAZARD_PATH = os.path.join(DATA_DIR, "historical_hazards_2023.csv")
DEFAULT_MODEL_PATH = os.path.join(OUT_DIR, "hazard_model.json")

EARTH_RADIUS_M = 6_371_008.8          # IUGG mean radius
DEFAULT_SEED = 26002                  # SIH problem-statement id as seed
V_REF = 10.0                          # m/s - unit normaliser for slope friction
CARGO_OVERHEAD_S = 60.0               # seconds of flat handling overhead / edge

# --------------------------------------------------------------------------- #
#  FEATURE SCHEMA                                                              #
# --------------------------------------------------------------------------- #
# The five canonical drivers from the problem statement, plus engineered terms
# (interactions & exposure) that a boosted-tree ensemble exploits.
FEATURES: Tuple[str, ...] = (
    "rain_mm_hr",       # instantaneous rainfall intensity at edge centroid
    "api_3d",           # 3-day Antecedent Precipitation Index (mm)
    "slope_deg",        # mean gradient of the edge (degrees)
    "soil_saturation",  # volumetric soil-water saturation, 0..1
    "hist_freq",        # historical hazard events / km / monsoon-season
    # --- engineered --------------------------------------------------------
    "slope_x_rain",     # orographic coupling: tan(slope) * rain
    "length_m",         # exposure length (longer edge = more exposure)
    "elev_band",        # normalised elevation (0 valley .. 1 ridge)
    "ndvi",             # vegetation / slope-stability proxy (0..1)
    "drainage",         # soil drainage class (0 impeded .. 1 free)
    "cut_slope",        # 1 if the road is a hillside bench-cut
    "sinuosity",        # plan curvature / hairpin proxy
)

FEATURE_UNITS: Dict[str, str] = {
    "rain_mm_hr": "mm/hr", "api_3d": "mm", "slope_deg": "deg",
    "soil_saturation": "0-1", "hist_freq": "events/km/season",
    "slope_x_rain": "mm/hr", "length_m": "m", "elev_band": "0-1",
    "ndvi": "0-1", "drainage": "0-1", "cut_slope": "0|1", "sinuosity": "ratio",
}

# Risk bands: (upper_bound_exclusive, band, colour, recommended action)
RISK_BANDS: Tuple[Tuple[float, str, str, str], ...] = (
    (0.15, "LOW",      "#2ecc71", "PROCEED - normal convoy discipline"),
    (0.35, "MODERATE", "#f1c40f", "MONITOR - advisory broadcast to fleet"),
    (0.60, "HIGH",     "#e67e22", "CAUTION - escort vehicle + reduced speed"),
    (0.80, "SEVERE",   "#e74c3c", "AVOID - reroute unless mission-critical"),
    (1.01, "CRITICAL", "#8e1b1b", "NO-GO - segment treated as blocked"),
)

# Weather scenarios: baseline rain (mm/hr), API-3d (mm), soil wetness bias,
# number of monsoon cells and their characteristic radius (km).
SCENARIOS: Dict[str, Dict[str, float]] = {
    "light":    {"rain": 2.5,  "api": 25.0,  "wet": 0.32, "cells": 3, "radius_km": 58.0},
    "moderate": {"rain": 12.0, "api": 85.0,  "wet": 0.52, "cells": 5, "radius_km": 50.0},
    "heavy":    {"rain": 35.0, "api": 190.0, "wet": 0.74, "cells": 6, "radius_km": 44.0},
    "extreme":  {"rain": 78.0, "api": 340.0, "wet": 0.91, "cells": 8, "radius_km": 40.0},
}
SCENARIO_ORDER = ("light", "moderate", "heavy", "extreme")

CARGO_PROFILES: Dict[str, float] = {
    "standard":   1.0,
    "perishable": 1.5,
    "medical":    1.75,
    "hazmat":     2.0,
    "fuel":       2.0,
}

# --------------------------------------------------------------------------- #
#  NORTH-EASTERN REGION GEOGRAPHY (real anchors for a plausible mock network)  #
# --------------------------------------------------------------------------- #
# name -> (lat, lon, elevation_m, state)
NER_CITIES: Dict[str, Tuple[float, float, float, str]] = {
    "Guwahati":       (26.1445, 91.7364,   55.0, "Assam"),
    "Tezpur":         (26.6311, 92.7986,   70.0, "Assam"),
    "Jorhat":         (26.7526, 94.2026,  118.0, "Assam"),
    "Dibrugarh":      (27.4728, 94.9120,  108.0, "Assam"),
    "Lumding":        (25.7499, 93.1780,  140.0, "Assam"),
    "Silchar":        (24.8333, 92.7789,   22.0, "Assam"),
    "Kokrajhar":      (26.4006, 90.2718,   40.0, "Assam"),
    "Dhubri":         (26.0210, 89.9839,   30.0, "Assam"),
    "Haflong":        (25.1780, 93.0260,  680.0, "Assam"),
    "Shillong":       (25.5788, 91.8933, 1520.0, "Meghalaya"),
    "Sonapur Pass":   (25.8600, 91.8700,  920.0, "Meghalaya"),
    "Jowai":          (25.4500, 92.2000, 1400.0, "Meghalaya"),
    "Tura":           (25.5180, 90.2201,  180.0, "Meghalaya"),
    "Nongstoin":      (25.5200, 91.2700, 1150.0, "Meghalaya"),
    "Itanagar":       (27.1000, 93.6200,  320.0, "Arunachal Pradesh"),
    "Pasighat":       (28.0667, 95.3333,  155.0, "Arunachal Pradesh"),
    "Ziro":           (27.6333, 93.8333, 1500.0, "Arunachal Pradesh"),
    "Kohima":         (25.6751, 94.1086, 1261.0, "Nagaland"),
    "Mokokchung":     (26.3333, 94.5167, 1325.0, "Nagaland"),
    "Dimapur":        (25.9167, 93.7500,  145.0, "Nagaland"),
    "Imphal":         (24.8170, 93.9368,  786.0, "Manipur"),
    "Churachandpur":  (24.3333, 93.9667,  900.0, "Manipur"),
    "Ukhrul":         (25.1167, 94.3667, 1662.0, "Manipur"),
    "Tamenglong":     (24.9833, 93.9833, 1250.0, "Manipur"),
    "Aizawl":         (23.7271, 92.7176, 1132.0, "Mizoram"),
    "Lunglei":        (22.8833, 92.7500,  720.0, "Mizoram"),
    "Agartala":       (23.8315, 91.2868,   14.0, "Tripura"),
    "Gangtok":        (27.3389, 88.6065, 1650.0, "Sikkim"),
}

# (from, to, highway, road_class, parallel_chains)
NER_CORRIDORS: Tuple[Tuple[str, str, str, str, int], ...] = (
    ("Guwahati",      "Sonapur Pass",  "NH-6",    "national", 2),
    ("Sonapur Pass",  "Shillong",      "NH-10",   "national", 2),
    ("Guwahati",      "Lumding",       "NH-27",   "national", 2),
    ("Lumding",       "Haflong",       "NH-27",   "national", 1),
    ("Haflong",       "Silchar",       "NH-27",   "national", 2),
    ("Guwahati",      "Tezpur",        "NH-27",   "national", 2),
    ("Tezpur",        "Jorhat",        "NH-27",   "national", 1),
    ("Jorhat",        "Dibrugarh",     "NH-37",   "national", 1),
    ("Dibrugarh",     "Pasighat",      "NH-515",  "national", 1),
    ("Guwahati",      "Kokrajhar",     "NH-27",   "national", 1),
    ("Kokrajhar",     "Dhubri",        "NH-31",   "national", 1),
    ("Dhubri",        "Tura",          "NH-127B", "national", 1),
    ("Tura",          "Nongstoin",     "NH-127B", "national", 1),
    ("Nongstoin",     "Shillong",      "NH-127B", "national", 1),
    ("Guwahati",      "Jowai",         "NH-206",  "national", 1),
    ("Jowai",         "Shillong",      "NH-206",  "state",    1),
    ("Silchar",       "Aizawl",        "NH-306",  "national", 2),
    ("Aizawl",        "Lunglei",       "NH-54",   "national", 1),
    ("Lunglei",       "Agartala",      "NH-54",   "national", 1),
    ("Silchar",       "Imphal",        "NH-37",   "national", 2),
    ("Imphal",        "Tamenglong",    "NH-127A", "national", 1),
    ("Imphal",        "Ukhrul",        "NH-102",  "national", 1),
    ("Imphal",        "Churachandpur", "NH-2",    "national", 1),
    ("Imphal",        "Kohima",        "NH-2",    "national", 2),
    ("Kohima",        "Dimapur",       "NH-29",   "national", 1),
    ("Dimapur",       "Mokokchung",    "NH-129",  "national", 1),
    ("Dimapur",       "Itanagar",      "NH-129",  "national", 1),
    ("Itanagar",      "Ziro",          "NH-13",   "state",    1),
    ("Pasighat",      "Itanagar",      "NH-13",   "national", 1),
    ("Tezpur",        "Ziro",          "NH-13",   "national", 1),
    ("Agartala",      "Silchar",       "NH-44",   "national", 1),
    ("Kohima",        "Gangtok",       "NH-29",   "state",    1),
    ("Dhubri",        "Gangtok",       "NH-27",   "national", 1),
)

SOIL_CLASSES: Dict[str, Tuple[float, float]] = {
    # name -> (drainage 0..1, NDVI bias)
    "laterite":     (0.62, 0.55),
    "alluvial":     (0.48, 0.72),
    "red_loam":     (0.55, 0.60),
    "clay_loam":    (0.31, 0.68),
    "sandy_loam":   (0.78, 0.42),
    "colluvium":    (0.40, 0.35),   # loose talus at the toe of hill cuttings
    "peat_lowland": (0.22, 0.80),   # Brahmaputra / Barak wetlands
}
SOIL_NAMES = tuple(SOIL_CLASSES.keys())

HAZARD_TYPES = ("landslide", "slope_failure", "waterlogging", "flash_flood",
                "debris_flow", "boulder_fall", "road_collapse", "embankment_washout")

HAZARD_SOURCES = ("BRO", "NHIDCL", "SDMA", "IMD", "field_report", "satellite_SAR",
                  "highway_patrol")

ROAD_CLASSES: Dict[str, Dict[str, Any]] = {
    "national": {"base_kmh": 48.0, "surface_mix": ("asphalt", 0.78, "gravel", 0.17, "kucha", 0.05)},
    "state":    {"base_kmh": 38.0, "surface_mix": ("asphalt", 0.55, "gravel", 0.32, "kucha", 0.13)},
    "district": {"base_kmh": 30.0, "surface_mix": ("asphalt", 0.32, "gravel", 0.40, "kucha", 0.28)},
}

# --------------------------------------------------------------------------- #
#  TINY NUMERICAL / GEO HELPERS  (stdlib only - no numpy required)             #
# --------------------------------------------------------------------------- #
def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


def sigmoid(z: float) -> float:
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def logit(p: float, eps: float = 1e-6) -> float:
    p = clamp(p, eps, 1.0 - eps)
    return math.log(p / (1.0 - p))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def interpolate_point(lat1: float, lon1: float, lat2: float, lon2: float,
                      t: float) -> Tuple[float, float]:
    """Linear (equirectangular) interpolation - accurate enough at NER scale."""
    return lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def offset_point(lat: float, lon: float, dist_m: float, brg: float) -> Tuple[float, float]:
    """Move `dist_m` metres from (lat, lon) along compass bearing `brg`."""
    br = math.radians(brg)
    d_over_r = dist_m / EARTH_RADIUS_M
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d_over_r) +
                   math.cos(p1) * math.sin(d_over_r) * math.cos(br))
    l2 = l1 + math.atan2(math.sin(br) * math.sin(d_over_r) * math.cos(p1),
                         math.cos(d_over_r) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def bbox_of(coords: Iterable[Sequence[float]]) -> Tuple[float, float, float, float]:
    lats = [c[1] for c in coords]
    lons = [c[0] for c in coords]
    return (min(lons), min(lats), max(lons), max(lats))


def sha1_of(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _stable_hash(s: str) -> int:
    """Process-independent 32-bit string hash (Python's hash() is randomised)."""
    return int(hashlib.sha1(s.encode("utf-8")).hexdigest()[:8], 16)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def quantile(sorted_xs: Sequence[float], q: float) -> float:
    if not sorted_xs:
        return 0.0
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    pos = q * (len(sorted_xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_xs) - 1)
    frac = pos - lo
    return sorted_xs[lo] * (1.0 - frac) + sorted_xs[hi] * frac


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    """Rank correlation in [-1, 1]; audits learned vs. true generative weights."""
    if len(a) != len(b) or len(a) < 2:
        return 0.0

    def ranks(v: Sequence[float]) -> List[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    ra, rb = ranks(a), ranks(b)
    ma, mb = mean(ra), mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den > 1e-12 else 0.0


# --------------------------------------------------------------------------- #
#  DATA MODEL                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class RoadEdge:
    """One directed road segment (OSM way chunk) of the routable graph."""

    segment_id: str
    u: str
    v: str
    coords: List[List[float]]          # GeoJSON order: [[lon, lat], ...]
    length_m: float
    elev_from: float
    elev_to: float
    slope_deg: float                   # effective terrain gradient (hazard driver)
    grade_pct: float = 0.0             # signed mean grade of the carriageway, %
    relief_m: float = 0.0              # peak-to-valley relief inside the segment
    highway: str = "unclassified"
    road_class: str = "district"
    surface: str = "asphalt"
    lanes: int = 2
    speed_kmh: float = 40.0
    soil_type: str = "laterite"
    drainage: float = 0.5
    ndvi: float = 0.5
    cut_slope: int = 0
    sinuosity: float = 1.0
    state: str = "Assam"
    district: str = "Kamrup"
    name: str = ""
    hist_freq: float = 0.0             # events / km / season (empirical base risk)

    # ---- populated at scoring time (never persisted as input data) --------
    lat: float = 0.0
    lon: float = 0.0
    elevation_m: float = 0.0
    p_disruption: float = 0.0
    risk_band: str = "LOW"
    transit_s: float = 0.0
    slope_friction_s: float = 0.0
    base_cost_s: float = 0.0
    cost_s: float = 0.0
    cost_s_soft: float = 0.0           # dynamic cost ignoring the hard block
    weather: Dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    def centroid(self) -> Tuple[float, float]:
        """Length-weighted centroid (falls back to polyline midpoint)."""
        if not self.coords:
            return (self.lat, self.lon)
        if len(self.coords) == 1:
            return (self.coords[0][0], self.coords[0][1])
        if len(self.coords) == 2:
            a, b = self.coords
            return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        # weighted by sub-segment length
        tot = 0.0
        clat = 0.0
        clon = 0.0
        for i in range(len(self.coords) - 1):
            (x1, y1), (x2, y2) = self.coords[i], self.coords[i + 1]
            w = haversine_m(y1, x1, y2, x2)
            tot += w
            clon += w * (x1 + x2) / 2.0
            clat += w * (y1 + y2) / 2.0
        if tot <= 1e-9:
            a, b = self.coords[0], self.coords[-1]
            return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        return (clon / tot, clat / tot)

    def resolve_geometry(self) -> None:
        """Fill centroid/elevation convenience fields from the polyline."""
        self.lon, self.lat = self.centroid()
        self.elevation_m = (self.elev_from + self.elev_to) / 2.0

    def other(self, node_id: str) -> str:
        return self.v if node_id == self.u else self.u

    # ------------------------------------------------------------------ #
    def to_properties(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p: Dict[str, Any] = {
            "segment_id": self.segment_id,
            "u": self.u, "v": self.v,
            "name": self.name, "highway": self.highway, "road_class": self.road_class,
            "surface": self.surface, "lanes": self.lanes,
            "length_m": round(self.length_m, 2),
            "elev_from": round(self.elev_from, 1),
            "elev_to": round(self.elev_to, 1),
            "elevation_m": round(self.elevation_m, 1),
            "slope_deg": round(self.slope_deg, 2),
            "grade_pct": round(self.grade_pct, 2),
            "relief_m": round(self.relief_m, 1),
            "sinuosity": round(self.sinuosity, 3),
            "speed_kmh": round(self.speed_kmh, 1),
            "soil_type": self.soil_type, "drainage": round(self.drainage, 3),
            "ndvi": round(self.ndvi, 3), "cut_slope": int(self.cut_slope),
            "state": self.state, "district": self.district,
            "lat": round(self.lat, 6), "lon": round(self.lon, 6),
            "hist_freq": round(self.hist_freq, 4),
        }
        if extra:
            p.update(extra)
        return p

    @staticmethod
    def from_properties(pr: Dict[str, Any], coords: List[List[float]]) -> "RoadEdge":
        def f(k: str, d: float = 0.0) -> float:
            try:
                return float(pr.get(k, d))
            except (TypeError, ValueError):
                return d

        e = RoadEdge(
            segment_id=str(pr.get("segment_id") or pr.get("id") or "SEG-?"),
            u=str(pr.get("u", "")), v=str(pr.get("v", "")),
            coords=coords,
            length_m=f("length_m"),
            elev_from=f("elev_from"), elev_to=f("elev_to"),
            slope_deg=f("slope_deg"),
            grade_pct=f("grade_pct"), relief_m=f("relief_m"),
            highway=str(pr.get("highway", "unclassified")),
            road_class=str(pr.get("road_class", "district")),
            surface=str(pr.get("surface", "asphalt")),
            lanes=int(f("lanes", 2)),
            speed_kmh=f("speed_kmh", 40.0),
            soil_type=str(pr.get("soil_type", "laterite")),
            drainage=f("drainage", 0.5), ndvi=f("ndvi", 0.5),
            cut_slope=int(f("cut_slope", 0)), sinuosity=f("sinuosity", 1.0),
            state=str(pr.get("state", "")), district=str(pr.get("district", "")),
            name=str(pr.get("name", "")), hist_freq=f("hist_freq", 0.0),
        )
        e.lat = f("lat"); e.lon = f("lon"); e.elevation_m = f("elevation_m")
        e.resolve_geometry()
        # a caller may have supplied a geometry-derived length
        if e.length_m <= 0.0:
            e.length_m = polyline_length_m(coords)
        return e

    def to_dict(self) -> Dict[str, Any]:
        d = self.to_properties()
        blocked = self.cost_s == math.inf
        d.update({
            "p_disruption": round(self.p_disruption, 5),
            "risk_band": self.risk_band,
            "transit_s": (None if blocked else round(self.transit_s, 2)),
            "slope_friction_s": round(self.slope_friction_s, 2),
            "base_cost_s": round(self.base_cost_s, 2),
            # strict JSON: an impassable segment carries null cost, never Infinity
            "cost_s": (None if blocked else round(self.cost_s, 2)),
            "cost_s_soft": round(self.cost_s_soft, 2),
            "impassable": blocked,
        })
        if self.weather:
            d["weather"] = {k: round(float(v), 4) for k, v in self.weather.items()}
        return d


def polyline_length_m(coords: Sequence[Sequence[float]]) -> float:
    return sum(
        haversine_m(coords[i][1], coords[i][0], coords[i + 1][1], coords[i + 1][0])
        for i in range(len(coords) - 1)
    )


@dataclass
class WeatherState:
    """A monsoon weather field evaluated per edge (or a single scalar state)."""

    scenario: str = "moderate"
    rain_mm_hr: float = 12.0
    rain_24h_mm: float = 60.0
    api_3d: float = 85.0
    soil_saturation: float = 0.5
    wind_kmh: float = 18.0
    generated_utc: str = ""
    per_edge: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def for_edge(self, edge: RoadEdge) -> Dict[str, float]:
        """Return the weather vector for `edge` (per-edge field if present)."""
        if self.per_edge and edge.segment_id in self.per_edge:
            return self.per_edge[edge.segment_id]
        return {
            "rain_mm_hr": self.rain_mm_hr,
            "rain_24h_mm": self.rain_24h_mm,
            "api_3d": self.api_3d,
            "soil_saturation": self.soil_saturation,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario,
            "rain_mm_hr": round(self.rain_mm_hr, 3),
            "rain_24h_mm": round(self.rain_24h_mm, 3),
            "api_3d": round(self.api_3d, 3),
            "soil_saturation": round(self.soil_saturation, 4),
            "wind_kmh": round(self.wind_kmh, 2),
            "generated_utc": self.generated_utc,
            "n_edges_with_local_field": len(self.per_edge),
        }


@dataclass
class RoadGraph:
    """Undirected routable road network (edges are traversable both ways)."""

    nodes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    edges: List[RoadEdge] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    _adj: Optional[Dict[str, List[Tuple[int, str]]]] = field(default=None, repr=False)

    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self.edges)

    def add_node(self, nid: str, lat: float, lon: float, elev_m: float = 0.0,
                 name: str = "", state: str = "") -> None:
        self.nodes[nid] = {"lat": lat, "lon": lon, "elev_m": elev_m,
                           "name": name, "state": state}

    def add_edge(self, edge: RoadEdge) -> None:
        edge.resolve_geometry()
        self.edges.append(edge)
        self._adj = None

    def adjacency(self) -> Dict[str, List[Tuple[int, str]]]:
        """node id -> [(edge_index, neighbour_id), ...]  (cached)."""
        if self._adj is None:
            adj: Dict[str, List[Tuple[int, str]]] = {n: [] for n in self.nodes}
            for i, e in enumerate(self.edges):
                adj.setdefault(e.u, []).append((i, e.v))
                adj.setdefault(e.v, []).append((i, e.u))
            self._adj = adj
        return self._adj

    # ------------------------------------------------------------------ #
    def total_length_km(self) -> float:
        return sum(e.length_m for e in self.edges) / 1000.0

    def bbox(self) -> Tuple[float, float, float, float]:
        if not self.nodes:
            return (0.0, 0.0, 0.0, 0.0)
        lons = [n["lon"] for n in self.nodes.values()]
        lats = [n["lat"] for n in self.nodes.values()]
        return (min(lons), min(lats), max(lons), max(lats))

    def node_by_name(self, name: str) -> Optional[str]:
        key = name.strip().lower()
        for nid, n in self.nodes.items():
            if (n.get("name") or "").strip().lower() == key:
                return nid
        return None

    def nearest_node(self, lat: float, lon: float) -> Optional[str]:
        best, best_d = None, float("inf")
        for nid, n in self.nodes.items():
            d = haversine_m(lat, lon, n["lat"], n["lon"])
            if d < best_d:
                best, best_d = nid, d
        return best

    def resolve_endpoint(self, ref: str) -> Optional[str]:
        """Accept a node id, a city name, or 'lat,lon'."""
        if ref in self.nodes:
            return ref
        nid = self.node_by_name(ref)
        if nid:
            return nid
        if "," in ref:
            try:
                la, lo = (float(x) for x in ref.split(",", 1))
                return self.nearest_node(la, lo)
            except ValueError:
                return None
        return None

    def named_nodes(self) -> List[str]:
        return sorted(nid for nid, n in self.nodes.items() if n.get("name"))

    # ------------------------------------------------------------------ #
    def to_geojson(self, extra_meta: Optional[Dict[str, Any]] = None,
                   include_scores: bool = False) -> Dict[str, Any]:
        features = []
        for e in self.edges:
            props = e.to_dict() if include_scores else e.to_properties()
            features.append({
                "type": "Feature",
                "id": e.segment_id,
                "geometry": {"type": "LineString", "coordinates": e.coords},
                "properties": props,
            })
        meta = dict(self.metadata)
        meta.update({
            "generator": f"SIH26002 Hazard Prediction Engine v{__version__}",
            "generated_utc": utcnow_iso(),
            "n_nodes": len(self.nodes),
            "n_edges": len(self.edges),
            "total_length_km": round(self.total_length_km(), 2),
            "bbox": [round(x, 5) for x in self.bbox()],
            # compact node table: id -> [lat, lon, elev_m, name, state]
            "nodes": {
                nid: [round(n["lat"], 6), round(n["lon"], 6), round(n["elev_m"], 1),
                      n.get("name", ""), n.get("state", "")]
                for nid, n in self.nodes.items()
            },
        })
        if extra_meta:
            meta.update(extra_meta)
        return {
            "type": "FeatureCollection",
            "metadata": meta,
            "features": features,
        }

    @staticmethod
    def from_geojson(obj: Dict[str, Any]) -> "RoadGraph":
        g = RoadGraph()
        meta = obj.get("metadata") or {}
        g.metadata = {k: v for k, v in meta.items() if k != "nodes"}

        node_table = meta.get("nodes") or {}
        for nid, row in node_table.items():
            g.add_node(nid, float(row[0]), float(row[1]),
                       elev_m=float(row[2]) if len(row) > 2 else 0.0,
                       name=str(row[3]) if len(row) > 3 else "",
                       state=str(row[4]) if len(row) > 4 else "")

        for feat in obj.get("features", []):
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if geom.get("type") == "MultiLineString":
                coords = [c for part in coords for c in part]
            if len(coords) < 2:
                continue
            pr = dict(feat.get("properties") or {})
            e = RoadEdge.from_properties(pr, coords)
            if not e.u or not e.v:
                e.u = f"N::{coords[0][1]:.6f},{coords[0][0]:.6f}"
                e.v = f"N::{coords[-1][1]:.6f},{coords[-1][0]:.6f}"
            for nid, c in ((e.u, coords[0]), (e.v, coords[-1])):
                if nid not in g.nodes:
                    g.add_node(nid, c[1], c[0],
                               elev_m=e.elev_from if nid == e.u else e.elev_to)
            g.add_edge(e)
        return g

    def to_networkx(self) -> Any:
        """Optional bridge for NetworkX users (returns nx.Graph with cost_s)."""
        if not _HAS["networkx"]:
            raise RuntimeError("networkx is not installed")
        nx = _probe("networkx")
        G = nx.Graph()
        for nid, n in self.nodes.items():
            G.add_node(nid, **n)
        for e in self.edges:
            G.add_edge(e.u, e.v, segment_id=e.segment_id, length_m=e.length_m,
                       transit_s=e.transit_s, cost_s=e.cost_s or e.base_cost_s,
                       p_disruption=e.p_disruption, risk_band=e.risk_band,
                       highway=e.highway)
        return G


# --------------------------------------------------------------------------- #
#  SYNTHETIC TERRAIN FIELD                                                     #
# --------------------------------------------------------------------------- #
# Anisotropic Gaussian terrain features for the North-Eastern Region, in metres.
# (lat, lon, amplitude, sigma_lat_deg, sigma_lon_deg) - river valleys are given a
# long longitudinal sigma so they read as trenches rather than as round bowls.
TERRAIN_BUMPS: Tuple[Tuple[float, float, float, float, float], ...] = (
    (25.45, 92.10,  1300.0, 0.42, 0.62),   # Shillong / Khasi-Jaintia plateau
    (25.55, 90.35,   620.0, 0.30, 0.35),   # Garo Hills (Tura)
    (26.10, 94.30,  1200.0, 0.62, 0.42),   # Naga Hills
    (23.30, 92.75,   980.0, 0.72, 0.42),   # Mizo / Lushai Hills
    (27.75, 93.40,  1550.0, 0.55, 1.35),   # Arunachal Himalaya
    (24.90, 94.10,   900.0, 0.55, 0.50),   # Manipur hills rim (Ukhrul / Tamenglong)
    (25.15, 93.25,   480.0, 0.35, 0.45),   # North Cachar / Dima Hasao hills
    (26.55, 92.60,  -420.0, 0.34, 2.60),   # Brahmaputra valley trench (NW-2)
    (24.80, 92.80,  -150.0, 0.35, 0.60),   # Barak valley (Silchar plain)
    (24.78, 93.95,  -260.0, 0.28, 0.28),   # Imphal valley
    (23.80, 91.40,  -150.0, 0.55, 0.55),   # Tripura / Agartala plain
    (22.95, 92.60,   -90.0, 0.45, 0.50),   # southern Mizoram lowland
)
TERRAIN_BASE_M = 150.0


class TerrainModel:
    """
    Deterministic elevation + terrain-attribute field for the NER.

    Two spatial scales are superposed:

      * `smooth`  - broad orography (Shillong Plateau, Naga / Mizo Hills,
        Arunachal Himalaya), river-valley depressions (Brahmaputra, Barak) and
        low-frequency ridge harmonics, anchor-corrected so that known city
        elevations are reproduced;
      * `fine`    - km-scale ruggedness (4 plane-wave harmonics, 1.3-5.2 km
        wavelength) whose amplitude is modulated by how mountainous the smooth
        field already is.  Flat Brahmaputra alluvium stays flat, Khasi / Naga
        hill terrain gets 100-500 m of local relief per few kilometres, which is
        what actually drives cut-slope failure.
    """

    KM_PER_DEG_LAT = 110.9
    COS_LAT_REF = 0.90                 # ~ cos(26 deg N), NER mid-latitude
    IDW_EPS_KM = 6.0                   # inverse-distance softening
    IDW_POWER = 4.0                    # sharp: cities pin their own neighbourhood
    IDW_FAR_KM = 150.0                 # background pseudo-anchor at this range
    IDW_DAMP_KM = 12.0                 # ruggedness admitted at 50% beyond this
    IDW_PRUNE_KM2 = 320.0 ** 2         # beyond this an anchor is negligible

    def __init__(self, seed: int = DEFAULT_SEED,
                 anchors: Optional[Dict[str, Tuple[float, float, float, str]]] = None):
        self.seed = seed
        rng = random.Random(seed ^ 0x5EED)
        self.anchors = list((anchors or NER_CITIES).values())
        # low-frequency residual ridge roughness (amplitude m, cycles, phase)
        self.ridges = [(rng.uniform(35.0, 120.0),
                        rng.uniform(2.0, 7.0), rng.uniform(2.0, 7.0),
                        rng.uniform(0.0, math.tau)) for _ in range(3)]
        # km-scale plane waves: (amplitude_m, wavelength_km, azimuth_deg, phase)
        self.fine = [(amp, wl, rng.uniform(0.0, 360.0), rng.uniform(0.0, math.tau))
                     for amp, wl in ((130.0, 5.2), (95.0, 3.4), (62.0, 2.1), (40.0, 1.3))]
        self._idw_eps2 = self.IDW_EPS_KM ** 2
        self._idw_w0 = 1.0 / ((self.IDW_FAR_KM ** 2 + self._idw_eps2)
                              ** (self.IDW_POWER / 2.0))
        # ruggedness damping reference: at this range from a surveyed city the
        # stochastic fine-scale term is admitted at 50% amplitude.
        self._idw_damp_ref = 1.0 / ((self.IDW_DAMP_KM ** 2 + self._idw_eps2)
                                    ** (self.IDW_POWER / 2.0))
        self._residual = [a[2] - self._smooth(a[0], a[1]) for a in self.anchors]
        self._mean_residual = mean(self._residual) if self._residual else 0.0

    # ------------------------------------------------------------------ #
    def _smooth(self, lat: float, lon: float) -> float:
        h = TERRAIN_BASE_M
        for blat, blon, amp, sla, slo in TERRAIN_BUMPS:
            d2 = ((lat - blat) / sla) ** 2 + ((lon - blon) / slo) ** 2
            h += amp * math.exp(-0.5 * d2)
        for amp, fa, fb, ph in self.ridges:
            h += amp * math.sin(fa * math.radians(lat) * 22.0 +
                                fb * math.radians(lon) * 22.0 + ph)
        return h

    def _anchor_blend(self, lat: float, lon: float) -> Tuple[float, float]:
        """
        Sharp inverse-distance blend of the anchor residuals.

        w_i = 1 / (d_i^2 + eps^2)^2 pins each surveyed city elevation almost
        exactly (error < 5 m within a few km) while a far-field pseudo-anchor
        carrying the regional mean residual keeps un-surveyed terrain sane.

        Returns (correction_m, ruggedness_damping).  The damping factor goes to
        ~0 at a surveyed point and to ~1 beyond ~25 km, so the stochastic
        km-scale ruggedness never corrupts a known city elevation.
        """
        kmlon = self.KM_PER_DEG_LAT * max(0.2, math.cos(math.radians(lat)))
        num = self._mean_residual * self._idw_w0
        den = self._idw_w0
        wmax = 0.0
        p2 = self.IDW_POWER / 2.0
        e2 = self._idw_eps2
        for (alat, alon, _aelv, _st), res in zip(self.anchors, self._residual):
            dy = (lat - alat) * self.KM_PER_DEG_LAT
            dx = (lon - alon) * kmlon
            d2 = dy * dy + dx * dx
            if d2 > self.IDW_PRUNE_KM2:
                continue
            w = 1.0 / ((d2 + e2) ** p2)
            if w > wmax:
                wmax = w
            num += w * res
            den += w
        damp = 1.0 - (wmax / (wmax + self._idw_damp_ref)) if wmax > 0.0 else 1.0
        return num / den, damp

    def _anchor_correction(self, lat: float, lon: float) -> float:
        return self._anchor_blend(lat, lon)[0]

    def ruggedness(self, smooth_h: float) -> float:
        """0.10 in the alluvial plains -> 1.45 on the high ridges."""
        return clamp((smooth_h - 100.0) / 800.0, 0.10, 1.45)

    def _fine(self, lat: float, lon: float, rug: float) -> float:
        if rug <= 0.02:
            return 0.0
        x = lat * self.KM_PER_DEG_LAT
        y = lon * self.KM_PER_DEG_LAT * self.COS_LAT_REF
        h = 0.0
        for amp, wl, az, ph in self.fine:
            a = math.radians(az)
            d = x * math.sin(a) + y * math.cos(a)
            h += amp * math.sin(math.tau * d / wl + ph)
        return h * rug

    def smooth_elevation(self, lat: float, lon: float) -> float:
        """Regional orography without km-scale ruggedness (used for banding)."""
        return max(2.0, self._smooth(lat, lon) + self._anchor_correction(lat, lon))

    def elevation(self, lat: float, lon: float) -> float:
        """Full elevation in metres: orography + anchor correction + ruggedness."""
        corr, damp = self._anchor_blend(lat, lon)
        s = self._smooth(lat, lon) + corr
        return max(2.0, s + damp * self._fine(lat, lon, self.ruggedness(s)))

    def profile(self, lat1: float, lon1: float, lat2: float, lon2: float,
                spacing_m: float = 400.0, min_samples: int = 5
                ) -> Tuple[List[float], float]:
        """(elevations at ~`spacing_m` stations along the segment, station spacing)."""
        horiz = max(1.0, haversine_m(lat1, lon1, lat2, lon2))
        n = max(min_samples, int(round(horiz / spacing_m)))
        hs = [self.elevation(*interpolate_point(lat1, lon1, lat2, lon2, i / n))
              for i in range(n + 1)]
        return hs, horiz / n

    def relief_m(self, lat1: float, lon1: float, lat2: float, lon2: float,
                 samples: int = 8) -> float:
        """Peak-to-valley elevation range sampled along a segment."""
        hs = [self.elevation(*interpolate_point(lat1, lon1, lat2, lon2, i / samples))
              for i in range(samples + 1)]
        return max(hs) - min(hs)

    def slope_of(self, lat1: float, lon1: float, h1: float,
                 lat2: float, lon2: float, h2: float) -> float:
        """Mean gradient in degrees between two survey points."""
        horiz = haversine_m(lat1, lon1, lat2, lon2)
        if horiz < 1.0:
            return 0.0
        return math.degrees(math.atan(abs(h2 - h1) / horiz))

    def effective_slope(self, lat1: float, lon1: float, h1: float,
                        lat2: float, lon2: float, h2: float,
                        spacing_m: float = 400.0, q: float = 0.85
                        ) -> Tuple[float, float]:
        """
        (effective_slope_deg, relief_m) for a road segment.

        The hazard-relevant gradient of a segment is *not* its endpoint mean
        grade: a 5 km bench-cut can average 2% and still carry a 25% cut face
        halfway along.  We therefore survey the longitudinal profile at ~400 m
        stations and take the steeper of

            (a) the mean endpoint grade, and
            (b) the 85th-percentile station-to-station gradient,

        i.e. the `maxslope` surrogate used in landslide-susceptibility mapping.
        A high percentile rather than the raw maximum keeps the metric robust to
        a single spike while still catching sustained steep cuttings.
        """
        horiz = max(1.0, haversine_m(lat1, lon1, lat2, lon2))
        hs, step = self.profile(lat1, lon1, lat2, lon2, spacing_m=spacing_m)
        hs[0], hs[-1] = h1, h2                 # honour surveyed node elevations
        relief = max(hs) - min(hs)
        grade = abs(h2 - h1) / horiz
        windows = sorted(abs(hs[i + 1] - hs[i]) / step for i in range(len(hs) - 1))
        steep = quantile(windows, q)
        return math.degrees(math.atan(max(grade, steep))), relief


    def soil_at(self, lat: float, lon: float, elev: float, slope_deg: float,
                rng: random.Random) -> str:
        """Sample a soil class conditioned on elevation and gradient."""
        if elev < 60.0 and slope_deg < 3.0:
            weights = (("peat_lowland", 0.42), ("alluvial", 0.48), ("clay_loam", 0.10))
        elif elev < 250.0:
            weights = (("alluvial", 0.44), ("clay_loam", 0.22), ("laterite", 0.18),
                       ("sandy_loam", 0.16))
        elif elev < 800.0:
            weights = (("laterite", 0.36), ("red_loam", 0.26), ("clay_loam", 0.18),
                       ("colluvium", 0.20))
        else:
            weights = (("colluvium", 0.44), ("red_loam", 0.26), ("laterite", 0.18),
                       ("sandy_loam", 0.12))
        if slope_deg > 14.0:
            weights = tuple((n, w * (1.9 if n == "colluvium" else 0.85)) for n, w in weights)
        return _weighted_choice(rng, weights)

    def ndvi_at(self, soil: str, elev: float, slope_deg: float,
                rng: random.Random) -> float:
        bias = SOIL_CLASSES.get(soil, (0.5, 0.55))[1]
        v = bias - 0.10 * (elev / 1600.0) - 0.16 * (slope_deg / 30.0)
        return round(clamp(v + rng.gauss(0.0, 0.05), 0.05, 0.95), 3)


def _weighted_choice(rng: random.Random,
                     weights: Sequence[Tuple[str, float]]) -> str:
    tot = sum(w for _, w in weights)
    x = rng.random() * tot
    acc = 0.0
    for name, w in weights:
        acc += w
        if x <= acc:
            return name
    return weights[-1][0]


# --------------------------------------------------------------------------- #
#  MOCK OSM GRAPH GENERATOR                                                    #
# --------------------------------------------------------------------------- #
def _solve_edge_length(corridor_km: Sequence[float], target_edges: int) -> float:
    """Bisect a target edge length (km) so total edges ~= target_edges."""
    lo, hi = 0.15, 40.0
    def count(tl: float) -> int:
        return sum(max(2, int(round(L / tl))) for L in corridor_km)
    if count(hi) >= target_edges:
        return hi
    if count(lo) <= target_edges:
        return lo
    for _ in range(60):
        mid = math.sqrt(lo * hi)
        if count(mid) > target_edges:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


def build_mock_osm_graph(target_edges: int = 1000, seed: int = DEFAULT_SEED,
                         roads: Sequence[Any] = NER_CORRIDORS,
                         cities: Optional[Dict[str, Tuple[float, float, float, str]]] = None
                         ) -> RoadGraph:
    """
    Synthesise a plausible OpenStreetMap-style road network for the 8 North
    Eastern states: real city anchors, real national-highway corridors, parallel
    alternative chains (so that rerouting is actually possible), cross-links,
    terrain-derived gradients, surfaces, soils and free-flow speeds.

    Fully deterministic for a given (target_edges, seed).
    """
    cities = cities or NER_CITIES
    rng = random.Random(seed)
    terrain = TerrainModel(seed=seed, anchors=cities)

    # ---- pass 1: corridor geometry lengths --------------------------------
    seg_km: List[Tuple[Any, float]] = []
    for (a, b, hw, cls, npar) in roads:
        if a not in cities or b not in cities:
            continue
        la1, lo1 = cities[a][0], cities[a][1]
        la2, lo2 = cities[b][0], cities[b][1]
        d = haversine_m(la1, lo1, la2, lo2) / 1000.0
        road_km = d * rng.uniform(1.14, 1.32)          # real roads bow around hills
        seg_km.append(((a, b, hw, cls, npar), road_km * max(1, npar)))
    if not seg_km:
        raise ValueError("no valid corridors")
    edge_km = _solve_edge_length([k for _, k in seg_km], max(20, target_edges))

    graph = RoadGraph(metadata={
        "problem_id": __problem__,
        "title": "Mock OSM road graph - North Eastern Region (synthetic)",
        "seed": seed,
        "target_edges": target_edges,
        "crs": "EPSG:4326",
        "note": "Synthetic but georeferenced to real NER cities and highways.",
    })

    # ---- anchor nodes ------------------------------------------------------
    anchor_id: Dict[str, str] = {}
    for name, (lat, lon, elev, state) in cities.items():
        nid = f"N::{name}"
        anchor_id[name] = nid
        # the anchor-corrected field reproduces the surveyed elevation to <15 m,
        # so node heights and sampled profiles stay mutually consistent
        graph.add_node(nid, lat, lon, elev_m=round(terrain.elevation(lat, lon), 1),
                       name=name, state=state)
    graph.metadata["surveyed_elevation_m"] = {n: c[2] for n, c in cities.items()}

    seg_counter = 0
    connector_candidates: List[Tuple[str, float, float, float, str]] = []

    # ---- pass 2: build chains ---------------------------------------------
    for ci, ((a, b, hw, cls, npar), _km) in enumerate(seg_km):
        la1, lo1, ea1, st1 = cities[a]
        la2, lo2, ea2, st2 = cities[b]
        straight_m = haversine_m(la1, lo1, la2, lo2)
        n_seg = max(2, int(round((_km / max(1, npar)) / edge_km)))
        brg = bearing_deg(la1, lo1, la2, lo2)

        for p in range(npar):
            # Chain 0 is the surveyed alignment (it still meanders around hills);
            # chain 1+ are alternative alignments thrown well clear of it so they
            # sample genuinely different weather / terrain - that is what makes
            # risk-aware rerouting possible at all.
            bow_sign = 1.0 if p % 2 == 0 else -1.0
            if p == 0:
                bow_amp = rng.uniform(2_200.0, 6_500.0) * bow_sign
            else:
                bow_amp = rng.uniform(11_000.0, 26_000.0) * bow_sign
            ph2, ph3 = rng.uniform(0.0, math.tau), rng.uniform(0.0, math.tau)
            chain_nodes: List[Tuple[str, float, float, float]] = []
            for i in range(n_seg + 1):
                t = i / n_seg
                lat, lon = interpolate_point(la1, lo1, la2, lo2, t)
                off = (bow_amp * math.sin(math.pi * t)
                       + 0.34 * bow_amp * math.sin(2.0 * math.pi * t + ph2)
                       + 0.18 * bow_amp * math.sin(3.0 * math.pi * t + ph3))
                if abs(off) > 1.0:
                    lat, lon = offset_point(lat, lon, off, (brg + 90.0) % 360.0)
                if i == 0:
                    nid, elev = anchor_id[a], graph.nodes[anchor_id[a]]["elev_m"]
                elif i == n_seg:
                    nid, elev = anchor_id[b], graph.nodes[anchor_id[b]]["elev_m"]
                else:
                    nid = f"N::c{ci:02d}p{p}_{i:03d}"
                    elev = terrain.elevation(lat, lon)
                    if nid not in graph.nodes:
                        graph.add_node(nid, lat, lon, elev_m=elev, state=(st1 if t < 0.5 else st2))
                chain_nodes.append((nid, lat, lon, elev))

            for i in range(n_seg):
                (nu, lau, lou, hu), (nv, lav, lov, hv) = chain_nodes[i], chain_nodes[i + 1]
                mid_lat, mid_lon = interpolate_point(lau, lou, lav, lov, 0.5)
                chord = max(1.0, haversine_m(lau, lou, lav, lov))
                slope, relief = terrain.effective_slope(lau, lou, hu, lav, lov, hv)
                grade = 100.0 * (hv - hu) / chord
                # switchback jitter grows with gradient -> sinuosity > 1
                jitter = rng.uniform(0.0, 1.0) * (60.0 + 26.0 * slope)
                mj_lat, mj_lon = offset_point(mid_lat, mid_lon, jitter,
                                              rng.uniform(0.0, 360.0))
                coords = [[round(lou, 6), round(lau, 6)],
                          [round(mj_lon, 6), round(mj_lat, 6)],
                          [round(lov, 6), round(lav, 6)]]
                length = polyline_length_m(coords)
                sinuosity = clamp(length / chord, 1.0, 1.9)
                soil = terrain.soil_at(mid_lat, mid_lon, (hu + hv) / 2.0, slope, rng)
                ndvi = terrain.ndvi_at(soil, (hu + hv) / 2.0, slope, rng)
                surf_mix = ROAD_CLASSES[cls]["surface_mix"]
                surface = _weighted_choice(
                    rng, ((surf_mix[0], surf_mix[1]), (surf_mix[2], surf_mix[3]),
                          (surf_mix[4], surf_mix[5])))
                if slope > 12.0 and rng.random() < 0.35:
                    surface = "gravel"
                speed = _free_flow_kmh(cls, surface, slope, sinuosity)
                cut = 1 if (slope >= 8.0 and (hu + hv) / 2.0 > 220.0 and rng.random() < 0.88) else 0
                seg_counter += 1
                e = RoadEdge(
                    segment_id=f"SEG-{seg_counter:05d}", u=nu, v=nv, coords=coords,
                    length_m=round(length, 2), elev_from=round(hu, 1), elev_to=round(hv, 1),
                    slope_deg=round(slope, 2), grade_pct=round(grade, 2),
                    relief_m=round(relief, 1),
                    highway=hw, road_class=cls, surface=surface,
                    lanes=2 if cls == "national" else (2 if rng.random() < 0.5 else 1),
                    speed_kmh=round(speed, 1), soil_type=soil,
                    drainage=round(SOIL_CLASSES[soil][0], 3), ndvi=ndvi,
                    cut_slope=cut, sinuosity=round(sinuosity, 3),
                    state=(st1 if i < n_seg / 2 else st2),
                    district=(a if i < n_seg / 2 else b),
                    name=f"{hw} {a}-{b}" + (f" (alt {p + 1})" if p else ""),
                )
                graph.add_edge(e)
                if npar > 1 and p == 1 and 0 < i < n_seg:
                    connector_candidates.append((nu, mid_lat, mid_lon, hv, cls))

    # ---- pass 3: cross-links between parallel chains -----------------------
    # Real terrain has minor roads tying a bypass back to the main highway; these
    # are what make risk-aware rerouting feasible.
    added = 0
    for (nid, lat, lon, _hv, cls) in connector_candidates:
        best, best_d = None, float("inf")
        for other, on in graph.nodes.items():
            if other == nid or on.get("name"):
                continue
            if not other.startswith("N::c") or "p0_" not in other:
                continue
            d = haversine_m(lat, lon, on["lat"], on["lon"])
            if d < best_d:
                best, best_d = other, d
        if best is None or not (400.0 < best_d < 14_000.0):
            continue
        nu, nv = nid, best
        a, b = graph.nodes[nu], graph.nodes[nv]
        coords = [[round(a["lon"], 6), round(a["lat"], 6)],
                  [round(b["lon"], 6), round(b["lat"], 6)]]
        length = polyline_length_m(coords)
        chord = max(1.0, length)
        slope, relief = terrain.effective_slope(a["lat"], a["lon"], a["elev_m"],
                                                b["lat"], b["lon"], b["elev_m"])
        grade = 100.0 * (b["elev_m"] - a["elev_m"]) / chord
        soil = terrain.soil_at(a["lat"], a["lon"], a["elev_m"], slope, rng)
        seg_counter += 1
        graph.add_edge(RoadEdge(
            segment_id=f"SEG-{seg_counter:05d}", u=nu, v=nv, coords=coords,
            length_m=round(length, 2), elev_from=round(a["elev_m"], 1),
            elev_to=round(b["elev_m"], 1), slope_deg=round(slope, 2),
            grade_pct=round(grade, 2), relief_m=round(relief, 1),
            highway="district connector", road_class="district",
            surface=_weighted_choice(rng, (("asphalt", 0.3), ("gravel", 0.5), ("kucha", 0.2))),
            lanes=1, speed_kmh=round(_free_flow_kmh("district", "gravel", slope, 1.15), 1),
            soil_type=soil, drainage=round(SOIL_CLASSES[soil][0], 3),
            ndvi=terrain.ndvi_at(soil, a["elev_m"], slope, rng),
            cut_slope=1 if slope >= 9.0 else 0, sinuosity=round(rng.uniform(1.05, 1.35), 3),
            state=a.get("state", ""), district="connector", name="cross-link",
        ))
        added += 1
        if added >= max(6, len(connector_candidates)):
            break

    graph.metadata["n_cross_links"] = added
    graph.metadata["edge_length_target_km"] = round(edge_km, 3)
    graph.metadata["sha1"] = sha1_of([e.segment_id for e in graph.edges])
    return graph


def _free_flow_kmh(road_class: str, surface: str, slope_deg: float,
                   sinuosity: float) -> float:
    """Empirical free-flow speed on hill terrain (IRC-SP:48 style derating)."""
    base = ROAD_CLASSES.get(road_class, ROAD_CLASSES["district"])["base_kmh"]
    surf_factor = {"asphalt": 1.00, "gravel": 0.78, "kucha": 0.56}.get(surface, 0.8)
    slope_factor = max(0.34, 1.0 - 0.0225 * slope_deg)
    sin_factor = clamp(1.0 / max(1.0, sinuosity) ** 0.65, 0.6, 1.0)
    return max(12.0, base * surf_factor * slope_factor * sin_factor)


# --------------------------------------------------------------------------- #
#  HISTORICAL HAZARD LOG  (previous-year learning signal)                      #
# --------------------------------------------------------------------------- #
# Ground-truth data-generating process (DGP).  The synthetic 2023 monsoon log is
# produced from an explicit physical/logistic model so that the module can be
# validated against a known ground truth (see `--audit-dgp`).  Real IMD / BRO /
# NHIDCL logs that follow the same CSV schema drop in with no code change.
DGP: Dict[str, float] = {
    "intercept":     -4.20,
    "rain":           0.0240,   # per mm/hr
    "api3":           0.0062,   # per mm of antecedent precipitation
    "slope":          0.0850,   # per degree of gradient
    "soil":           1.6000,   # per unit soil saturation
    "hist":           1.3500,   # per historical event / km / season (moisture-gated)
    "rain_x_slope":   0.9000,   # orographic coupling  tan(slope) * rain / 10
    "cut_x_soil":     0.5500,   # bench-cut + saturation -> shear failure
    "ndvi":          -0.5500,   # root cohesion is protective
    "drainage":      -0.4000,   # free drainage is protective
    "length":         0.1600,   # per km of exposure
    "noise_sd":       0.5500,   # unobserved geology / drainage-blockage noise
}

SEASON_PHASES: Tuple[Tuple[str, int, int, float], ...] = (
    # name, month_start, month_end, intensity multiplier
    ("pre_monsoon", 5,  5,  0.35),
    ("onset",       6,  6,  0.85),
    ("peak",        7,  8,  1.00),
    ("late",        9,  9,  0.78),
    ("post",        10, 10, 0.30),
)


def _phase_for(month: int) -> Tuple[str, float]:
    for name, m0, m1, mult in SEASON_PHASES:
        if m0 <= month <= m1:
            return name, mult
    return "post", 0.30


def true_disruption_logit(row: Dict[str, float],
                          intercept: Optional[float] = None) -> float:
    """
    Ground-truth logit of the synthetic DGP (auditing / diagnostics only).

    The historical-frequency term is moisture-gated: susceptibility sets the
    ceiling, antecedent wetness decides how much of it is realised.  A dry
    bench-cut with a bad history is not about to fail; the same cut at 0.8
    saturation is.
    """
    slope = float(row.get("slope_deg", 0.0))
    rain = float(row.get("rain_mm_hr", 0.0))
    return (
        (DGP["intercept"] if intercept is None else float(intercept))
        + DGP["rain"] * rain
        + DGP["api3"] * float(row.get("api_3d_mm", 0.0))
        + DGP["slope"] * slope
        + DGP["soil"] * float(row.get("soil_saturation", 0.0))
        + DGP["hist"] * float(row.get("hist_freq_per_km", 0.0))
        * (0.30 + 1.15 * float(row.get("soil_saturation", 0.0)))
        + DGP["rain_x_slope"] * (math.tan(math.radians(slope)) * rain / 10.0)
        + DGP["cut_x_soil"] * float(row.get("cut_slope", 0.0)) * float(row.get("soil_saturation", 0.0))
        + DGP["ndvi"] * float(row.get("ndvi", 0.5))
        + DGP["drainage"] * float(row.get("drainage", 0.5))
        + DGP["length"] * (float(row.get("length_m", 1000.0)) / 1000.0)
    )


def _sample_weather(rng: random.Random, edge: RoadEdge, phase_mult: float,
                    cell_exposure: float) -> Dict[str, float]:
    """Jointly plausible rain / API-3d / soil-saturation triple for one day."""
    orographic = 1.0 + 0.022 * edge.slope_deg + 0.12 * (edge.elevation_m / 1600.0)
    shape = max(0.35, 0.55 * phase_mult * cell_exposure * orographic)
    rain = rng.gammavariate(shape, 15.5) * (1.0 if rng.random() > 0.18 else 2.4)
    rain = clamp(rain, 0.0, 220.0)
    if rng.random() < 0.24:                              # dry spell
        rain *= rng.uniform(0.0, 0.12)
    rain_24h = rain * rng.uniform(3.2, 9.5) + rng.gammavariate(0.9, 6.0 * phase_mult)
    api_3d = rain_24h * rng.uniform(1.7, 3.4) + rng.gammavariate(1.4, 18.0)
    retention = (55.0 + 210.0 * (1.0 - edge.drainage)) * max(0.45, 1.0 - 0.020 * edge.slope_deg)
    sat = (1.0 - math.exp(-max(api_3d, 0.0) / max(retention, 20.0)))
    sat = clamp(sat * (0.62 + 0.42 * phase_mult) + rng.gauss(0.0, 0.045), 0.02, 0.99)
    return {
        "rain_mm_hr": round(rain, 2),
        "rain_24h_mm": round(rain_24h, 1),
        "api_3d_mm": round(api_3d, 1),
        "soil_saturation": round(sat, 4),
    }


def build_historical_hazard_log(graph: RoadGraph, seed: int = DEFAULT_SEED,
                                target_positive_rate: float = 0.145,
                                obs_per_segment: Tuple[int, int] = (3, 6),
                                autotune: bool = True) -> List[Dict[str, Any]]:
    """
    Generate `historical_hazards_2023.csv` content: one row per (segment,
    observation day) over the 2023 South-West monsoon, labelled with whether the
    segment was disrupted (blocked / closed) that day.

    `hist_freq_per_km` is the *previous* season's (2022) empirical hazard rate,
    drawn from a latent terrain susceptibility so that it is informative but not
    a copy of the label.  The intercept of the DGP is auto-tuned so the realised
    positive rate matches `target_positive_rate` (typical NER monsoon disruption
    base rate).
    """
    rng = random.Random(seed ^ 0xC0FFEE)
    edges = graph.edges
    if not edges:
        raise ValueError("graph has no edges")

    # --- latent per-segment susceptibility (2022 season) --------------------
    suscept: Dict[str, float] = {}
    hist_freq: Dict[str, float] = {}
    for e in edges:
        s = (0.52 * min(1.6, e.slope_deg / 14.0)
             + 0.24 * e.cut_slope
             + 0.22 * (1.0 - e.ndvi)
             + 0.16 * (1.0 - e.drainage)
             + 0.10 * min(1.0, e.elevation_m / 1500.0)
             + rng.gauss(0.0, 0.16))
        s = max(0.02, s)
        suscept[e.segment_id] = s
        per_km = s * 3.4
        km = max(0.15, e.length_m / 1000.0)
        lam = per_km * km
        cnt = max(0, int(round(lam + rng.gauss(0.0, math.sqrt(lam) + 0.35))))
        hist_freq[e.segment_id] = round(cnt / km, 4)

    # --- per-segment monsoon cell exposure ----------------------------------
    exposure = {e.segment_id: rng.uniform(0.45, 1.75) for e in edges}

    # ---- pass 1: draw every random quantity ONCE --------------------------
    # Draws must not depend on the intercept, otherwise re-labelling changes the
    # feature stream and the base-rate calibration cannot converge.  The hazard
    # payload is therefore drawn unconditionally and only applied if the row ends
    # up labelled positive.
    draws = random.Random(seed ^ 0xC0FFEE)
    rows: List[Dict[str, Any]] = []
    rid = 0
    for e in edges:
        for _ in range(draws.randint(*obs_per_segment)):
            month = draws.choices([5, 6, 7, 8, 9, 10],
                                  weights=[9, 17, 26, 24, 15, 9])[0]
            day = draws.randint(1, 28)
            phase, mult = _phase_for(month)
            w = _sample_weather(draws, e, mult, exposure[e.segment_id])
            row: Dict[str, Any] = {
                "record_id": f"HZ23-{rid:06d}",
                "timestamp": f"2023-{month:02d}-{day:02d}T{draws.randint(0, 23):02d}:"
                             f"{draws.randint(0, 59):02d}:00+05:30",
                "season_phase": phase,
                "segment_id": e.segment_id,
                "state": e.state, "district": e.district, "highway": e.highway,
                "lat": round(e.lat, 6), "lon": round(e.lon, 6),
                "elevation_m": round(e.elevation_m, 1),
                "length_m": e.length_m, "slope_deg": e.slope_deg,
                "soil_type": e.soil_type, "drainage": e.drainage,
                "ndvi": e.ndvi, "cut_slope": e.cut_slope,
                "sinuosity": e.sinuosity, "surface": e.surface,
                "rain_mm_hr": w["rain_mm_hr"], "rain_24h_mm": w["rain_24h_mm"],
                "api_3d_mm": w["api_3d_mm"], "soil_saturation": w["soil_saturation"],
                "hist_freq_per_km": hist_freq[e.segment_id],
                "_p_true": None, "disrupted": 0, "hazard_type": "none",
                "closure_hours": 0.0, "debris_tonnes": 0.0,
                "source": "", "notes": "",
            }
            slope = e.slope_deg
            if slope >= 11.0 and e.cut_slope:
                hz = draws.choices(("landslide", "debris_flow", "slope_failure",
                                    "boulder_fall", "road_collapse"),
                                   weights=[42, 21, 18, 11, 8])[0]
            elif slope >= 6.0:
                hz = draws.choices(("slope_failure", "landslide", "boulder_fall",
                                    "waterlogging"), weights=[38, 26, 16, 20])[0]
            else:
                hz = draws.choices(("waterlogging", "flash_flood",
                                    "embankment_washout"), weights=[54, 32, 14])[0]
            sev = 1.0 + slope / 22.0 + w["rain_mm_hr"] / 90.0
            row["_payload"] = {
                "hazard_type": hz,
                "closure_hours": round(max(0.5, draws.gammavariate(2.1, 3.4) * sev), 1),
                "debris_tonnes": round(
                    draws.lognormvariate(3.4, 1.1) * sev
                    if hz in ("landslide", "debris_flow", "boulder_fall",
                              "road_collapse", "slope_failure") else 0.0, 1),
                "source_pos": draws.choice(HAZARD_SOURCES),
                "source_neg": draws.choice(("BRO", "NHIDCL", "field_report",
                                            "highway_patrol", "satellite_SAR")),
                "u": draws.random(),
            }
            row["_z_base"] = true_disruption_logit(row, intercept=0.0)
            row["_eps"] = draws.gauss(0.0, DGP["noise_sd"])
            rows.append(row)
            rid += 1

    # ---- pass 2: label, bisecting the intercept onto the target base rate ---
    def label(intercept: float) -> float:
        pos = 0
        for r in rows:
            p = sigmoid(intercept + r["_z_base"] + r["_eps"])
            r["_p_true"] = round(p, 5)
            pl = r["_payload"]
            if pl["u"] < p:
                pos += 1
                r["disrupted"] = 1
                r["hazard_type"] = pl["hazard_type"]
                r["closure_hours"] = pl["closure_hours"]
                r["debris_tonnes"] = pl["debris_tonnes"]
                r["source"] = pl["source_pos"]
                r["notes"] = (f"{r['season_phase']} monsoon observation, "
                              f"{pl['hazard_type']} reported")
            else:
                r["disrupted"] = 0
                r["hazard_type"] = "none"
                r["closure_hours"] = 0.0
                r["debris_tonnes"] = 0.0
                r["source"] = pl["source_neg"]
                r["notes"] = (f"{r['season_phase']} monsoon observation, "
                              f"carriageway passable")
        return pos / len(rows) if rows else 0.0

    intercept = float(DGP["intercept"])
    if autotune and rows:
        # The disruption rate is monotone increasing in the intercept, so a
        # bracketed bisection converges where a Newton step on the mean of
        # sigmoids oscillates.  The bracket is FIXED (not derived from the
        # current DGP["intercept"]) and the winner is rounded before the final
        # labelling pass, so repeated calls are bit-identical: the label of the
        # row sitting exactly on the decision boundary would otherwise depend on
        # where the previous call happened to stop.
        lo, hi = -30.0, 15.0
        for _ in range(6):
            if label(lo) <= target_positive_rate:
                break
            lo -= 12.0
        for _ in range(6):
            if label(hi) >= target_positive_rate:
                break
            hi += 12.0
        for _ in range(26):
            mid = 0.5 * (lo + hi)
            if label(mid) < target_positive_rate:
                lo = mid
            else:
                hi = mid
        intercept = round(0.5 * (lo + hi), 4)

    label(intercept)
    DGP["intercept"] = intercept
    for r in rows:
        r.pop("_payload", None)
        r.pop("_z_base", None)
        r.pop("_eps", None)
    return rows


HAZARD_CSV_COLUMNS = (
    "record_id", "timestamp", "season_phase", "segment_id", "state", "district",
    "highway", "lat", "lon", "elevation_m", "length_m", "slope_deg", "soil_type",
    "drainage", "ndvi", "cut_slope", "sinuosity", "surface", "rain_mm_hr",
    "rain_24h_mm", "api_3d_mm", "soil_saturation", "hist_freq_per_km", "disrupted",
    "hazard_type", "closure_hours", "debris_tonnes", "source", "notes",
)


def write_hazard_csv(rows: Sequence[Dict[str, Any]], path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(HAZARD_CSV_COLUMNS), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in HAZARD_CSV_COLUMNS})
    return path


# --------------------------------------------------------------------------- #
#  LOADERS  (CSV / JSON / GeoJSON - stdlib only)                               #
# --------------------------------------------------------------------------- #
_NUM_FIELDS = ("lat", "lon", "elevation_m", "length_m", "slope_deg", "drainage",
               "ndvi", "cut_slope", "sinuosity", "rain_mm_hr", "rain_24h_mm",
               "api_3d_mm", "soil_saturation", "hist_freq_per_km", "disrupted",
               "closure_hours", "debris_tonnes")


def load_hazard_records(path: str) -> List[Dict[str, Any]]:
    """Load a hazard log from CSV or JSON (list of objects / {"records": [...]})."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        head = fh.read(4096)
        fh.seek(0)
        text = fh.read()
    rows: List[Dict[str, Any]]
    if path.lower().endswith((".json", ".geojson")) or head.lstrip().startswith(("{", "[")):
        obj = json.loads(text)
        rows = obj.get("records", obj) if isinstance(obj, dict) else obj
    else:
        rows = list(csv.DictReader(io.StringIO(text)))
    out = []
    for r in rows:
        rec = dict(r)
        for k in _NUM_FIELDS:
            if k in rec and rec[k] not in ("", None):
                try:
                    rec[k] = float(rec[k])
                except (TypeError, ValueError):
                    rec[k] = 0.0
        if "disrupted" in rec:
            rec["disrupted"] = int(rec["disrupted"] or 0)
        out.append(rec)
    return out


def load_geojson_graph(path: str) -> RoadGraph:
    with open(path, "r", encoding="utf-8") as fh:
        return RoadGraph.from_geojson(json.load(fh))


def _json_default(o: Any) -> Any:
    """JSON fallback that understands numpy scalars/arrays (and anything else)."""
    tolist = getattr(o, "tolist", None)
    if callable(tolist):
        return tolist()
    item = getattr(o, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    return str(o)


def save_json(obj: Any, path: str, indent: int = 2) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=indent, ensure_ascii=False, default=_json_default)
        fh.write("\n")
    return path


def ensure_inputs(graph_path: str = DEFAULT_GRAPH_PATH,
                  hazard_path: str = DEFAULT_HAZARD_PATH,
                  seed: int = DEFAULT_SEED, target_edges: int = 1000,
                  verbose: bool = True, regenerate: bool = False
                  ) -> Tuple[RoadGraph, List[Dict[str, Any]]]:
    """Auto-generate the demo inputs if absent, then load them."""
    say = (lambda *a: None) if not verbose else (lambda *a: print(*a))
    graph_exists = os.path.exists(graph_path)
    haz_exists = os.path.exists(hazard_path)

    if regenerate or not graph_exists:
        say(f"  [gen] synthesising mock OSM graph -> {os.path.relpath(graph_path, HERE)}")
        g = build_mock_osm_graph(target_edges=target_edges, seed=seed)
        save_json(g.to_geojson(), graph_path)
        graph = g
    else:
        graph = load_geojson_graph(graph_path)
        say(f"  [load] {os.path.relpath(graph_path, HERE)}: "
            f"{len(graph.edges)} edges / {len(graph.nodes)} nodes")

    if regenerate or not haz_exists:
        say(f"  [gen] synthesising 2023 hazard log -> {os.path.relpath(hazard_path, HERE)}")
        rows = build_historical_hazard_log(graph, seed=seed)
        write_hazard_csv(rows, hazard_path)
        # sidecar: the exact (auto-tuned) ground-truth DGP, so `--audit-dgp` can
        # reproduce the truth model even in a fresh process reading the CSV.
        save_json({
            "note": "Ground-truth data-generating process of the synthetic 2023 log.",
            "seed": seed, "generated_utc": utcnow_iso(), "dgp": dict(DGP),
            "positive_rate": round(mean([r["disrupted"] for r in rows]), 4),
            "n_rows": len(rows),
        }, os.path.splitext(hazard_path)[0] + ".dgp.json")
    else:
        rows = load_hazard_records(hazard_path)
        say(f"  [load] {os.path.relpath(hazard_path, HERE)}: {len(rows)} observations")
    return graph, rows


# --------------------------------------------------------------------------- #
#  EMPIRICAL BASE-RISK INDEX  (historical learning)                            #
# --------------------------------------------------------------------------- #
class HistoricalHazardIndex:
    """
    Turns a raw hazard log into a per-segment empirical base risk

        hist_freq(segment) = EB-shrunk hazard events / km / season

    Gamma-Poisson (empirical-Bayes) shrinkage stops rarely-observed segments from
    collapsing to zero and lets unseen segments inherit their (state, highway)
    group prior - exactly what a live router needs when a segment has no history.
    """

    def __init__(self, records: Sequence[Dict[str, Any]], shrink_k: float = 1.6):
        self.shrink_k = shrink_k
        self.n_records = len(records)
        self.events = sum(1 for r in records if int(r.get("disrupted", 0) or 0) == 1)
        self.positive_rate = (self.events / self.n_records) if self.n_records else 0.0

        seg_len: Dict[str, float] = {}
        seg_rows: Dict[str, int] = {}
        seg_prior: Dict[str, List[float]] = {}
        seg_hits: Dict[str, int] = {}
        seg_hours: Dict[str, float] = {}
        seg_debris: Dict[str, float] = {}
        seg_group: Dict[str, Tuple[str, str]] = {}
        type_counts: Dict[str, int] = {}
        month_counts: Dict[int, int] = {}
        for r in records:
            sid = str(r.get("segment_id", ""))
            if not sid:
                continue
            km = max(0.15, float(r.get("length_m", 1000.0)) / 1000.0)
            seg_len.setdefault(sid, km)
            seg_rows[sid] = seg_rows.get(sid, 0) + 1
            seg_group.setdefault(sid, (str(r.get("state", "")), str(r.get("highway", ""))))
            v = r.get("hist_freq_per_km", r.get("hist_freq"))
            if v not in (None, ""):
                seg_prior.setdefault(sid, []).append(float(v))
            if int(r.get("disrupted", 0) or 0) == 1:
                seg_hits[sid] = seg_hits.get(sid, 0) + 1
                seg_hours[sid] = seg_hours.get(sid, 0.0) + float(r.get("closure_hours", 0.0) or 0.0)
                seg_debris[sid] = seg_debris.get(sid, 0.0) + float(r.get("debris_tonnes", 0.0) or 0.0)
                ht = str(r.get("hazard_type", "unknown"))
                type_counts[ht] = type_counts.get(ht, 0) + 1
                ts = str(r.get("timestamp", ""))
                try:
                    month_counts[int(ts[5:7])] = month_counts.get(int(ts[5:7]), 0) + 1
                except (ValueError, IndexError):
                    pass

        self.seg_len = seg_len
        self.seg_rows = seg_rows
        self.seg_hits = seg_hits
        self.hazard_type_counts = type_counts
        self.month_counts = month_counts
        self.seg_group = seg_group
        self.closure_hours_total = sum(seg_hours.values())
        self.debris_tonnes_total = sum(seg_debris.values())
        self.seg_closure_hours = seg_hours
        self.seg_debris = seg_debris

        # ---- prior-season hazard frequency -> the `hist_freq` model feature ----
        # Every log row carries the segment's *previous* season rate.  Averaging
        # per segment (real logs repeat it on each incident record) and shrinking
        # it towards the exposure-weighted (state, highway) group mean gives a
        # base risk that is on exactly the same scale at training and at serving
        # time - the classic train/serve-skew trap for this feature.
        self.raw_freq: Dict[str, float] = {sid: mean(v) for sid, v in seg_prior.items()}
        grp_num: Dict[Tuple[str, str], float] = {}
        grp_den: Dict[Tuple[str, str], float] = {}
        for sid, km in seg_len.items():
            g = seg_group.get(sid, ("", ""))
            grp_num[g] = grp_num.get(g, 0.0) + self.raw_freq.get(sid, 0.0) * km
            grp_den[g] = grp_den.get(g, 0.0) + km
        self.group_prior = {g: (grp_num[g] / grp_den[g]) if grp_den[g] > 0 else 0.0
                            for g in grp_num}
        tot_km = sum(seg_len.values())
        tot_raw = sum(self.raw_freq.get(sid, 0.0) * km for sid, km in seg_len.items())
        self.global_prior = (tot_raw / tot_km) if tot_km > 0 else 0.0

        self.freq: Dict[str, float] = {}
        for sid, km in seg_len.items():
            prior = self.group_prior.get(seg_group.get(sid, ("", "")), self.global_prior)
            raw = self.raw_freq.get(sid)
            self.freq[sid] = prior if raw is None else \
                (km * raw + self.shrink_k * prior) / (km + self.shrink_k)

        # ---- realised 2023 disruption statistics (operational reporting) ----
        self.observed_freq = {sid: seg_hits.get(sid, 0) / km for sid, km in seg_len.items()}
        self.observed_rate = {sid: seg_hits.get(sid, 0) / max(1, seg_rows.get(sid, 0))
                              for sid in seg_len}

    # ------------------------------------------------------------------ #
    def prior_for(self, edge: RoadEdge) -> float:
        """Base risk for an edge, falling back to its (state, highway) group."""
        if edge.segment_id in self.freq:
            return self.freq[edge.segment_id]
        return self.group_prior.get((edge.state, edge.highway), self.global_prior)

    def attach_to_graph(self, graph: RoadGraph) -> int:
        """Stamp `hist_freq` onto every edge; unseen edges get the group prior."""
        n = 0
        for e in graph.edges:
            e.hist_freq = round(self.prior_for(e), 4)
            if e.segment_id in self.freq:
                n += 1
        return n

    def top_segments(self, k: int = 10) -> List[Tuple[str, float, int]]:
        ranked = sorted(self.freq.items(), key=lambda kv: -kv[1])[:k]
        return [(sid, round(f, 3), self.seg_hits.get(sid, 0)) for sid, f in ranked]

    def summary(self) -> Dict[str, Any]:
        return {
            "records": self.n_records,
            "disruption_events": self.events,
            "positive_rate": round(self.positive_rate, 4),
            "segments_covered": len(self.freq),
            "global_prior_events_per_km": round(self.global_prior, 4),
            "eb_shrinkage_k": self.shrink_k,
            "hist_freq_range": [round(min(self.freq.values()), 4),
                                round(max(self.freq.values()), 4)] if self.freq else None,
            "observed_2023_rate": round(self.positive_rate, 4),
            "hazard_type_mix": dict(sorted(self.hazard_type_counts.items(),
                                           key=lambda kv: -kv[1])),
            "month_histogram": {str(k): v for k, v in sorted(self.month_counts.items())},
            "total_closure_hours": round(self.closure_hours_total, 1),
            "total_debris_tonnes": round(self.debris_tonnes_total, 1),
        }


# --------------------------------------------------------------------------- #
#  FEATURE ENGINEERING                                                         #
# --------------------------------------------------------------------------- #
def edge_features(edge: RoadEdge, w: Dict[str, float]) -> List[float]:
    """Project (edge statics, weather state) onto the FEATURES vector."""
    rain = float(w.get("rain_mm_hr", 0.0))
    api3 = float(w.get("api_3d", w.get("api_3d_mm", 0.0)))
    sat = float(w.get("soil_saturation", 0.0))
    slope = float(edge.slope_deg)
    return [
        rain,
        api3,
        slope,
        sat,
        float(edge.hist_freq),
        math.tan(math.radians(slope)) * rain,
        float(edge.length_m),
        clamp(edge.elevation_m / 2000.0, 0.0, 1.0),
        float(edge.ndvi),
        float(edge.drainage),
        float(edge.cut_slope),
        float(edge.sinuosity),
    ]


def record_features(r: Dict[str, Any]) -> List[float]:
    """Project one historical log row onto the FEATURES vector."""
    rain = float(r.get("rain_mm_hr", 0.0))
    api3 = float(r.get("api_3d_mm", r.get("api_3d", 0.0)))
    slope = float(r.get("slope_deg", 0.0))
    sat = float(r.get("soil_saturation", 0.0))
    return [
        rain, api3, slope, sat,
        float(r.get("hist_freq_per_km", r.get("hist_freq", 0.0))),
        math.tan(math.radians(slope)) * rain,
        float(r.get("length_m", 1000.0)),
        clamp(float(r.get("elevation_m", 0.0)) / 2000.0, 0.0, 1.0),
        float(r.get("ndvi", 0.5)),
        float(r.get("drainage", 0.5)),
        float(r.get("cut_slope", 0.0)),
        float(r.get("sinuosity", 1.0)),
    ]


HIST_IDX = FEATURES.index("hist_freq")


def build_dataset(records: Sequence[Dict[str, Any]],
                  index: Optional["HistoricalHazardIndex"] = None
                  ) -> Tuple[List[List[float]], List[int], List[Dict[str, Any]]]:
    """
    (X, y, meta) with meta carrying per-row context for reporting.

    When `index` is supplied the `hist_freq` feature is taken from the same
    empirical-Bayes estimate the router uses at scoring time, so training and
    serving cannot drift apart.
    """
    X, y, meta = [], [], []
    for r in records:
        if "disrupted" not in r:
            continue
        feats = record_features(r)
        if index is not None:
            sid = str(r.get("segment_id", ""))
            if sid in index.freq:
                feats[HIST_IDX] = index.freq[sid]
        X.append(feats)
        y.append(1 if int(r.get("disrupted", 0) or 0) == 1 else 0)
        meta.append({
            "segment_id": r.get("segment_id"),
            "timestamp": r.get("timestamp"),
            "season_phase": r.get("season_phase"),
            "hazard_type": r.get("hazard_type", "none"),
            "closure_hours": float(r.get("closure_hours", 0.0) or 0.0),
            "state": r.get("state"),
            "p_true": r.get("_p_true"),
        })
    return X, y, meta


# --------------------------------------------------------------------------- #
#  EVALUATION METRICS  (pure Python - no sklearn needed)                       #
# --------------------------------------------------------------------------- #
def roc_auc(y: Sequence[int], p: Sequence[float]) -> float:
    """Mann-Whitney AUC with proper tie handling."""
    pairs = sorted(zip(p, y))
    n_pos = sum(y)
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    rank_sum = 0.0
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            if pairs[k][1] == 1:
                rank_sum += avg_rank
        i = j + 1
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def pr_auc(y: Sequence[int], p: Sequence[float]) -> float:
    """Average precision (area under the precision-recall curve)."""
    if sum(y) == 0:
        return float("nan")
    order = sorted(range(len(p)), key=lambda i: -p[i])
    tp = 0
    ap = 0.0
    prev = None
    n_pos = sum(y)
    for rank, i in enumerate(order, start=1):
        if y[i] == 1:
            tp += 1
            prec = tp / rank
            rec = tp / n_pos
            if prev is None or rec != prev:
                ap += prec * (rec - (prev or 0.0))
                prev = rec
    return ap


def log_loss(y: Sequence[int], p: Sequence[float], eps: float = 1e-12) -> float:
    if not y:
        return float("nan")
    s = 0.0
    for yi, pi in zip(y, p):
        pi = clamp(pi, eps, 1.0 - eps)
        s += -(yi * math.log(pi) + (1 - yi) * math.log(1 - pi))
    return s / len(y)


def brier_score(y: Sequence[int], p: Sequence[float]) -> float:
    return mean([(pi - yi) ** 2 for yi, pi in zip(y, p)]) if y else float("nan")


def confusion_at(y: Sequence[int], p: Sequence[float], thr: float) -> Dict[str, float]:
    tp = fp = fn = tn = 0
    for yi, pi in zip(y, p):
        pred = 1 if pi >= thr else 0
        if pred == 1 and yi == 1:
            tp += 1
        elif pred == 1 and yi == 0:
            fp += 1
        elif pred == 0 and yi == 1:
            fn += 1
        else:
            tn += 1
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    acc = (tp + tn) / max(1, len(y))
    spec = tn / (tn + fp) if (tn + fp) else 0.0
    return {"threshold": thr, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(f1, 4), "accuracy": round(acc, 4),
            "specificity": round(spec, 4)}


def ks_statistic(y: Sequence[int], p: Sequence[float]) -> float:
    pos = sorted(pi for yi, pi in zip(y, p) if yi == 1)
    neg = sorted(pi for yi, pi in zip(y, p) if yi == 0)
    if not pos or not neg:
        return float("nan")
    grid = sorted(set(pos + neg))
    best = 0.0
    for t in grid:
        tpr = bisect.bisect_right(pos, t) / len(pos)
        fpr = bisect.bisect_right(neg, t) / len(neg)
        best = max(best, abs(tpr - fpr))
    return round(best, 4)


def reliability_bins(y: Sequence[int], p: Sequence[float], nbins: int = 10
                     ) -> List[Dict[str, float]]:
    out = []
    for b in range(nbins):
        lo, hi = b / nbins, (b + 1) / nbins
        idx = [i for i in range(len(p)) if (lo <= p[i] < hi or (b == nbins - 1 and p[i] >= hi))]
        if not idx:
            out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": 0, "mean_pred": None,
                        "observed": None, "gap": None})
            continue
        mp = mean([p[i] for i in idx])
        ob = mean([float(y[i]) for i in idx])
        out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx),
                    "mean_pred": round(mp, 4), "observed": round(ob, 4),
                    "gap": round(ob - mp, 4)})
    return out


def expected_calibration_error(y: Sequence[int], p: Sequence[float],
                               nbins: int = 10) -> float:
    bins = reliability_bins(y, p, nbins)
    tot = sum(b["n"] for b in bins) or 1
    return round(sum(b["n"] * abs(b["gap"]) for b in bins if b["n"]) / tot, 4)


def evaluate(y: Sequence[int], p: Sequence[float], thr: float = 0.5,
             extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = mean([float(v) for v in y]) if y else 0.0
    m: Dict[str, Any] = {
        "n": len(y),
        "positive_rate": round(base, 4),
        "roc_auc": round(roc_auc(y, p), 4) if y else None,
        "pr_auc": round(pr_auc(y, p), 4) if y else None,
        "log_loss": round(log_loss(y, p), 4),
        "brier": round(brier_score(y, p), 4),
        "ks": ks_statistic(y, p) if y else None,
        "ece": expected_calibration_error(y, p),
    }
    m["brier_skill_vs_base"] = round(
        1.0 - m["brier"] / max(1e-9, base * (1 - base)), 4) if y else None
    m.update(confusion_at(y, p, thr))
    if extra:
        m.update(extra)
    return m


def band_lift_table(y: Sequence[int], p: Sequence[float],
                    thr: float = 0.5) -> List[Dict[str, Any]]:
    """
    Operational validation per risk band: how often a segment assigned to a band
    was *actually* disrupted, and the lift over the network base rate.

    This is the table an SDMA / BRO duty officer cares about: "when the engine
    says SEVERE, how often does the road really fail?"
    """
    n = len(y)
    base = mean([float(v) for v in y]) if n else 0.0
    out: List[Dict[str, Any]] = []
    lo = 0.0
    for ub, name, _colour, action in RISK_BANDS:
        idx = [i for i in range(n) if lo <= p[i] < ub]
        row: Dict[str, Any] = {
            "band": name, "p_range": [round(lo, 2), round(min(ub, 1.0), 2)],
            "action": action, "n": len(idx),
            "share_of_network": round(len(idx) / n, 4) if n else 0.0,
        }
        if idx:
            obs = mean([float(y[i]) for i in idx])
            pred = mean([p[i] for i in idx])
            row.update({
                "observed_disruption_rate": round(obs, 4),
                "mean_predicted": round(pred, 4),
                "lift_vs_base_rate": round(obs / base, 2) if base > 0 else None,
                "flagged_at_threshold": sum(1 for i in idx if p[i] >= thr),
            })
        else:
            row.update({"observed_disruption_rate": None, "mean_predicted": None,
                        "lift_vs_base_rate": None, "flagged_at_threshold": 0})
        out.append(row)
        lo = ub
    return out


def stratified_kfold(y: Sequence[int], k: int = 5, seed: int = DEFAULT_SEED
                     ) -> List[Tuple[List[int], List[int]]]:
    """Deterministic stratified K-fold (train_idx, test_idx) pairs."""
    rng = random.Random(seed)
    by_cls: Dict[int, List[int]] = {}
    for i, yi in enumerate(y):
        by_cls.setdefault(int(yi), []).append(i)
    folds: List[List[int]] = [[] for _ in range(k)]
    for cls in sorted(by_cls):
        idx = by_cls[cls][:]
        rng.shuffle(idx)
        for j, i in enumerate(idx):
            folds[j % k].append(i)
    out = []
    for f in range(k):
        test = sorted(folds[f])
        train = sorted(i for j in range(k) if j != f for i in folds[j])
        if test and train:
            out.append((train, test))
    return out


def fit_platt(z: Sequence[float], y: Sequence[int], iters: int = 60
              ) -> Tuple[float, float]:
    """
    Platt scaling: fit sigma(a*z + b) to labels by Newton / IRLS on 2 params.
    Returns (a, b) = (1.0, 0.0) when the fit is degenerate.
    """
    n = len(z)
    if n < 20 or sum(y) in (0, n):
        return (1.0, 0.0)
    t = [1.0 if yi else 0.0 for yi in y]
    # target smoothing (Platt's regularised targets)
    npos = sum(t)
    hi, lo = (npos + 1.0) / (npos + 2.0), 1.0 / (n - npos + 2.0)
    t = [hi if v == 1.0 else lo for v in t]
    a, b = 1.0, 0.0
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for zi, ti in zip(z, t):
            f = a * zi + b
            p = sigmoid(f)
            d = p - ti
            r = max(1e-9, p * (1.0 - p))
            ga += d * zi
            gb += d
            haa += r * zi * zi
            hab += r * zi
            hbb += r
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = -(hbb * ga - hab * gb) / det
        db = -(haa * gb - hab * ga) / det
        a += clamp(da, -3.0, 3.0)
        b += clamp(db, -3.0, 3.0)
        if abs(da) < 1e-7 and abs(db) < 1e-7:
            break
    if not (math.isfinite(a) and math.isfinite(b)) or a <= 0.0:
        return (1.0, 0.0)
    return (round(a, 6), round(b, 6))


# --------------------------------------------------------------------------- #
#  BACKEND 1/4 - PURE-PYTHON NEWTON-BOOSTED TREES (zero-dependency fallback)   #
# --------------------------------------------------------------------------- #
def _make_bins(X: Sequence[Sequence[float]], n_bins: int = 32
               ) -> List[List[float]]:
    """Per-feature quantile cut points; bin(x) = bisect_right(cuts, x)."""
    m = len(X[0]) if X else 0
    cuts: List[List[float]] = []
    for f in range(m):
        vals = sorted({float(row[f]) for row in X})
        if len(vals) <= 2:
            cuts.append(vals[:1])
            continue
        if len(vals) <= n_bins:
            cuts.append(vals[:-1])
            continue
        c: List[float] = []
        for b in range(1, n_bins):
            q = quantile(vals, b / n_bins)
            if not c or q > c[-1]:
                c.append(q)
        cuts.append(c)
    return cuts


class PureGBDT:
    """
    Gradient-boosted decision trees with Newton (second-order) updates on the
    logistic loss - a compact re-implementation of the XGBoost algorithm using
    only the Python standard library.  Supports histogram binning, L1/L2 leaf
    regularisation, min-split-gain, min-child-weight, row and column subsampling.

    Node encoding (flat array, index 0 = root):
        [feature_idx, bin_boundary, left_child, right_child, leaf_value, default_left]
    with feature_idx == -1 marking a leaf.
    """

    def __init__(self, n_estimators: int = 140, max_depth: int = 4,
                 learning_rate: float = 0.085, subsample: float = 0.85,
                 colsample_bytree: float = 0.85, reg_lambda: float = 2.0,
                 reg_alpha: float = 0.0, min_split_gain: float = 0.0,
                 min_child_weight: float = 2.0, min_child_samples: int = 8,
                 n_bins: int = 32, seed: int = DEFAULT_SEED):
        self.n_estimators = int(n_estimators)
        self.max_depth = int(max_depth)
        self.learning_rate = float(learning_rate)
        self.subsample = float(subsample)
        self.colsample_bytree = float(colsample_bytree)
        self.reg_lambda = float(reg_lambda)
        self.reg_alpha = float(reg_alpha)
        self.min_split_gain = float(min_split_gain)
        self.min_child_weight = float(min_child_weight)
        self.min_child_samples = int(min_child_samples)
        self.n_bins = int(n_bins)
        self.seed = int(seed)
        self.cuts: List[List[float]] = []
        self.trees: List[List[List[Any]]] = []
        self.base_score: float = 0.0
        self.gain_by_feature: Dict[int, float] = {}
        self.n_features: int = 0

    # ------------------------------------------------------------------ #
    def _binarise(self, X: Sequence[Sequence[float]]) -> List[List[int]]:
        br = bisect.bisect_right
        return [[br(self.cuts[f], float(row[f])) for f in range(self.n_features)]
                for row in X]

    def _best_split(self, Xb: Sequence[Sequence[int]], g: Sequence[float],
                    h: Sequence[float], idx: Sequence[int], cols: Sequence[int]
                    ) -> Optional[Tuple[int, int, float, List[int], List[int]]]:
        G = sum(g[i] for i in idx)
        H = sum(h[i] for i in idx)
        n = len(idx)
        base_gain = 0.5 * (G * G / (H + self.reg_lambda))
        best: Optional[Tuple[int, int, float]] = None
        for f in cols:
            nb = len(self.cuts[f]) + 1
            if nb <= 1:
                continue
            Gb = [0.0] * nb
            Hb = [0.0] * nb
            Cb = [0] * nb
            for i in idx:
                b = Xb[i][f]
                Gb[b] += g[i]
                Hb[b] += h[i]
                Cb[b] += 1
            GL = HL = 0.0
            CL = 0
            for b in range(nb - 1):
                GL += Gb[b]
                HL += Hb[b]
                CL += Cb[b]
                GR = G - GL
                HR = H - HL
                CR = n - CL
                if CL < self.min_child_samples or CR < self.min_child_samples:
                    continue
                if HL < self.min_child_weight or HR < self.min_child_weight:
                    continue
                gain = 0.5 * (GL * GL / (HL + self.reg_lambda)
                              + GR * GR / (HR + self.reg_lambda)) - base_gain
                gain -= self.min_split_gain + self.reg_alpha * (abs(GL) + abs(GR)) / max(1, n)
                if best is None or gain > best[2]:
                    best = (f, b, gain)
        if best is None or best[2] <= 0.0:
            return None
        f, bb, gain = best
        left = [i for i in idx if Xb[i][f] <= bb]
        right = [i for i in idx if Xb[i][f] > bb]
        if not left or not right:
            return None
        return (f, bb, gain, left, right)

    def _grow(self, Xb: Sequence[Sequence[int]], g: Sequence[float],
              h: Sequence[float], rows: Sequence[int], cols: Sequence[int]
              ) -> List[List[Any]]:
        nodes: List[List[Any]] = []

        def rec(idx: List[int], depth: int) -> int:
            ni = len(nodes)
            nodes.append([])
            G = sum(g[i] for i in idx)
            H = sum(h[i] for i in idx)
            leaf = -G / (H + self.reg_lambda)
            split = None
            if depth < self.max_depth and len(idx) >= 2 * self.min_child_samples:
                split = self._best_split(Xb, g, h, idx, cols)
            if split is None:
                nodes[ni] = [-1, 0, -1, -1, leaf, 0]
                return ni
            f, bb, gain, lidx, ridx = split
            li = rec(lidx, depth + 1)
            ri = rec(ridx, depth + 1)
            self.gain_by_feature[f] = self.gain_by_feature.get(f, 0.0) + max(0.0, gain)
            nodes[ni] = [f, bb, li, ri, 0.0, 1 if len(lidx) >= len(ridx) else 0]
            return ni

        rec(list(rows), 0)
        return nodes

    def _tree_value(self, nodes: Sequence[Sequence[Any]], xb: Sequence[int]) -> float:
        ni = 0
        for _ in range(len(nodes) + 1):
            node = nodes[ni]
            if node[0] < 0:
                return float(node[4])
            go_left = xb[node[0]] <= node[1]
            ni = node[2] if go_left else node[3]
        return float(nodes[0][4])

    # ------------------------------------------------------------------ #
    def fit(self, X: Sequence[Sequence[float]], y: Sequence[int],
            seed: Optional[int] = None,
            sample_weight: Optional[Sequence[float]] = None) -> "PureGBDT":
        """Backend-uniform signature: fit(X, y, seed)."""
        n = len(X)
        if n == 0:
            raise ValueError("empty training set")
        if seed is not None:
            self.seed = int(seed)
        self.n_features = len(X[0])
        self.cuts = _make_bins(X, self.n_bins)
        Xb = self._binarise(X)
        w = [1.0] * n if sample_weight is None else [float(v) for v in sample_weight]
        pos = sum(w[i] for i in range(n) if y[i] == 1)
        neg = sum(w[i] for i in range(n) if y[i] == 0)
        self.base_score = clamp(logit(max(pos, 1e-3) / max(neg, 1e-3)), -4.0, 4.0)
        F = [self.base_score] * n
        self.trees = []
        self.gain_by_feature = {}
        rng = random.Random(self.seed)
        eta = self.learning_rate
        for t in range(self.n_estimators):
            p = [sigmoid(F[i]) for i in range(n)]
            g = [w[i] * (p[i] - y[i]) for i in range(n)]
            h = [max(1e-7, w[i] * p[i] * (1.0 - p[i])) for i in range(n)]
            rows = list(range(n))
            if self.subsample < 1.0:
                k = max(8, int(round(n * self.subsample)))
                rows = sorted(rng.sample(rows, k))
            cols = list(range(self.n_features))
            if self.colsample_bytree < 1.0:
                k = max(1, int(round(self.n_features * self.colsample_bytree)))
                cols = sorted(rng.sample(cols, k))
            tree = self._grow(Xb, g, h, rows, cols)
            self.trees.append(tree)
            for i in range(n):
                F[i] += eta * self._tree_value(tree, Xb[i])
        return self

    def predict_proba(self, X: Sequence[Sequence[float]]) -> List[float]:
        Xb = self._binarise(X)
        out = []
        for xb in Xb:
            F = self.base_score
            for tree in self.trees:
                F += self.learning_rate * self._tree_value(tree, xb)
            out.append(sigmoid(F))
        return out

    def feature_importances(self) -> Dict[int, float]:
        tot = sum(self.gain_by_feature.values())
        if tot <= 0:
            return {}
        return {f: v / tot for f, v in self.gain_by_feature.items()}

    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "algo": "pure-newton-gbdt",
            "n_features": self.n_features,
            "n_estimators": len(self.trees),
            "learning_rate": self.learning_rate,
            "base_score": round(self.base_score, 6),
            "cuts": [[round(c, 6) for c in f] for f in self.cuts],
            "trees": self.trees,
            "gain_by_feature": {str(k): round(v, 6) for k, v in self.gain_by_feature.items()},
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PureGBDT":
        m = PureGBDT(learning_rate=float(d.get("learning_rate", 0.1)),
                     n_estimators=int(d.get("n_estimators", 0)))
        m.n_features = int(d["n_features"])
        m.cuts = [[float(c) for c in f] for f in d["cuts"]]
        m.trees = [[[int(x[0]), int(x[1]), int(x[2]), int(x[3]), float(x[4]), int(x[5])]
                    for x in t] for t in d["trees"]]
        m.base_score = float(d.get("base_score", 0.0))
        m.gain_by_feature = {int(k): float(v) for k, v in (d.get("gain_by_feature") or {}).items()}
        return m


# --------------------------------------------------------------------------- #
#  BACKEND 2/4 - XGBOOST                                                       #
# --------------------------------------------------------------------------- #
class XGBBackend:
    name = "xgboost"

    def __init__(self, hp: Dict[str, Any]):
        self.hp = hp
        self.model = None

    def fit(self, X: List[List[float]], y: List[int], seed: int) -> None:
        from xgboost import XGBClassifier                     # type: ignore
        self.model = XGBClassifier(
            n_estimators=int(self.hp.get("n_estimators", 300)),
            max_depth=int(self.hp.get("max_depth", 4)),
            learning_rate=float(self.hp.get("learning_rate", 0.07)),
            subsample=float(self.hp.get("subsample", 0.9)),
            colsample_bytree=float(self.hp.get("colsample_bytree", 0.85)),
            reg_lambda=float(self.hp.get("reg_lambda", 2.0)),
            reg_alpha=float(self.hp.get("reg_alpha", 0.0)),
            min_child_weight=float(self.hp.get("min_child_weight", 2.0)),
            gamma=float(self.hp.get("min_split_gain", 0.0)),
            tree_method="hist", objective="binary:logistic",
            eval_metric="logloss", random_state=seed, n_jobs=2,
            verbosity=0,
        )
        self.model.fit(X, y)

    def predict_proba(self, X: List[List[float]]) -> List[float]:
        return [float(v) for v in self.model.predict_proba(X)[:, 1]]

    def importances(self) -> Dict[int, float]:
        try:
            vals = [float(v) for v in self.model.feature_importances_]
        except Exception:                                    # pragma: no cover
            return {}
        tot = sum(vals) or 1.0
        return {i: v / tot for i, v in enumerate(vals) if v > 0}

    def save(self, path: str) -> str:
        self.model.save_model(path)
        return path

    def load(self, path: str) -> None:
        from xgboost import XGBClassifier                     # type: ignore
        self.model = XGBClassifier()
        self.model.load_model(path)


# --------------------------------------------------------------------------- #
#  BACKEND 3/4 - SCIKIT-LEARN                                                  #
# --------------------------------------------------------------------------- #
class SklearnBackend:
    name = "sklearn"

    def __init__(self, hp: Dict[str, Any]):
        self.hp = hp
        self.model = None
        self._imp: Dict[int, float] = {}

    def fit(self, X: List[List[float]], y: List[int], seed: int) -> None:
        from sklearn.ensemble import HistGradientBoostingClassifier   # type: ignore
        self.model = HistGradientBoostingClassifier(
            max_iter=int(self.hp.get("n_estimators", 300)),
            max_depth=int(self.hp.get("max_depth", 4)) or None,
            learning_rate=float(self.hp.get("learning_rate", 0.07)),
            min_samples_leaf=int(self.hp.get("min_child_samples", 12)),
            l2_regularization=float(self.hp.get("reg_lambda", 2.0)),
            max_leaf_nodes=2 ** int(self.hp.get("max_depth", 4)),
            random_state=seed,
        )
        self.model.fit(X, y)
        self._imp = self._permutation_importance(X, y, seed)

    def _permutation_importance(self, X: List[List[float]], y: List[int],
                                seed: int) -> Dict[int, float]:
        try:
            from sklearn.inspection import permutation_importance    # type: ignore
            base = roc_auc(y, list(self.model.predict_proba(X)[:, 1]))
            r = permutation_importance(self.model, X, y, scoring="roc_auc",
                                       n_repeats=4, random_state=seed, n_jobs=1)
            vals = [max(0.0, float(v)) for v in r.importances_mean]
            if not any(vals):
                return {}
            tot = sum(vals) or 1.0
            _ = base
            return {i: v / tot for i, v in enumerate(vals) if v > 0}
        except Exception:                                    # pragma: no cover
            return {}

    def predict_proba(self, X: List[List[float]]) -> List[float]:
        return [float(v) for v in self.model.predict_proba(X)[:, 1]]

    def importances(self) -> Dict[int, float]:
        return self._imp

    def save(self, path: str) -> str:
        import pickle
        with open(path, "wb") as fh:
            pickle.dump(self.model, fh, protocol=4)
        return path

    def load(self, path: str) -> None:
        import pickle
        with open(path, "rb") as fh:
            self.model = pickle.load(fh)


# --------------------------------------------------------------------------- #
#  BACKEND 4/4 - PYTORCH MLP (research extension hook)                         #
# --------------------------------------------------------------------------- #
class TorchBackend:
    """
    Small standardised-input MLP trained with BCEWithLogitsLoss.  Kept as the
    documented research hook: swap in a GRU / GNN head over the same feature
    vector without touching the rest of the engine.
    """

    name = "torch"

    def __init__(self, hp: Dict[str, Any]):
        self.hp = hp
        self.net = None
        self.mu: List[float] = []
        self.sd: List[float] = []

    def fit(self, X: List[List[float]], y: List[int], seed: int) -> None:
        import torch                                          # type: ignore
        import torch.nn as nn                                 # type: ignore
        torch.manual_seed(seed)
        m = len(X[0])
        self.mu = [mean([r[f] for r in X]) for f in range(m)]
        self.sd = [max(1e-6, stdev([r[f] for r in X])) for f in range(m)]
        xs = torch.tensor([[(r[f] - self.mu[f]) / self.sd[f] for f in range(m)]
                           for r in X], dtype=torch.float32)
        ys = torch.tensor(y, dtype=torch.float32).unsqueeze(1)
        hidden = int(self.hp.get("hidden", 48))
        self.net = nn.Sequential(
            nn.Linear(m, hidden), nn.ReLU(), nn.Dropout(0.15),
            nn.Linear(hidden, hidden // 2), nn.ReLU(),
            nn.Linear(hidden // 2, 1),
        )
        opt = torch.optim.Adam(self.net.parameters(),
                               lr=float(self.hp.get("lr", 3e-3)),
                               weight_decay=float(self.hp.get("reg_lambda", 2.0)) * 1e-4)
        lossf = nn.BCEWithLogitsLoss()
        epochs = int(self.hp.get("epochs", 160))
        bs = min(256, max(32, len(X) // 8))
        n = len(X)
        g = torch.Generator().manual_seed(seed)
        self.net.train()
        for _ in range(epochs):
            perm = torch.randperm(n, generator=g)
            for s in range(0, n, bs):
                b = perm[s:s + bs]
                opt.zero_grad()
                loss = lossf(self.net(xs[b]), ys[b])
                loss.backward()
                opt.step()
        self.net.eval()

    def predict_proba(self, X: List[List[float]]) -> List[float]:
        import torch                                          # type: ignore
        m = len(X[0])
        xs = torch.tensor([[(r[f] - self.mu[f]) / self.sd[f] for f in range(m)]
                           for r in X], dtype=torch.float32)
        with torch.no_grad():
            return [float(v) for v in torch.sigmoid(self.net(xs)).flatten()]

    def importances(self) -> Dict[int, float]:
        return {}

    def save(self, path: str) -> str:
        import torch                                          # type: ignore
        torch.save({"state_dict": self.net.state_dict(), "mu": self.mu,
                    "sd": self.sd, "hp": self.hp}, path)
        return path

    def load(self, path: str) -> None:
        import torch                                          # type: ignore
        import torch.nn as nn                                 # type: ignore
        blob = torch.load(path, map_location="cpu")
        self.mu, self.sd = blob["mu"], blob["sd"]
        hp = blob.get("hp", {})
        m, hidden = len(self.mu), int(hp.get("hidden", 48))
        self.net = nn.Sequential(
            nn.Linear(m, hidden), nn.ReLU(), nn.Dropout(0.15),
            nn.Linear(hidden, hidden // 2), nn.ReLU(), nn.Linear(hidden // 2, 1))
        self.net.load_state_dict(blob["state_dict"])
        self.net.eval()


# --------------------------------------------------------------------------- #
#  THE MODEL                                                                   #
# --------------------------------------------------------------------------- #
HYPERPARAMS: Dict[str, Any] = {
    "n_estimators": 260,
    "max_depth": 4,
    "learning_rate": 0.06,
    "subsample": 0.9,
    "colsample_bytree": 0.85,
    "reg_lambda": 2.0,
    "reg_alpha": 0.0,
    "min_split_gain": 0.0,
    "min_child_weight": 2.0,
    "min_child_samples": 12,
    "n_bins": 32,
    # torch backend only
    "hidden": 48, "lr": 3e-3, "epochs": 160,
}

# The pure-Python fallback is a literal re-implementation of the same algorithm,
# but runs single-threaded in an interpreter loop - use a leaner ensemble so the
# zero-dependency demo stays inside the "< 2 s for 1,000 edges" budget.
PURE_HYPERPARAMS: Dict[str, Any] = {
    "n_estimators": 110, "max_depth": 4, "learning_rate": 0.085,
    "subsample": 0.85, "colsample_bytree": 0.85, "reg_lambda": 2.0,
    "min_child_weight": 2.0, "min_child_samples": 10, "n_bins": 24,
}


class HazardRiskModel:
    """
    Segment Disruption Risk model.

        P_disruption(e) = sigma( a * g(features(e)) + b )

    where g is a gradient-boosted ensemble (XGBoost > scikit-learn > PyTorch >
    built-in pure-Python Newton boosting) and (a, b) is a Platt calibration layer
    fitted on out-of-fold predictions, so the emitted numbers are probabilities
    and not merely scores.
    """

    FORMAT = "sih26002.hazard-model"
    FORMAT_VERSION = 1

    def __init__(self, backend: str = "auto",
                 hyperparams: Optional[Dict[str, Any]] = None,
                 seed: int = DEFAULT_SEED, calibrate: bool = True,
                 threshold: float = 0.5,
                 features: Sequence[str] = FEATURES):
        self.backend_request = backend
        self.backend = resolve_backend(backend)
        base = dict(PURE_HYPERPARAMS if self.backend == "pure" else HYPERPARAMS)
        if hyperparams:
            base.update(hyperparams)
        self.hyperparams = base
        self.seed = seed
        self.calibrate_enabled = calibrate
        self.threshold = threshold
        self.features: Tuple[str, ...] = tuple(features)
        self.platt: Tuple[float, float] = (1.0, 0.0)
        self._impl: Any = None
        self.metrics: Dict[str, Any] = {}
        self.importance: Dict[str, float] = {}
        self.trained_utc: str = ""
        self.train_seconds: float = 0.0
        self.fitted = False
        # out-of-fold predictions from the last fit (in-memory only, not serialised):
        # lets `audit_dgp` compare against the truth ceiling without optimism bias.
        self.oof: Optional[Tuple[List[int], List[float]]] = None

    # ------------------------------------------------------------------ #
    def _new_impl(self, backend: Optional[str] = None) -> Any:
        b = backend or self.backend
        if b == "xgboost":
            return XGBBackend(self.hyperparams)
        if b == "sklearn":
            return SklearnBackend(self.hyperparams)
        if b == "torch":
            return TorchBackend(self.hyperparams)
        return PureGBDT(seed=self.seed, **{k: v for k, v in self.hyperparams.items()
                                           if k in PURE_HYPERPARAMS})

    def _impl_predict(self, impl: Any, X: Sequence[Sequence[float]]) -> List[float]:
        return list(impl.predict_proba(X))

    def _calibrate(self, p: Sequence[float]) -> List[float]:
        a, b = self.platt
        if a == 1.0 and b == 0.0:
            return [clamp(v, 1e-6, 1 - 1e-6) for v in p]
        return [clamp(sigmoid(a * logit(v) + b), 1e-6, 1 - 1e-6) for v in p]

    # ------------------------------------------------------------------ #
    def fit(self, X: Sequence[Sequence[float]], y: Sequence[int],
            folds: int = 5, verbose: bool = False) -> Dict[str, Any]:
        t0 = time.perf_counter()
        n = len(X)
        n_pos = int(sum(y))
        if n < 40 or n_pos in (0, n):
            raise ValueError(f"not enough labelled data to train (n={n}, positives={n_pos})")

        fold_metrics: List[Dict[str, Any]] = []
        oof = [0.0] * n
        k = max(2, min(folds, n_pos, n - n_pos))
        splits = stratified_kfold(y, k=k, seed=self.seed)
        for fi, (tr, te) in enumerate(splits):
            impl = self._new_impl()
            impl.fit([X[i] for i in tr], [y[i] for i in tr], self.seed + fi)
            pred = self._impl_predict(impl, [X[i] for i in te])
            for j, i in enumerate(te):
                oof[i] = pred[j]
            fold_metrics.append(evaluate([y[i] for i in te], pred, self.threshold))
            if verbose:
                print(f"    fold {fi + 1}/{len(splits)}: n={len(te)} "
                      f"auc={fold_metrics[-1]['roc_auc']:.4f} "
                      f"logloss={fold_metrics[-1]['log_loss']:.4f}")

        cv = evaluate(y, oof, self.threshold)
        cv["folds"] = len(splits)
        cv["fold_auc"] = [round(f["roc_auc"], 4) for f in fold_metrics]
        cv["auc_std"] = round(stdev([f["roc_auc"] for f in fold_metrics
                                     if f["roc_auc"] == f["roc_auc"]]), 4)

        # --- Platt calibration on out-of-fold logits -----------------------
        if self.calibrate_enabled:
            self.platt = fit_platt([logit(v) for v in oof], list(y))
            cv["platt_a"], cv["platt_b"] = self.platt
        else:
            self.platt = (1.0, 0.0)
        cal_p = self._calibrate(oof)
        cv["calibrated"] = evaluate(y, cal_p, self.threshold)
        cv["band_lift"] = band_lift_table(y, cal_p, self.threshold)

        # --- final model on all data ---------------------------------------
        self._impl = self._new_impl()
        self._impl.fit([list(r) for r in X], list(y), self.seed)
        self.fitted = True
        insample = evaluate(y, self.predict_proba(X), self.threshold)

        imp = self._impl.importances() if hasattr(self._impl, "importances") else {}
        if not imp and isinstance(self._impl, PureGBDT):
            imp = self._impl.feature_importances()
        tot = float(sum(imp.values())) or 1.0
        self.importance = {self.features[f]: round(float(imp.get(f, 0.0)) / tot, 4)
                           for f in range(len(self.features))}

        self.metrics = {
            "n_rows": n, "n_positive": n_pos,
            "positive_rate": round(n_pos / n, 4),
            "backend": self.backend,
            "backend_stack": available_backends(),
            "hyperparams": self.hyperparams,
            "cv": cv, "insample": insample,
            "threshold": self.threshold,
            "calibration": {"enabled": self.calibrate_enabled, "a": self.platt[0],
                            "b": self.platt[1],
                            "reliability": reliability_bins(y, self.predict_proba(X)),
                            "ece_cv": cv.get("ece")},
        }
        self.oof = (list(y), self._calibrate(oof))
        self.train_seconds = round(time.perf_counter() - t0, 3)
        self.trained_utc = utcnow_iso()
        self.fitted = True
        return self.metrics

    # ------------------------------------------------------------------ #
    def predict_proba(self, X: Sequence[Sequence[float]]) -> List[float]:
        if not self.fitted or self._impl is None:
            raise RuntimeError("model is not fitted / loaded")
        if not X:
            return []
        return self._calibrate(self._impl_predict(self._impl, X))

    def predict(self, X: Sequence[Sequence[float]]) -> List[int]:
        return [1 if p >= self.threshold else 0 for p in self.predict_proba(X)]

    def score_edge(self, edge: RoadEdge, weather: Dict[str, float]) -> float:
        return self.predict_proba([edge_features(edge, weather)])[0]

    # ------------------------------------------------------------------ #
    def to_dict(self, native_artifact: Optional[str] = None,
                embed_ensemble: bool = True) -> Dict[str, Any]:
        card: Dict[str, Any] = {
            "format": self.FORMAT,
            "format_version": self.FORMAT_VERSION,
            "engine_version": __version__,
            "problem_id": __problem__,
            "created_utc": self.trained_utc or utcnow_iso(),
            "backend": self.backend,
            "backend_stack": available_backends(),
            "features": list(self.features),
            "feature_units": {f: FEATURE_UNITS.get(f, "") for f in self.features},
            "hyperparams": self.hyperparams,
            "seed": self.seed,
            "threshold": self.threshold,
            "platt_calibration": {"a": self.platt[0], "b": self.platt[1]},
            "metrics": self.metrics,
            "feature_importance": self.importance,
            "risk_bands": [{"band": b, "max_p": m, "colour": c, "action": a}
                           for m, b, c, a in RISK_BANDS],
            "runtime": {"python": sys.version.split()[0],
                        "platform": platform.platform(),
                        "train_seconds": self.train_seconds,
                        "optional_packages": {k: v for k, v in _HAS.items()}},
            "native_artifact": native_artifact,
            "ensemble": None,
        }
        if embed_ensemble and isinstance(self._impl, PureGBDT):
            card["ensemble"] = self._impl.to_dict()
        card["sha1"] = sha1_of({k: v for k, v in card.items() if k != "sha1"})
        return card

    def save(self, path: str = DEFAULT_MODEL_PATH) -> str:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        native = None
        if self.backend == "xgboost" and self._impl is not None:
            native = os.path.splitext(path)[0] + ".xgb.ubj"
            self._impl.save(native)
        elif self.backend == "sklearn" and self._impl is not None:
            native = os.path.splitext(path)[0] + ".sklearn.pkl"
            self._impl.save(native)
        elif self.backend == "torch" and self._impl is not None:
            native = os.path.splitext(path)[0] + ".torch.pt"
            self._impl.save(native)
        card = self.to_dict(native_artifact=(os.path.basename(native) if native else None))
        save_json(card, path)
        return path

    @classmethod
    def load(cls, path: str = DEFAULT_MODEL_PATH,
             backend_override: Optional[str] = None) -> "HazardRiskModel":
        with open(path, "r", encoding="utf-8") as fh:
            card = json.load(fh)
        if card.get("format") != cls.FORMAT:
            raise ValueError(f"{path} is not a {cls.FORMAT} card")
        backend = backend_override or card.get("backend", "pure")
        m = cls(backend=backend, hyperparams=card.get("hyperparams"),
                seed=int(card.get("seed", DEFAULT_SEED)),
                calibrate=bool((card.get("platt_calibration") or {}).get("a", 1.0) != 1.0
                               or (card.get("platt_calibration") or {}).get("b", 0.0) != 0.0),
                threshold=float(card.get("threshold", 0.5)),
                features=tuple(card.get("features", FEATURES)))
        pc = card.get("platt_calibration") or {}
        m.platt = (float(pc.get("a", 1.0)), float(pc.get("b", 0.0)))
        m.metrics = card.get("metrics", {})
        m.importance = card.get("feature_importance", {})
        m.trained_utc = card.get("created_utc", "")
        m.train_seconds = float((card.get("runtime") or {}).get("train_seconds", 0.0))

        if card.get("ensemble"):
            m.backend = "pure"
            m._impl = PureGBDT.from_dict(card["ensemble"])
        elif card.get("native_artifact"):
            art = os.path.join(os.path.dirname(os.path.abspath(path)),
                               card["native_artifact"])
            if not os.path.exists(art):
                raise FileNotFoundError(f"native model artifact missing: {art}")
            if not _HAS.get(m.backend, False):
                raise RuntimeError(
                    f"model card was trained with backend '{m.backend}' which is not "
                    f"installed here.  Re-train with --backend pure for a portable model.")
            impl = m._new_impl(m.backend)
            impl.load(art)
            m._impl = impl
        else:
            raise ValueError("model card contains neither an ensemble nor a native artifact")
        m.fitted = True
        return m


# --------------------------------------------------------------------------- #
#  MONSOON WEATHER FIELD                                                       #
# --------------------------------------------------------------------------- #
class MonsoonField:
    """
    Spatially coherent synthetic rainfall field.

    A scenario places K Gaussian monsoon cells over the network bbox; each edge
    reads its own intensity, which is then amplified by orography (gradient and
    elevation) and converted into a 3-day Antecedent Precipitation Index and a
    soil-saturation state through a simple bucket model

        retention = (55 + 210 * (1 - drainage)) * max(0.45, 1 - 0.02 * slope)
        saturation = (1 - exp(-API_3d / retention)) * (0.62 + 0.42 * wetness)

    i.e. clay on a flat bench-cut saturates, free-draining gravel on a ridge does
    not.  Deterministic for a given (scenario, seed).
    """

    def __init__(self, scenario: str = "moderate", seed: int = DEFAULT_SEED,
                 bbox: Optional[Sequence[float]] = None,
                 n_cells: Optional[int] = None,
                 focus_points: Optional[Sequence[Tuple[float, float]]] = None):
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario '{scenario}' "
                             f"(choose from {', '.join(SCENARIO_ORDER)})")
        self.scenario = scenario
        self.cfg = SCENARIOS[scenario]
        self.seed = seed
        self.bbox = tuple(bbox) if bbox else (89.5, 22.5, 96.0, 28.5)
        rng = random.Random(seed ^ _stable_hash(scenario))
        focus = list(focus_points or [])
        self.cells: List[Tuple[float, float, float, float]] = []
        n = int(n_cells or self.cfg["cells"])
        for _ in range(n):
            if focus and rng.random() < 0.88:
                # centre the cell over the road network (with jitter) so that
                # corridors experience a real rainfall gradient rather than
                # sitting uniformly inside one vast cell
                clat, clon = rng.choice(focus)
                clat, clon = offset_point(clat, clon, rng.uniform(0.0, 28_000.0),
                                          rng.uniform(0.0, 360.0))
            else:
                clat = rng.uniform(self.bbox[1] + 0.4, self.bbox[3] - 0.4)
                clon = rng.uniform(self.bbox[0] + 0.4, self.bbox[2] - 0.4)
            self.cells.append((clat, clon,
                               rng.uniform(0.60, 1.50),
                               self.cfg["radius_km"] * rng.uniform(0.22, 0.58)))
        self.focus_points = focus

    # ------------------------------------------------------------------ #
    def intensity(self, lat: float, lon: float) -> float:
        """Normalised cell exposure at a point (0 = outside all cells)."""
        acc = 0.0
        for clat, clon, w, r in self.cells:
            d = haversine_m(lat, lon, clat, clon) / 1000.0
            acc += w * math.exp(-(d * d) / (2.0 * r * r))
        return acc

    def at_edge(self, edge: RoadEdge, rng: random.Random) -> Dict[str, float]:
        acc = self.intensity(edge.lat, edge.lon)
        orog = 1.0 + 0.019 * edge.slope_deg + 0.11 * clamp(edge.elevation_m / 1600.0, 0, 1)
        rain = self.cfg["rain"] * (0.32 + 1.42 * acc) * orog * rng.uniform(0.72, 1.32)
        rain = round(clamp(rain, 0.0, 260.0), 2)
        rain_24h = round(rain * rng.uniform(3.4, 8.6), 1)
        api = self.cfg["api"] * (0.38 + 1.25 * acc) * rng.uniform(0.8, 1.25) \
            + 0.32 * rain_24h
        api = round(clamp(api, 0.0, 900.0), 1)
        retention = (55.0 + 210.0 * (1.0 - edge.drainage)) * \
            max(0.45, 1.0 - 0.020 * edge.slope_deg)
        sat = (1.0 - math.exp(-api / max(retention, 20.0))) * \
            (0.62 + 0.42 * self.cfg["wet"]) + rng.gauss(0.0, 0.03)
        sat = round(clamp(sat, 0.02, 0.99), 4)
        return {"rain_mm_hr": rain, "rain_24h_mm": rain_24h, "api_3d": api,
                "soil_saturation": sat}

    def evaluate(self, edges: Sequence[RoadEdge]) -> WeatherState:
        rng = random.Random(self.seed ^ 0xBEEF)
        per_edge = {e.segment_id: self.at_edge(e, rng) for e in edges}
        rains = [v["rain_mm_hr"] for v in per_edge.values()]
        apis = [v["api_3d"] for v in per_edge.values()]
        sats = [v["soil_saturation"] for v in per_edge.values()]
        return WeatherState(
            scenario=self.scenario,
            rain_mm_hr=round(mean(rains), 2) if rains else 0.0,
            rain_24h_mm=round(mean([v["rain_24h_mm"] for v in per_edge.values()]), 1),
            api_3d=round(mean(apis), 1) if apis else 0.0,
            soil_saturation=round(mean(sats), 3) if sats else 0.0,
            wind_kmh=round(12.0 + 26.0 * self.cfg["wet"], 1),
            generated_utc=utcnow_iso(),
            per_edge=per_edge,
        )


# --------------------------------------------------------------------------- #
#  DYNAMIC COST FUNCTION                                                       #
# --------------------------------------------------------------------------- #
def risk_band(p: float) -> Tuple[str, str, str]:
    for ub, name, colour, action in RISK_BANDS:
        if p < ub:
            return name, colour, action
    return RISK_BANDS[-1][1], RISK_BANDS[-1][2], RISK_BANDS[-1][3]


@dataclass
class CostPolicy:
    """
    C(e) = t(e) * [1 + alpha * P(e)] + beta * H(e) + gamma * S(e) * C0

        t(e) = L(e) / v(e)                       transit time           [s]
        H(e) = tan(theta_e) * L(e) / V_REF       slope friction         [s-equiv]
        S(e) = cargo sensitivity multiplier      dimensionless
        C0   = cargo handling overhead per edge  [s]

    With `cargo_mode="multiplied"` the cargo multiplier additionally scales
    transit time (S(e) * t(e)), which is the correct form when perishable or
    hazmat cargo degrades with time on the road rather than per segment.
    """

    alpha: float = 2.5
    beta: float = 0.8
    gamma: float = 1.2
    cargo: str = "standard"
    cargo_mode: str = "additive"          # additive | multiplied
    cargo_overhead_s: float = CARGO_OVERHEAD_S
    v_ref: float = V_REF
    rain_derate: float = 0.0              # fraction of free-flow lost at P = 1
    block_threshold: float = 0.90         # P above this -> segment impassable
    hard_block: bool = True

    def __post_init__(self) -> None:
        if self.cargo not in CARGO_PROFILES:
            raise ValueError(f"unknown cargo profile '{self.cargo}' "
                             f"(choose from {', '.join(CARGO_PROFILES)})")
        if self.cargo_mode not in ("additive", "multiplied"):
            raise ValueError("cargo_mode must be 'additive' or 'multiplied'")

    # ------------------------------------------------------------------ #
    @property
    def cargo_multiplier(self) -> float:
        return CARGO_PROFILES[self.cargo]

    def speed_ms(self, edge: RoadEdge, p: float = 0.0) -> float:
        v = max(2.0, edge.speed_kmh) / 3.6
        if self.rain_derate > 0.0:
            v *= max(0.25, 1.0 - self.rain_derate * clamp(p, 0.0, 1.0))
        return v

    def transit_s(self, edge: RoadEdge, p: float = 0.0) -> float:
        return edge.length_m / self.speed_ms(edge, p)

    def slope_friction_s(self, edge: RoadEdge) -> float:
        return math.tan(math.radians(edge.slope_deg)) * edge.length_m / max(1e-6, self.v_ref)

    def cost_soft(self, edge: RoadEdge, p: float) -> Tuple[float, float, float]:
        """Dynamic cost with no hard block - the 'least-risk' fallback weight."""
        t = self.transit_s(edge, p)
        h = self.slope_friction_s(edge)
        s = self.cargo_multiplier
        tt = s * t if self.cargo_mode == "multiplied" else t
        c = tt * (1.0 + self.alpha * p) + self.beta * h + self.gamma * s * self.cargo_overhead_s
        return (c, t, h)

    def cost(self, edge: RoadEdge, p: float) -> Tuple[float, float, float]:
        """Return (total_cost_s, transit_s, slope_friction_s); inf if impassable."""
        if self.hard_block and p >= self.block_threshold:
            return (math.inf, math.inf, math.inf)
        return self.cost_soft(edge, p)

    def base_cost(self, edge: RoadEdge) -> Tuple[float, float, float]:
        """Risk-blind cost (P = 0) - the 'static OSRM' baseline for comparison."""
        return self.cost(edge, 0.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "formula": "C(e) = t(e)*[1 + alpha*P(e)] + beta*H(e) + gamma*S(e)*C0",
            "alpha": self.alpha, "beta": self.beta, "gamma": self.gamma,
            "cargo": self.cargo, "S": self.cargo_multiplier,
            "cargo_mode": self.cargo_mode,
            "C0_seconds": self.cargo_overhead_s, "V_REF_ms": self.v_ref,
            "rain_derate": self.rain_derate,
            "block_threshold": self.block_threshold if self.hard_block else None,
            "H_definition": "tan(slope_deg) * length_m / V_REF   [seconds-equivalent]",
            "t_definition": "length_m / (speed_kmh/3.6) * (1 - rain_derate*P)",
        }


# --------------------------------------------------------------------------- #
#  ROUTING  (Dijkstra + A*, stdlib heapq, works on the re-weighted graph)       #
# --------------------------------------------------------------------------- #
@dataclass
class RouteResult:
    found: bool
    algorithm: str
    mode: str                              # "risk" | "baseline"
    source: str
    target: str
    source_name: str = ""
    target_name: str = ""
    nodes: List[str] = field(default_factory=list)
    segment_ids: List[str] = field(default_factory=list)
    distance_km: float = 0.0
    transit_min: float = 0.0
    cost_s: float = 0.0
    base_cost_s: float = 0.0
    risk_exposure: float = 0.0             # length-weighted mean P along route
    risk_km: float = 0.0                   # sum P(e) * L(e) / 1000
    expected_closure_hours: float = 0.0
    max_p: float = 0.0
    worst_segment: str = ""
    impassable_segments: int = 0           # legs the cost model treats as cut
    impassable_km: float = 0.0
    band_counts: Dict[str, int] = field(default_factory=dict)
    states: List[str] = field(default_factory=list)
    highways: List[str] = field(default_factory=list)
    legs: List[Dict[str, Any]] = field(default_factory=list)
    search_ms: float = 0.0

    # ------------------------------------------------------------------ #
    @property
    def n_segments(self) -> int:
        return len(self.segment_ids)

    def to_dict(self, include_legs: bool = True) -> Dict[str, Any]:
        d = {
            "found": self.found, "algorithm": self.algorithm, "mode": self.mode,
            "source": self.source, "target": self.target,
            "source_name": self.source_name, "target_name": self.target_name,
            "n_segments": self.n_segments,
            "segment_ids": self.segment_ids,
            "distance_km": round(self.distance_km, 3),
            "transit_min": round(self.transit_min, 2),
            "cost_s": round(self.cost_s, 2),
            "base_cost_s": round(self.base_cost_s, 2),
            "risk_exposure": round(self.risk_exposure, 4),
            "risk_km": round(self.risk_km, 3),
            "impassable_segments": self.impassable_segments,
            "impassable_km": self.impassable_km,
            "expected_closure_hours": round(self.expected_closure_hours, 2),
            "max_p": round(self.max_p, 4),
            "worst_segment": self.worst_segment,
            "band_counts": self.band_counts,
            "states": self.states, "highways": self.highways,
            "search_ms": round(self.search_ms, 2),
        }
        if include_legs:
            d["legs"] = self.legs
        return d


def _reconstruct(prev: Dict[str, Tuple[str, int]], src: str, dst: str
                 ) -> Tuple[List[str], List[int]]:
    nodes = [dst]
    eidx: List[int] = []
    cur = dst
    guard = 0
    while cur != src:
        guard += 1
        if guard > 1_000_000 or cur not in prev:
            return [], []
        p, ei = prev[cur]
        eidx.append(ei)
        nodes.append(p)
        cur = p
    nodes.reverse()
    eidx.reverse()
    return nodes, eidx


def dijkstra(graph: RoadGraph, src: str, dst: str,
             weight: Callable[[RoadEdge], float]
             ) -> Optional[Tuple[float, List[str], List[int]]]:
    """Exact shortest path under an arbitrary non-negative edge weight."""
    adj = graph.adjacency()
    if src not in adj or dst not in adj:
        return None
    dist: Dict[str, float] = {src: 0.0}
    prev: Dict[str, Tuple[str, int]] = {}
    done: Set[str] = set()
    pq: List[Tuple[float, int, str]] = [(0.0, 0, src)]
    tie = 0
    while pq:
        d, _, u = heapq.heappop(pq)
        if u in done:
            continue
        done.add(u)
        if u == dst:
            break
        for ei, v in adj.get(u, ()):
            if v in done:
                continue
            w = weight(graph.edges[ei])
            if w is None or w == math.inf or w != w:
                continue
            nd = d + w
            if nd < dist.get(v, math.inf) - 1e-12:
                dist[v] = nd
                prev[v] = (u, ei)
                tie += 1
                heapq.heappush(pq, (nd, tie, v))
    if dst not in dist:
        return None
    nodes, eidx = _reconstruct(prev, src, dst)
    if not nodes:
        return None
    return (dist[dst], nodes, eidx)


def astar(graph: RoadGraph, src: str, dst: str,
          weight: Callable[[RoadEdge], float],
          v_max_ms: Optional[float] = None
          ) -> Optional[Tuple[float, List[str], List[int]]]:
    """
    A* with a great-circle heuristic h(n) = dist(n, dst) / v_max.

    Admissible because every edge cost is >= transit time >= length / v_max
    (slope, cargo and risk terms are all non-negative, and hard-blocked edges are
    excluded from the search).
    """
    adj = graph.adjacency()
    if src not in adj or dst not in adj:
        return None
    if v_max_ms is None:
        v_max_ms = max((e.speed_kmh for e in graph.edges), default=40.0) / 3.6
    v_max_ms = max(1.0, v_max_ms)
    dn = graph.nodes[dst]
    dlat, dlon = dn["lat"], dn["lon"]

    def h(nid: str) -> float:
        n = graph.nodes[nid]
        return haversine_m(n["lat"], n["lon"], dlat, dlon) / v_max_ms

    gscore: Dict[str, float] = {src: 0.0}
    prev: Dict[str, Tuple[str, int]] = {}
    done: Set[str] = set()
    pq: List[Tuple[float, float, int, str]] = [(h(src), 0.0, 0, src)]
    tie = 0
    while pq:
        _f, g, _, u = heapq.heappop(pq)
        if u in done:
            continue
        done.add(u)
        if u == dst:
            break
        for ei, v in adj.get(u, ()):
            if v in done:
                continue
            w = weight(graph.edges[ei])
            if w is None or w == math.inf or w != w:
                continue
            ng = g + w
            if ng < gscore.get(v, math.inf) - 1e-12:
                gscore[v] = ng
                prev[v] = (u, ei)
                tie += 1
                heapq.heappush(pq, (ng + h(v), ng, tie, v))
    if dst not in gscore:
        return None
    nodes, eidx = _reconstruct(prev, src, dst)
    if not nodes:
        return None
    return (gscore[dst], nodes, eidx)


def route(graph: RoadGraph, src_ref: str, dst_ref: str, algorithm: str = "astar",
          mode: str = "risk") -> RouteResult:
    """
    Route between two endpoints (node id, city name or 'lat,lon').

    mode = "risk"     -> use the dynamic cost C(e)   (hazard-aware routing)
    mode = "baseline" -> use the static cost C(e)|P=0 (what OSRM/Google would do)
    """
    src = graph.resolve_endpoint(src_ref)
    dst = graph.resolve_endpoint(dst_ref)
    res = RouteResult(found=False, algorithm=algorithm, mode=mode,
                      source=src_ref, target=dst_ref)
    if not src or not dst:
        return res
    res.source = src
    res.target = dst
    res.source_name = graph.nodes[src].get("name", "")
    res.target_name = graph.nodes[dst].get("name", "")
    if mode == "risk":
        weight: Callable[[RoadEdge], float] = \
            lambda e: e.cost_s if e.cost_s else e.base_cost_s
    elif mode == "risk_soft":
        weight = lambda e: e.cost_s_soft if e.cost_s_soft else e.base_cost_s
    elif mode == "baseline":
        weight = lambda e: e.base_cost_s
    else:
        raise ValueError(f"unknown routing mode '{mode}' "
                         f"(baseline | risk | risk_soft)")
    t0 = time.perf_counter()
    if algorithm == "dijkstra":
        out = dijkstra(graph, src, dst, weight)
    elif algorithm == "astar":
        out = astar(graph, src, dst, weight)
    else:
        raise ValueError(f"unknown algorithm '{algorithm}'")
    res.search_ms = (time.perf_counter() - t0) * 1000.0
    if out is None:
        return res
    cost, nodes, eidx = out
    res.found = True
    res.nodes = nodes
    res.cost_s = cost
    legs: List[Dict[str, Any]] = []
    tot_len = 0.0
    tot_t = 0.0
    tot_base = 0.0
    risk_km = 0.0
    exp_hours = 0.0
    impassable = 0
    impassable_km = 0.0
    bands: Dict[str, int] = {}
    states: List[str] = []
    hws: List[str] = []
    worst = (0.0, "")
    for ei in eidx:
        e = graph.edges[ei]
        tot_len += e.length_m
        tot_t += e.transit_s
        tot_base += e.base_cost_s
        risk_km += e.p_disruption * e.length_m / 1000.0
        exp_hours += e.p_disruption * _closure_hours_of(e)
        bands[e.risk_band] = bands.get(e.risk_band, 0) + 1
        if e.cost_s == math.inf:
            impassable += 1
            impassable_km += e.length_m / 1000.0
        if e.p_disruption > worst[0]:
            worst = (e.p_disruption, e.segment_id)
        if e.state and (not states or states[-1] != e.state):
            states.append(e.state)
        if e.highway and (not hws or hws[-1] != e.highway):
            hws.append(e.highway)
        legs.append({
            "segment_id": e.segment_id, "highway": e.highway, "name": e.name,
            "from": e.u, "to": e.v, "length_km": round(e.length_m / 1000.0, 3),
            "slope_deg": e.slope_deg, "state": e.state,
            "rain_mm_hr": e.weather.get("rain_mm_hr"),
            "api_3d": e.weather.get("api_3d"),
            "soil_saturation": e.weather.get("soil_saturation"),
            "hist_freq": e.hist_freq,
            "p_disruption": round(e.p_disruption, 4), "risk_band": e.risk_band,
            "transit_s": round(e.transit_s, 1),
            "base_cost_s": round(e.base_cost_s, 1),
            "cost_s": (None if e.cost_s == math.inf else round(e.cost_s, 1)),
        })
    res.segment_ids = [e.segment_id for e in (graph.edges[i] for i in eidx)]
    res.legs = legs
    res.distance_km = tot_len / 1000.0
    res.transit_min = tot_t / 60.0
    res.base_cost_s = tot_base
    res.risk_km = risk_km
    res.impassable_segments = impassable
    res.impassable_km = round(impassable_km, 3)
    res.expected_closure_hours = exp_hours
    res.risk_exposure = (risk_km / max(1e-9, tot_len / 1000.0))
    res.max_p = worst[0]
    res.worst_segment = worst[1]
    res.band_counts = {b: bands.get(b, 0) for b in
                       ("LOW", "MODERATE", "HIGH", "SEVERE", "CRITICAL")}
    res.states = states
    res.highways = hws
    return res


def _closure_hours_of(edge: RoadEdge) -> float:
    """Expected clearance hours if this segment fails (attached by the engine)."""
    v = getattr(edge, "_closure_hours", None)
    return float(v) if v else 8.0


# --------------------------------------------------------------------------- #
#  THE ENGINE                                                                  #
# --------------------------------------------------------------------------- #
DEFAULT_CORRIDORS: Tuple[Tuple[str, str], ...] = (
    ("Guwahati", "Shillong"),
    ("Silchar", "Aizawl"),
    ("Imphal", "Kohima"),
)


class HazardEngine:
    """
    Façade tying the four stages together:

        historical log  ->  HazardRiskModel  ->  P_disruption(e)  ->  C(e)  ->  route

    Typical use::

        engine = HazardEngine.bootstrap()
        summary = engine.run_all_scenarios()
    """

    def __init__(self, model: HazardRiskModel, graph: RoadGraph,
                 policy: Optional[CostPolicy] = None,
                 index: Optional[HistoricalHazardIndex] = None,
                 records: Optional[List[Dict[str, Any]]] = None):
        self.model = model
        self.graph = graph
        self.policy = policy or CostPolicy()
        self.index = index
        self.records = records or []
        self._closure_hours: Dict[str, float] = {}
        if index is not None:
            gmean = (index.closure_hours_total / index.events) if index.events else 8.0
            for sid, hits in index.seg_hits.items():
                if hits > 0:
                    self._closure_hours[sid] = index.seg_closure_hours.get(sid, 0.0) / hits
            self._default_closure_hours = gmean
        else:
            self._default_closure_hours = 8.0

    # ------------------------------------------------------------------ #
    @classmethod
    def bootstrap(cls, graph_path: str = DEFAULT_GRAPH_PATH,
                  hazard_path: str = DEFAULT_HAZARD_PATH,
                  model_path: str = DEFAULT_MODEL_PATH,
                  backend: str = "auto", seed: int = DEFAULT_SEED,
                  target_edges: int = 1000, folds: int = 5,
                  policy: Optional[CostPolicy] = None,
                  regenerate: bool = False, retrain: bool = False,
                  calibrate: bool = True, threshold: float = 0.5,
                  verbose: bool = False) -> "HazardEngine":
        """Load-or-generate inputs, load-or-train the model, return a live engine."""
        graph, records = ensure_inputs(graph_path, hazard_path, seed=seed,
                                       target_edges=target_edges, verbose=verbose,
                                       regenerate=regenerate)
        index = HistoricalHazardIndex(records)
        index.attach_to_graph(graph)
        model: Optional[HazardRiskModel] = None
        if not retrain and os.path.exists(model_path):
            try:
                model = HazardRiskModel.load(model_path)
                if model.backend != resolve_backend(backend) and backend != "auto":
                    model = None
            except Exception:
                model = None
        if model is None:
            X, y, _meta = build_dataset(records, index)
            model = HazardRiskModel(backend=backend, seed=seed, calibrate=calibrate,
                                    threshold=threshold)
            model.fit(X, y, folds=folds, verbose=verbose)
            model.save(model_path)
        engine = cls(model, graph, policy=policy, index=index, records=records)
        engine._apply_closure_hours()
        return engine

    def _apply_closure_hours(self) -> None:
        for e in self.graph.edges:
            setattr(e, "_closure_hours",
                    self._closure_hours.get(e.segment_id, self._default_closure_hours))

    # ------------------------------------------------------------------ #
    def score_edges(self, edges: Sequence[RoadEdge],
                    weather: WeatherState) -> List[float]:
        """Batch-score edges against a weather state; returns P_disruption per edge."""
        X = [edge_features(e, weather.for_edge(e)) for e in edges]
        return self.model.predict_proba(X)

    def score_point(self, lat: float, lon: float, rain_mm_hr: float,
                    api_3d: float, soil_saturation: float, slope_deg: float,
                    hist_freq: float = 0.5, length_m: float = 500.0,
                    elevation_m: float = 300.0, ndvi: float = 0.5,
                    drainage: float = 0.5, cut_slope: int = 1,
                    sinuosity: float = 1.1) -> float:
        """
        Real-time single-point API: score an arbitrary location without a graph.
        Used by incident-reporting / telematics callers.
        """
        e = RoadEdge(segment_id="LIVE", u="", v="", coords=[[lon, lat], [lon, lat]],
                     length_m=length_m, elev_from=elevation_m, elev_to=elevation_m,
                     slope_deg=slope_deg, ndvi=ndvi, drainage=drainage,
                     cut_slope=cut_slope, sinuosity=sinuosity, hist_freq=hist_freq)
        e.resolve_geometry()
        return self.score_edges([e], WeatherState(
            rain_mm_hr=rain_mm_hr, api_3d=api_3d, soil_saturation=soil_saturation))[0]

    # ------------------------------------------------------------------ #
    def reweight(self, graph: RoadGraph, scenario: str = "moderate",
                 weather: Optional[WeatherState] = None,
                 policy: Optional[CostPolicy] = None,
                 seed: int = DEFAULT_SEED) -> Tuple[RoadGraph, WeatherState, float]:
        """
        Score every edge, compute C(e), and stamp the results onto the graph.
        Returns (graph, weather_state, seconds_elapsed).
        """
        pol = policy or self.policy
        t0 = time.perf_counter()
        if weather is None:
            field_ = MonsoonField(scenario=scenario, seed=seed, bbox=graph.bbox(),
                                  focus_points=[(e.lat, e.lon) for e in graph.edges])
            weather = field_.evaluate(graph.edges)
        for e in graph.edges:
            setattr(e, "_closure_hours",
                    self._closure_hours.get(e.segment_id, self._default_closure_hours))
        probs = self.score_edges(graph.edges, weather)
        for e, p in zip(graph.edges, probs):
            e.p_disruption = p
            e.risk_band, _colour, _action = risk_band(p)
            e.weather = weather.for_edge(e)
            e.base_cost_s, t0_s, e.slope_friction_s = pol.base_cost(e)
            c, t_s, h_s = pol.cost_soft(e, p)
            e.cost_s_soft = c
            e.cost_s = math.inf if (pol.hard_block and p >= pol.block_threshold) else c
            e.transit_s = t_s if math.isfinite(t_s) else t0_s
            e.slope_friction_s = h_s if math.isfinite(h_s) else e.slope_friction_s
        elapsed = time.perf_counter() - t0
        return graph, weather, elapsed

    # ------------------------------------------------------------------ #
    def compare_routes(self, graph: RoadGraph, src: str, dst: str,
                       algorithm: str = "astar") -> Dict[str, Any]:
        """
        Baseline (risk-blind) vs. risk-aware routing on one corridor.

        If the hazard-aware search cannot find a path because every alignment is
        hard-blocked, the corridor is reported as SEVERED and re-solved with the
        least-risk weight (`cost_s_soft`) so operators still get a best-effort
        convoy route instead of nothing.
        """
        base = route(graph, src, dst, algorithm=algorithm, mode="baseline")
        risk = route(graph, src, dst, algorithm=algorithm, mode="risk")
        severed = base.found and not risk.found
        if severed:
            risk = route(graph, src, dst, algorithm=algorithm, mode="risk_soft")
            risk.mode = "risk_soft(severed)"
        out: Dict[str, Any] = {
            "corridor": f"{base.source_name or src} -> {base.target_name or dst}",
            "algorithm": algorithm,
            "severed": severed,
            "baseline": base.to_dict(),
            "risk_aware": risk.to_dict(),
        }
        if base.found and risk.found:
            shared = set(base.segment_ids) & set(risk.segment_ids)
            out["delta"] = {
                "distance_km": round(risk.distance_km - base.distance_km, 3),
                "distance_pct": round(100.0 * (risk.distance_km - base.distance_km)
                                      / max(1e-9, base.distance_km), 2),
                "transit_min": round(risk.transit_min - base.transit_min, 2),
                "transit_pct": round(100.0 * (risk.transit_min - base.transit_min)
                                     / max(1e-9, base.transit_min), 2),
                "risk_exposure": round(risk.risk_exposure - base.risk_exposure, 4),
                "risk_exposure_pct": round(
                    100.0 * (risk.risk_exposure - base.risk_exposure)
                    / max(1e-9, base.risk_exposure), 2),
                "risk_km": round(risk.risk_km - base.risk_km, 3),
                "expected_closure_hours": round(
                    risk.expected_closure_hours - base.expected_closure_hours, 2),
                "max_p": round(risk.max_p - base.max_p, 4),
                # what a risk-blind router would have driven into:
                "baseline_impassable_segments": base.impassable_segments,
                "baseline_impassable_km": base.impassable_km,
                "baseline_blocked": base.impassable_segments > 0,
                "risk_impassable_segments": risk.impassable_segments,
                "segments_shared": len(shared),
                "segments_avoided": sorted(set(base.segment_ids) - set(risk.segment_ids)),
                "segments_added": sorted(set(risk.segment_ids) - set(base.segment_ids)),
                "rerouted": base.segment_ids != risk.segment_ids,
                "severed": severed,
            }
        else:
            out["delta"] = {"rerouted": False, "severed": severed,
                            "note": "corridor unreachable in one or both modes"}
        return out

    def severance_report(self, graph: RoadGraph, hub: Optional[str] = None
                         ) -> Dict[str, Any]:
        """
        Accessibility audit: with hard-blocked segments removed, which hubs are
        still reachable from the reference hub?  This is the NER Accessibility
        Index that a static router cannot produce.
        """
        adj = graph.adjacency()
        named = graph.named_nodes()
        if not named:
            return {"error": "graph has no named hubs"}
        start = graph.resolve_endpoint(hub) if hub else None
        if not start:
            start = graph.node_by_name("Guwahati") or named[0]
        seen = {start}
        stack = [start]
        while stack:
            u = stack.pop()
            for ei, v in adj.get(u, ()):
                if v in seen or graph.edges[ei].cost_s == math.inf:
                    continue
                seen.add(v)
                stack.append(v)
        cut = sorted(graph.nodes[n].get("name", n) for n in named if n not in seen)
        blocked = [e for e in graph.edges if e.cost_s == math.inf]
        return {
            "hub": graph.nodes[start].get("name") or start,
            "named_hubs": len(named),
            "reachable_hubs": len(named) - len(cut),
            "accessibility_index": round((len(named) - len(cut)) / len(named), 4),
            "severed_hubs": cut,
            "impassable_segments": len(blocked),
            "impassable_km": round(sum(e.length_m for e in blocked) / 1000.0, 2),
        }

    # ------------------------------------------------------------------ #
    def hub_resilience(self, graph: RoadGraph, algorithm: str = "astar",
                       sample_pairs: int = 60) -> Dict[str, Any]:
        """
        Network-level resilience audit between the 28 named hubs.

        * connectivity is exact: union-find over the segments that are still
          passable, so every one of the hub pairs is classified without running
          a single shortest-path search;
        * a deterministic sample of pairs then gets a full baseline-vs-hazard
          route comparison, which is what quantifies "how much detour does
          safety cost, and how much risk does it buy back".
        """
        named = graph.named_nodes()
        if len(named) < 2:
            return {"error": "graph needs at least two named hubs"}

        # ---- exact connectivity over passable segments --------------------
        parent: Dict[str, str] = {n: n for n in graph.nodes}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        passable = [e for e in graph.edges if e.cost_s != math.inf]
        for e in passable:
            union(e.u, e.v)
        comps: Dict[str, int] = {}
        for n in graph.nodes:
            r = find(n)
            comps[r] = comps.get(r, 0) + 1
        hub_root = {n: find(n) for n in named}
        pairs = [(a, b) for i, a in enumerate(named) for b in named[i + 1:]]
        connected = [(a, b) for a, b in pairs if hub_root[a] == hub_root[b]]

        # ---- deterministic sample: worst-case pairs first ------------------
        rng = random.Random(DEFAULT_SEED ^ _stable_hash("hubmatrix"))
        pool = pairs[:]
        rng.shuffle(pool)
        sample = pool[:max(1, min(sample_pairs, len(pool)))]

        held = rerouted = severed = unreachable = 0
        d_km, d_t, d_exp = [], [], []
        detours: List[Dict[str, Any]] = []
        for a, b in sample:
            cmp_ = self.compare_routes(graph, a, b, algorithm=algorithm)
            base, risk, d = cmp_["baseline"], cmp_["risk_aware"], cmp_.get("delta") or {}
            if not base["found"]:
                unreachable += 1
                continue
            if not risk["found"]:
                severed += 1
                continue
            if d.get("severed"):
                severed += 1
            if d.get("rerouted"):
                rerouted += 1
                d_km.append(d["distance_pct"])
                d_t.append(d["transit_pct"])
                d_exp.append(d["risk_exposure_pct"])
                detours.append({
                    "corridor": cmp_["corridor"],
                    "baseline_km": base["distance_km"],
                    "risk_km": risk["distance_km"],
                    "distance_pct": d["distance_pct"],
                    "transit_pct": d["transit_pct"],
                    "exposure_pct": d["risk_exposure_pct"],
                    "baseline_impassable": base["impassable_segments"],
                    "expected_closure_hours": {
                        "static": base["expected_closure_hours"],
                        "hazard": risk["expected_closure_hours"]},
                })
            else:
                held += 1
        detours.sort(key=lambda r: -abs(r["exposure_pct"]))
        return {
            "named_hubs": len(named),
            "hub_pairs": len(pairs),
            "connected_pairs": len(connected),
            "connectivity_index": round(len(connected) / len(pairs), 4),
            "passable_segments": len(passable),
            "impassable_segments": len(graph.edges) - len(passable),
            "components": sorted(comps.values(), reverse=True)[:8],
            "n_components": len([c for c in comps.values() if c > 0]),
            "sampled_pairs": len(sample),
            "sample_outcome": {"held": held, "rerouted": rerouted,
                               "severed": severed, "unreachable_baseline": unreachable},
            "sample_deltas": {
                "mean_distance_pct": round(mean(d_km), 2) if d_km else None,
                "mean_transit_pct": round(mean(d_t), 2) if d_t else None,
                "mean_exposure_pct": round(mean(d_exp), 2) if d_exp else None,
                "max_distance_pct": round(max(d_km), 2) if d_km else None,
            },
            "largest_detours": detours[:5],
        }

    def scenario_summary(self, graph: RoadGraph, weather: WeatherState,
                         elapsed: float, scenario: str) -> Dict[str, Any]:
        probs = [e.p_disruption for e in graph.edges]
        bands: Dict[str, int] = {}
        for e in graph.edges:
            bands[e.risk_band] = bands.get(e.risk_band, 0) + 1
        km = lambda f: round(sum(e.length_m for e in graph.edges if f(e)) / 1000.0, 2)
        ranked = sorted(graph.edges, key=lambda e: -e.p_disruption)[:10]
        blocked = [e for e in graph.edges if e.cost_s == math.inf]
        cost_ratio = [(e.cost_s / e.base_cost_s) for e in graph.edges
                      if e.cost_s != math.inf and e.base_cost_s > 0]
        return {
            "scenario": scenario,
            "weather": weather.to_dict(),
            "edges": len(graph.edges),
            "network_km": round(graph.total_length_km(), 2),
            "rain_mm_hr": {"mean": round(mean([w["rain_mm_hr"] for w in weather.per_edge.values()]), 2),
                           "max": round(max([w["rain_mm_hr"] for w in weather.per_edge.values()]), 2)},
            "api_3d_mean_mm": round(mean([w["api_3d"] for w in weather.per_edge.values()]), 1),
            "soil_saturation_mean": round(mean([w["soil_saturation"] for w in weather.per_edge.values()]), 3),
            "p_disruption": {
                "mean": round(mean(probs), 4), "median": round(quantile(sorted(probs), 0.5), 4),
                "p90": round(quantile(sorted(probs), 0.90), 4),
                "p99": round(quantile(sorted(probs), 0.99), 4),
                "max": round(max(probs), 4),
            },
            "band_counts": {b: bands.get(b, 0) for b in
                            ("LOW", "MODERATE", "HIGH", "SEVERE", "CRITICAL")},
            "km_at_risk": {
                "high_plus": km(lambda e: e.p_disruption >= 0.35),
                "severe_plus": km(lambda e: e.p_disruption >= 0.60),
                "critical": km(lambda e: e.p_disruption >= 0.80),
            },
            "segments_hard_blocked": len(blocked),
            "blocked_segment_ids": [e.segment_id for e in blocked][:25],
            "severance": self.severance_report(graph),
            "hub_resilience": self.hub_resilience(graph),
            "cost_inflation": {
                "mean_ratio": round(mean(cost_ratio), 3) if cost_ratio else None,
                "max_ratio": round(max(cost_ratio), 3) if cost_ratio else None,
                "total_baseline_s": round(sum(e.base_cost_s for e in graph.edges), 1),
                "total_dynamic_s": round(sum(e.cost_s for e in graph.edges
                                             if e.cost_s != math.inf), 1),
            },
            "top_risk_segments": [{
                "segment_id": e.segment_id, "highway": e.highway, "name": e.name,
                "state": e.state, "p_disruption": round(e.p_disruption, 4),
                "risk_band": e.risk_band, "slope_deg": e.slope_deg,
                "rain_mm_hr": e.weather.get("rain_mm_hr"),
                "api_3d": e.weather.get("api_3d"),
                "soil_saturation": e.weather.get("soil_saturation"),
                "hist_freq": e.hist_freq, "cut_slope": e.cut_slope,
                "expected_closure_hours": round(_closure_hours_of(e), 1),
            } for e in ranked],
            "reweight_seconds": round(elapsed, 4),
            "edges_per_second": round(len(graph.edges) / elapsed, 1) if elapsed > 0 else None,
        }


# --------------------------------------------------------------------------- #
#  TERMINAL PAINTER (headless reporting - no frontend, just stdout)            #
# --------------------------------------------------------------------------- #
class Painter:
    CODES = {"reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
             "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
             "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
             "white": "\033[37m", "orange": "\033[38;5;208m", "grey": "\033[90m"}
    BAND = {"LOW": "green", "MODERATE": "yellow", "HIGH": "orange",
            "SEVERE": "red", "CRITICAL": "magenta"}

    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)

    def c(self, name: str, s: str) -> str:
        if not self.enabled or name not in self.CODES:
            return str(s)
        return f"{self.CODES[name]}{s}{self.CODES['reset']}"

    def band(self, name: str) -> str:
        return self.c(self.BAND.get(name, "white"), name)

    def hdr(self, s: str) -> str:
        return self.c("bold", self.c("cyan", s))

    def kv(self, k: str, v: Any) -> str:
        return f"  {self.c('grey', k.ljust(14))} {v}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
           aligns: Optional[Sequence[str]] = None) -> str:
    cols = len(headers)
    aligns = list(aligns or ["l"] * cols)
    str_rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in str_rows:
        for i in range(cols):
            if i < len(r):
                widths[i] = max(widths[i], len(r[i]))

    def fmt(cells: Sequence[str], bold: bool = False) -> str:
        out = []
        for i in range(cols):
            cell = cells[i] if i < len(cells) else ""
            if aligns[i] == "r":
                out.append(cell.rjust(widths[i]))
            elif aligns[i] == "c":
                out.append(cell.center(widths[i]))
            else:
                out.append(cell.ljust(widths[i]))
        line = "  ".join(out).rstrip()
        return f"  {line}"

    lines = [fmt(headers, True), "  " + "  ".join("-" * w for w in widths)]
    for r in str_rows:
        lines.append(fmt(r))
    return "\n".join(lines)


def _human(n: float) -> str:
    return f"{n:,.0f}"


def _fmt_dur(minutes: float) -> str:
    """Human-readable duration from a value in minutes."""
    m = float(minutes)
    if m < 60:
        return f"{m:.0f} min"
    return f"{int(m // 60)} h {int(round(m % 60)):02d} m"


# --------------------------------------------------------------------------- #
#  SCENARIO RUNNER                                                             #
# --------------------------------------------------------------------------- #
def run_scenario(engine: HazardEngine, scenario: str, policy: CostPolicy,
                 out_dir: str, seed: int = DEFAULT_SEED,
                 corridors: Sequence[Tuple[str, str]] = DEFAULT_CORRIDORS,
                 algorithm: str = "astar", write: bool = True,
                 paint: Optional[Painter] = None) -> Dict[str, Any]:
    """Score + re-weight + route one weather scenario, persisting artifacts."""
    graph = engine.graph
    graph, weather, elapsed = engine.reweight(graph, scenario=scenario,
                                              policy=policy, seed=seed)
    summary = engine.scenario_summary(graph, weather, elapsed, scenario)
    summary["cost_policy"] = policy.to_dict()

    route_reports = []
    for src, dst in corridors:
        if algorithm == "both":
            for alg in ("astar", "dijkstra"):
                route_reports.append(engine.compare_routes(graph, src, dst, algorithm=alg))
        else:
            route_reports.append(engine.compare_routes(graph, src, dst, algorithm=algorithm))
    summary["routes"] = route_reports
    summary["artifacts"] = []

    if write:
        gp = os.path.join(out_dir, f"graph_{scenario}.json")
        save_json(graph.to_geojson(extra_meta={
            "scenario": scenario,
            "title": f"SIH26002 re-weighted road graph - {scenario.upper()} monsoon",
            "weather": weather.to_dict(),
            "cost_policy": policy.to_dict(),
            "risk_bands": [{"band": b, "max_p": m, "colour": c, "action": a}
                           for m, b, c, a in RISK_BANDS],
            "model": {"backend": engine.model.backend,
                      "trained_utc": engine.model.trained_utc,
                      "cv_roc_auc": ((engine.model.metrics.get("cv") or {}).get("roc_auc")),
                      "calibration": engine.model.platt},
            "summary": {k: summary[k] for k in
                        ("edges", "network_km", "p_disruption", "band_counts",
                         "km_at_risk", "segments_hard_blocked", "reweight_seconds")},
        }, include_scores=True), gp)
        summary["artifacts"].append(os.path.relpath(gp, HERE))

        rp = os.path.join(out_dir, f"route_{scenario}.json")
        save_json({"scenario": scenario, "generated_utc": utcnow_iso(),
                   "cost_policy": policy.to_dict(), "corridors": route_reports}, rp)
        summary["artifacts"].append(os.path.relpath(rp, HERE))

        sp = os.path.join(out_dir, f"summary_{scenario}.json")
        save_json(summary, sp)
        summary["artifacts"].append(os.path.relpath(sp, HERE))
    return summary


def write_risk_csv(graph: RoadGraph, path: str, scenario: str) -> str:
    """Flat per-segment risk register (GIS / analyst friendly)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = ("scenario", "segment_id", "state", "district", "highway", "name",
            "lat", "lon", "length_m", "slope_deg", "surface", "soil_type",
            "hist_freq", "rain_mm_hr", "api_3d", "soil_saturation",
            "p_disruption", "risk_band", "transit_s", "base_cost_s", "cost_s",
            "cost_ratio", "recommended_action")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for e in sorted(graph.edges, key=lambda x: -x.p_disruption):
            _b, _c, action = risk_band(e.p_disruption)
            w.writerow([
                scenario, e.segment_id, e.state, e.district, e.highway, e.name,
                f"{e.lat:.6f}", f"{e.lon:.6f}", f"{e.length_m:.1f}",
                f"{e.slope_deg:.2f}", e.surface, e.soil_type, f"{e.hist_freq:.4f}",
                f"{e.weather.get('rain_mm_hr', 0.0):.2f}",
                f"{e.weather.get('api_3d', 0.0):.1f}",
                f"{e.weather.get('soil_saturation', 0.0):.4f}",
                f"{e.p_disruption:.5f}", e.risk_band, f"{e.transit_s:.1f}",
                f"{e.base_cost_s:.1f}",
                ("BLOCKED" if e.cost_s == math.inf else f"{e.cost_s:.1f}"),
                ("inf" if e.cost_s == math.inf else f"{e.cost_s / max(1e-9, e.base_cost_s):.3f}"),
                action,
            ])
    return path


# --------------------------------------------------------------------------- #
#  BENCHMARK / VALIDATION                                                      #
# --------------------------------------------------------------------------- #
def run_benchmark(engine: HazardEngine, policy: CostPolicy,
                  scenario: str = "heavy", repeats: int = 3) -> Dict[str, Any]:
    """Measure the real-time SLA: re-weighting latency and per-edge throughput."""
    graph = engine.graph
    field_ = MonsoonField(scenario=scenario, seed=DEFAULT_SEED, bbox=graph.bbox())
    weather = field_.evaluate(graph.edges)

    timings = []
    for _ in range(max(1, repeats)):
        t0 = time.perf_counter()
        engine.score_edges(graph.edges, weather)
        for e, p in zip(graph.edges, [x for x in engine.score_edges(graph.edges, weather)]):
            e.p_disruption = p
        timings.append(time.perf_counter() - t0)

    # single-edge latency (the real-time incident path)
    probe = graph.edges[len(graph.edges) // 2]
    lat = []
    for _ in range(200):
        t0 = time.perf_counter()
        engine.score_edges([probe], weather)
        lat.append((time.perf_counter() - t0) * 1e6)
    lat_sorted = sorted(lat)

    t0 = time.perf_counter()
    route(graph, "Guwahati", "Shillong", algorithm="astar", mode="risk")
    route_ms = (time.perf_counter() - t0) * 1000.0

    n = len(graph.edges)
    best = min(timings)
    return {
        "edges": n,
        "backend": engine.model.backend,
        "repeats": len(timings),
        "score_and_reweight_s": {
            "best": round(best, 4), "mean": round(mean(timings), 4),
            "worst": round(max(timings), 4),
        },
        "edges_per_second": round(n / best, 0) if best > 0 else None,
        "single_edge_latency_us": {
            "p50": round(lat_sorted[len(lat_sorted) // 2], 1),
            "p95": round(lat_sorted[int(len(lat_sorted) * 0.95)], 1),
            "min": round(lat_sorted[0], 1),
        },
        "astar_route_ms": round(route_ms, 2),
        "sla_seconds": 2.0,
        "sla_pass": bool(best < 2.0),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
    }


def audit_dgp(records: Sequence[Dict[str, Any]], model: HazardRiskModel,
              dgp_overrides: Optional[Dict[str, float]] = None,
              index: Optional[HistoricalHazardIndex] = None) -> Dict[str, Any]:
    """
    Validate the learned model against the synthetic ground-truth DGP:
      * achievable ceiling: AUC of the true generative probability
      * fidelity: Spearman correlation between prediction and truth
      * does the ensemble recover the right sensitivities?
    """
    saved = dict(DGP)
    if dgp_overrides:
        DGP.update(dgp_overrides)
    try:
        X, y, _meta = build_dataset(records, index)
        if not X:
            return {"error": "no labelled records"}
        p_true = [sigmoid(true_disruption_logit(r)) for r in records if "disrupted" in r]
        # prefer out-of-fold predictions: in-sample scores are optimistically biased
        # and can even appear to beat the ground-truth ceiling.
        oof = getattr(model, "oof", None)
        if oof is not None and len(oof[1]) == len(X):
            p_hat = list(oof[1])
            scoring = "out-of-fold"
        else:
            p_hat = model.predict_proba(X)
            scoring = "in-sample"
        n_feat = len(model.features)
        sds = [stdev([row[f] for row in X]) for f in range(n_feat)]
        coef_map = {
            "rain_mm_hr": DGP["rain"], "api_3d": DGP["api3"], "slope_deg": DGP["slope"],
            "soil_saturation": DGP["soil"],
            "hist_freq": DGP["hist"] * (0.30 + 1.15 * mean(
                [float(r.get("soil_saturation", 0.0)) for r in records])),
            "slope_x_rain": DGP["rain_x_slope"] / 10.0,
            "length_m": DGP["length"] / 1000.0, "elev_band": 0.0,
            "ndvi": DGP["ndvi"], "drainage": DGP["drainage"],
            "cut_slope": DGP["cut_x_soil"] * mean([r.get("soil_saturation", 0.5)
                                                   for r in records]),
            "sinuosity": 0.0,
        }
        true_sens = [abs(coef_map.get(f, 0.0)) * sds[i] for i, f in enumerate(model.features)]
        learned = [model.importance.get(f, 0.0) for f in model.features]
        return {
            "n_rows": len(y),
            "dgp_intercept": DGP["intercept"],
            "scoring": scoring,
            "auc_true_probability": round(roc_auc(y, p_true), 4),
            "auc_model": round(roc_auc(y, p_hat), 4),
            "log_loss_true": round(log_loss(y, p_true), 4),
            "log_loss_model": round(log_loss(y, p_hat), 4),
            "spearman_pred_vs_truth": round(spearman(p_hat, p_true), 4),
            "spearman_importance_vs_true_sensitivity": round(spearman(learned, true_sens), 4),
            "true_standardised_sensitivity": {
                f: round(true_sens[i], 5) for i, f in enumerate(model.features)},
            "learned_importance": {f: round(v, 4) for f, v in model.importance.items()},
        }
    finally:
        DGP.clear()
        DGP.update(saved)


# --------------------------------------------------------------------------- #
#  REPORT RENDERING                                                            #
# --------------------------------------------------------------------------- #
def render_report(ctx: Dict[str, Any], paint: Painter) -> str:
    W = 88
    out: List[str] = []
    line = "=" * W

    out.append(line)
    out.append(paint.hdr(f" SIH26002 - HAZARD PREDICTION ENGINE  v{__version__}"))
    out.append(f" Predictive Route Optimization for Disaster-Prone Terrain")
    out.append(f" {__team__} | backend: {ctx['backend']} | seed: {ctx['seed']}")
    out.append(line)

    # ---- 1. inputs -------------------------------------------------------
    g = ctx["graph_stats"]
    h = ctx["hazard_stats"]
    out.append("")
    out.append(paint.hdr("[1/5] INPUTS"))
    out.append(paint.kv("graph", f"{os.path.relpath(ctx['graph_path'], HERE)} - "
                                 f"{_human(g['n_edges'])} edges / {_human(g['n_nodes'])} nodes / "
                                 f"{g['total_length_km']:,.1f} km / {g['n_cross_links']} cross-links"))
    out.append(paint.kv("corridors", f"{g['n_corridors']} NH corridors across {g['n_states']} states"))
    out.append(paint.kv("hazard log", f"{os.path.relpath(ctx['hazard_path'], HERE)} - "
                                      f"{_human(h['records'])} observations, "
                                      f"{_human(h['disruption_events'])} disruptions "
                                      f"({100 * h['positive_rate']:.1f}%)"))
    mix = ", ".join(f"{k} {v}" for k, v in list(h["hazard_type_mix"].items())[:4])
    out.append(paint.kv("hazard mix", mix))
    out.append(paint.kv("base risk", f"global prior {h['global_prior_events_per_km']:.2f} "
                                     f"events/km/season, EB shrinkage k={h['eb_shrinkage_k']}"))
    out.append(paint.kv("backends", f"{', '.join(ctx['backend_stack'])} "
                                    f"(numpy={ctx['packages']['numpy']}, "
                                    f"pandas={ctx['packages']['pandas']}, "
                                    f"networkx={ctx['packages']['networkx']})"))

    # ---- 2. model --------------------------------------------------------
    m = ctx["model"]
    cv = m.get("cv", {})
    cal = cv.get("calibrated") or cv
    out.append("")
    out.append(paint.hdr("[2/5] MODEL  P_disruption(e) = f(rain, API_3d, slope, soil_sat, hist_freq)"))
    out.append(paint.kv("training", f"{_human(m['n_rows'])} rows | {_human(m['n_positive'])} positive "
                                    f"({100 * m['positive_rate']:.2f}%) | {cv.get('folds')}-fold "
                                    f"stratified CV | {ctx['train_seconds']:.2f} s"))
    rows = [
        ["ROC AUC (CV)", f"{cv.get('roc_auc', float('nan')):.4f} +/- {cv.get('auc_std', 0):.4f}",
         "Log loss", f"{cal.get('log_loss', 0):.4f}"],
        ["PR AUC (CV)", f"{cv.get('pr_auc', float('nan')):.4f}",
         "Brier score", f"{cal.get('brier', 0):.4f}"],
        ["KS statistic", f"{cal.get('ks', 0):.4f}",
         "Brier skill vs base", f"{cal.get('brier_skill_vs_base', 0):+.4f}"],
        ["Precision @thr", f"{cal.get('precision', 0):.4f}",
         "ECE (calibration)", f"{cal.get('ece', 0):.4f}"],
        ["Recall @thr", f"{cal.get('recall', 0):.4f}",
         "F1 @thr", f"{cal.get('f1', 0):.4f}"],
    ]
    out.append(_table(["metric", "value", "metric", "value"], rows, ["l", "r", "l", "r"]))
    imp = sorted((m.get("feature_importance") or ctx["importance"]).items(),
                 key=lambda kv: -kv[1])[:6]
    out.append(paint.kv("importance", " | ".join(f"{k} {v:.2f}" for k, v in imp)))
    out.append(paint.kv("calibration", f"Platt a={ctx['platt'][0]:.4f} b={ctx['platt'][1]:.4f}"
                                       f"{' (disabled)' if not ctx['calibrated'] else ''}"))
    lift = cv.get("band_lift") or []
    if lift:
        out.append("")
        out.append(paint.c("grey", "  operational validation on out-of-fold predictions "
                                   "(what actually happened in each band):"))
        rows = [[b["band"], f"{b['p_range'][0]:.2f}-{b['p_range'][1]:.2f}", b["n"],
                 f"{100 * b['share_of_network']:.1f}%",
                 ("-" if b["observed_disruption_rate"] is None
                  else f"{100 * b['observed_disruption_rate']:.1f}%"),
                 ("-" if b["mean_predicted"] is None
                  else f"{100 * b['mean_predicted']:.1f}%"),
                 ("-" if b["lift_vs_base_rate"] is None
                  else f"{b['lift_vs_base_rate']:.2f}x")] for b in lift]
        out.append(_table(["risk band", "P range", "n", "share", "observed",
                           "predicted", "lift"], rows,
                          ["l", "c", "r", "r", "r", "r", "r"]))
    if ctx.get("audit"):
        a = ctx["audit"]
        out.append(paint.kv("DGP audit", f"[{a['scoring']}] truth-ceiling AUC "
                                         f"{a['auc_true_probability']:.4f} vs model "
                                         f"{a['auc_model']:.4f} | logloss "
                                         f"{a['log_loss_true']:.4f} vs {a['log_loss_model']:.4f}"))
        out.append(paint.kv("", f"rho(prediction, ground truth) = "
                                f"{a['spearman_pred_vs_truth']:.3f} | "
                                f"rho(importance, true sensitivity) = "
                                f"{a['spearman_importance_vs_true_sensitivity']:.3f}"))
    out.append(paint.kv("model card", os.path.relpath(ctx["model_path"], HERE)))

    # ---- 3. scenario sweep ----------------------------------------------
    out.append("")
    out.append(paint.hdr(f"[3/5] SCENARIO SWEEP  cargo={ctx['policy']['cargo']} "
                         f"(S={ctx['policy']['S']}) alpha={ctx['policy']['alpha']} "
                         f"beta={ctx['policy']['beta']} gamma={ctx['policy']['gamma']} "
                         f"block P>={ctx['policy']['block_threshold']}"))
    rows = []
    for s in ctx["scenarios"]:
        b = s["band_counts"]
        sev = s.get("severance") or {}
        rows.append([
            s["scenario"].upper(),
            f"{s['rain_mm_hr']['mean']:.1f}/{s['rain_mm_hr']['max']:.0f}",
            f"{s['api_3d_mean_mm']:.0f}",
            f"{s['soil_saturation_mean']:.2f}",
            f"{s['p_disruption']['mean']:.4f}",
            f"{s['p_disruption']['p99']:.3f}",
            f"{s['p_disruption']['max']:.3f}",
            f"{b['LOW']}/{b['MODERATE']}/{b['HIGH']}/{b['SEVERE']}/{b['CRITICAL']}",
            f"{s['km_at_risk']['high_plus']:.0f}",
            f"{s['segments_hard_blocked']}",
            f"{sev.get('accessibility_index', 1.0):.2f}",
            f"{s['reweight_seconds']:.2f}s",
        ])
    out.append(_table(["scenario", "rain mm/h", "API3", "soil", "P mean", "P p99",
                       "P max", "LOW/MOD/HIGH/SEV/CRT", "km>=.35", "cut", "access",
                       "reweight"],
                      rows, ["l", "r", "r", "r", "r", "r", "r", "c", "r", "r", "r", "r"]))
    out.append(paint.c("grey", "  rain = mean/max mm/hr | bands = segment counts | "
                               "km>=.35 = carriageway at MODERATE risk or worse |"))
    out.append(paint.c("grey", "  cut = segments treated as impassable | access = hubs "
                               "still reachable from Guwahati (NER Accessibility Index)"))
    for s in ctx["scenarios"]:
        label = s["scenario"].upper()
        pad = " " * max(1, 9 - len(label))
        top = s["top_risk_segments"][:3]
        bits = ", ".join(f"{t['segment_id']} {t['highway']} P={t['p_disruption']:.2f}"
                         for t in top)
        out.append(f"  {paint.band(label)}{pad}{paint.c('grey', 'worst segments:')} {bits}")
        sev = s.get("severance") or {}
        if sev.get("severed_hubs"):
            out.append(f"  {label.ljust(9)}{paint.c('red', 'severed hubs:')} "
                       f"{', '.join(sev['severed_hubs'])}")

    # ---- 4. routing ------------------------------------------------------
    out.append("")
    out.append(paint.hdr(f"[4/5] RISK-AWARE ROUTING  algorithm={ctx['algorithm']}"))
    hr = [s.get("hub_resilience") or {} for s in ctx["scenarios"]]
    if hr and "hub_pairs" in hr[0]:
        out.append(paint.c("grey", f"  hub-to-hub resilience: {hr[0]['named_hubs']} named hubs, "
                                   f"{hr[0]['hub_pairs']} pairs, exact connectivity + a "
                                   f"{hr[0]['sampled_pairs']}-pair route audit"))
        rows = []
        for s, h in zip(ctx["scenarios"], hr):
            so, sd = h.get("sample_outcome", {}), h.get("sample_deltas", {})
            det = (sd.get("mean_distance_pct") is not None)
            rows.append([
                s["scenario"].upper(),
                f"{100 * h['connectivity_index']:.1f}%",
                f"{h['n_components']}",
                f"{so.get('held', 0)}",
                f"{so.get('rerouted', 0)}",
                f"{so.get('severed', 0)}",
                (f"+{sd['mean_distance_pct']:.1f}% km / +{sd['mean_transit_pct']:.1f}% t"
                 if det else "-"),
                (f"{sd['mean_exposure_pct']:.1f}%" if det else "-"),
            ])
        out.append(_table(["scenario", "connectivity", "components", "held", "rerouted",
                           "severed", "detour cost", "exposure delta"], rows,
                          ["l", "r", "r", "r", "r", "r", "l", "r"]))
        out.append("")
    for s in ctx["scenarios"]:
        out.append(f"  {paint.c('bold', s['scenario'].upper())}")
        rows = []
        for r in s["routes"]:
            d = r.get("delta") or {}
            base, risk = r["baseline"], r["risk_aware"]
            alg = "" if r["algorithm"] == ctx["algorithm"] else f" [{r['algorithm']}]"
            if not base["found"]:
                rows.append([r["corridor"] + alg, "-", "NO PATH", "-", "-", "-", "-", "-"])
                continue
            rows.append([
                r["corridor"] + alg, "static",
                f"{base['distance_km']:.1f} km", _fmt_dur(base["transit_min"]),
                f"{base['risk_exposure']:.3f}", f"{base['max_p']:.2f}",
                f"{base['expected_closure_hours']:.0f} h",
                paint.c("red", f"{base['impassable_segments']} CUT")
                if base["impassable_segments"] else "passable",
            ])
            if not risk["found"]:
                rows.append(["", "hazard", "NO PATH", "-", "-", "-", "-",
                             paint.c("red", "CORRIDOR SEVERED")])
                continue
            if d.get("severed"):
                if d.get("rerouted"):
                    verdict = paint.c("red", f"SEVERED -> least-risk convoy route "
                                             f"{d['distance_pct']:+.0f}% km")
                else:
                    verdict = paint.c("red", "SEVERED -> shortest path is also least-risk")
            elif d.get("rerouted"):
                cut = d.get("baseline_impassable_segments", 0)
                if cut:
                    # the detour exists because the shortest path is impassable,
                    # so lead with the closure it removes, not with mean P
                    verdict = paint.c("yellow",
                                      f"AVOIDED {cut} CUT segment"
                                      f"{'s' if cut > 1 else ''} "
                                      f"({d['distance_pct']:+.0f}% km)")
                else:
                    verdict = paint.c("yellow",
                                      f"REROUTED {d['distance_pct']:+.0f}% km, "
                                      f"exposure {d['risk_exposure_pct']:+.0f}%")
            else:
                verdict = paint.c("green", "route holds")
            rows.append([
                "", "hazard",
                f"{risk['distance_km']:.1f} km", _fmt_dur(risk["transit_min"]),
                f"{risk['risk_exposure']:.3f}", f"{risk['max_p']:.2f}",
                f"{risk['expected_closure_hours']:.0f} h", verdict,
            ])
        out.append(_table(["corridor", "engine", "distance", "transit", "P exp",
                           "max P", "closure", "verdict"],
                          rows, ["l", "l", "r", "r", "r", "r", "r", "l"]))
    out.append(paint.c("grey", "  static = risk-blind C(e)|P=0 (what OSRM/Google Maps "
                               "would return); hazard = full C(e)"))
    out.append(paint.c("grey", "  P exp = length-weighted mean P_disruption on the "
                               "chosen path; closure = expected clearance hours if hit"))
    out.append(paint.c("grey", "  a detour can raise mean exposure when it trades one "
                               "hard-blocked segment for several moderately risky ones;"))
    out.append(paint.c("grey", "  the cost function minimises C(e), not P, which is why "
                               "the CUT column is the operative signal"))

    # ---- 5. benchmark + artifacts ---------------------------------------
    out.append("")
    out.append(paint.hdr("[5/5] PERFORMANCE & ARTIFACTS"))
    if ctx.get("benchmark"):
        b = ctx["benchmark"]
        verdict = paint.c("green", "PASS") if b["sla_pass"] else paint.c("red", "FAIL")
        out.append(paint.kv("re-weight", f"{_human(b['edges'])} edges in "
                                         f"{b['score_and_reweight_s']['best']:.3f} s "
                                         f"({_human(b['edges_per_second'])} edges/s) "
                                         f"— SLA < {b['sla_seconds']} s : {verdict}"))
        out.append(paint.kv("latency", f"single edge p50 {b['single_edge_latency_us']['p50']:.0f} us / "
                                       f"p95 {b['single_edge_latency_us']['p95']:.0f} us | "
                                       f"A* corridor {b['astar_route_ms']:.1f} ms"))
    for a in ctx["artifacts"]:
        out.append(f"  {paint.c('grey', '->')} {a}")
    out.append(line)
    out.append(paint.c("grey", f" generated {utcnow_iso()} | python {sys.version.split()[0]} | "
                               f"{platform.system()} {platform.machine()}"))
    out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
#  SELF-TEST SUITE  (stdlib only, runs offline, ~20 s)                         #
# --------------------------------------------------------------------------- #
def _toy_graph(edges: int = 140, seed: int = 7) -> RoadGraph:
    return build_mock_osm_graph(target_edges=edges, seed=seed)


def _toy_dataset(n: int = 900, seed: int = 11
                 ) -> Tuple[List[List[float]], List[int], List[float]]:
    """Synthetic labelled set *plus* its true generative probability.

    Returning p_true lets a test score a learner against the Bayes ceiling
    instead of against an arbitrary absolute AUC.
    """
    rng = random.Random(seed)
    X, y, pt = [], [], []
    for _ in range(n):
        rain = rng.gammavariate(1.6, 12.0)
        slope = rng.uniform(0.0, 24.0)
        sat = clamp(rain / 90.0 + rng.gauss(0.0, 0.1), 0.0, 1.0)
        hist = rng.gammavariate(1.1, 0.7)
        z = -3.0 + 0.05 * rain + 0.11 * slope + 2.0 * sat + 0.5 * hist
        p = sigmoid(z)
        pt.append(p)
        X.append([rain, 8.0 * rain + rng.gauss(0, 4), slope, sat, hist,
                  math.tan(math.radians(slope)) * rain, rng.uniform(200, 1500),
                  rng.random(), rng.random(), rng.random(), 1 if slope > 10 else 0,
                  rng.uniform(1.0, 1.4)])
        y.append(1 if rng.random() < p else 0)
    return X, y, pt


def _largest_component(graph: RoadGraph) -> int:
    adj = graph.adjacency()
    seen: Set[str] = set()
    best = 0
    for start in adj:
        if start in seen:
            continue
        stack = [start]
        size = 0
        seen.add(start)
        while stack:
            u = stack.pop()
            size += 1
            for _ei, v in adj.get(u, ()):
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        best = max(best, size)
    return best


def run_selftest(paint: Optional[Painter] = None, quiet: bool = False) -> int:
    """Execute the built-in verification suite; returns a process exit code."""
    paint = paint or Painter(enabled=False)
    results: List[Tuple[str, bool, str]] = []

    def check(name: str, fn: Callable[[], Optional[str]]) -> None:
        t0 = time.perf_counter()
        try:
            note = fn() or ""
            results.append((name, True, note + (f"  ({time.perf_counter() - t0:.2f}s)"
                                                if time.perf_counter() - t0 > 0.4 else "")))
        except AssertionError as exc:
            results.append((name, False, str(exc)))
        except Exception as exc:                               # pragma: no cover
            results.append((name, False, f"{type(exc).__name__}: {exc}"))

    # -- geo / graph --------------------------------------------------------
    def t_haversine() -> str:
        d = haversine_m(*NER_CITIES["Guwahati"][:2], *NER_CITIES["Shillong"][:2]) / 1000.0
        assert 40.0 < d < 110.0, f"Guwahati-Shillong great-circle = {d:.1f} km, expected ~65"
        assert abs(haversine_m(0, 0, 0, 0)) < 1e-6
        assert haversine_m(26.0, 91.0, 26.0, 91.01) > 900.0
        return f"{d:.1f} km Guwahati->Shillong"
    check("haversine / geo maths", t_haversine)

    def t_determinism() -> str:
        a, b = _toy_graph(seed=5), _toy_graph(seed=5)
        assert a.metadata["sha1"] == b.metadata["sha1"], "graph generation is not deterministic"
        c = _toy_graph(seed=6)
        assert c.metadata["sha1"] != a.metadata["sha1"], "seed has no effect"
        return f"{len(a.edges)} edges, sha1={a.metadata['sha1']}"
    check("graph generation is deterministic", t_determinism)

    def t_integrity() -> str:
        g = _toy_graph(edges=220, seed=3)
        assert len(g.edges) >= 100, f"only {len(g.edges)} edges generated"
        ids = set()
        for e in g.edges:
            assert e.length_m > 1.0, f"{e.segment_id} has non-positive length"
            assert 0.0 <= e.slope_deg < 65.0, f"{e.segment_id} slope {e.slope_deg}"
            assert e.u in g.nodes and e.v in g.nodes, f"{e.segment_id} references unknown node"
            assert e.segment_id not in ids, f"duplicate segment id {e.segment_id}"
            ids.add(e.segment_id)
            assert 5.0 <= e.speed_kmh <= 90.0, f"{e.segment_id} speed {e.speed_kmh}"
            assert 0.0 < e.drainage <= 1.0 and 0.0 < e.ndvi <= 1.0
        comp = _largest_component(g)
        frac = comp / max(1, len(g.nodes))
        assert frac >= 0.80, f"largest connected component only {100 * frac:.0f}% of nodes"
        return (f"{len(g.edges)} edges / {len(g.nodes)} nodes, "
                f"{g.total_length_km():.0f} km, largest component {100 * frac:.0f}%")
    check("graph integrity & connectivity", t_integrity)

    def t_roundtrip() -> str:
        g = _toy_graph(edges=160, seed=4)
        gj = json.loads(json.dumps(g.to_geojson()))
        g2 = RoadGraph.from_geojson(gj)
        assert len(g2.edges) == len(g.edges), "edge count changed in GeoJSON round-trip"
        assert {e.segment_id for e in g2.edges} == {e.segment_id for e in g.edges}
        assert len(g2.nodes) == len(g.nodes), "node count changed in GeoJSON round-trip"
        m = {e.segment_id: e for e in g.edges}
        for e in g2.edges:
            o = m[e.segment_id]
            assert abs(o.length_m - e.length_m) < 0.05, f"{e.segment_id} length drift"
            assert abs(o.slope_deg - e.slope_deg) < 0.01
            assert e.coords[0] == o.coords[0]
        return f"{len(g2.edges)} edges survive round-trip"
    check("GeoJSON round-trip is lossless", t_roundtrip)

    # -- data / features ----------------------------------------------------
    def t_hazard_log() -> str:
        g = _toy_graph(edges=180, seed=9)
        rows = build_historical_hazard_log(g, seed=9)
        assert len(rows) > 300, f"only {len(rows)} rows"
        rate = mean([r["disrupted"] for r in rows])
        assert 0.07 <= rate <= 0.24, f"positive rate {rate:.3f} out of plausible band"
        for r in rows:
            assert r["rain_mm_hr"] >= 0.0 and r["api_3d_mm"] >= 0.0
            assert 0.0 <= r["soil_saturation"] <= 1.0
            if r["disrupted"]:
                assert r["hazard_type"] in HAZARD_TYPES, r["hazard_type"]
                assert r["closure_hours"] > 0.0
            else:
                assert r["hazard_type"] == "none" and r["closure_hours"] == 0.0
        return f"{len(rows)} rows, {100 * rate:.1f}% disrupted, intercept={DGP['intercept']}"
    check("historical hazard log generator", t_hazard_log)

    def t_index() -> str:
        g = _toy_graph(edges=180, seed=9)
        rows = build_historical_hazard_log(g, seed=9)
        idx = HistoricalHazardIndex(rows)
        assert idx.events > 0
        n_attached = idx.attach_to_graph(g)
        assert n_attached == len(g.edges), "not every edge received a base risk"
        unseen = RoadEdge(segment_id="GHOST", u="a", v="b", coords=[[91.0, 26.0], [91.01, 26.01]],
                          length_m=900.0, elev_from=100.0, elev_to=120.0, slope_deg=5.0,
                          state="Assam", highway="NH-27")
        g.add_edge(unseen)
        idx.attach_to_graph(g)
        assert unseen.hist_freq > 0.0, "unseen segment did not inherit the group prior"
        hot = [e for e in g.edges if e.segment_id in idx.seg_hits
               and idx.seg_hits[e.segment_id] >= 3]
        assert not hot or mean([e.hist_freq for e in hot]) >= idx.global_prior, \
            "EB shrinkage is not monotone in event count"
        return (f"{idx.events} events -> global prior {idx.global_prior:.2f}/km, "
                f"top segment {idx.top_segments(1)[0][0]}={idx.top_segments(1)[0][1]:.2f}")
    check("empirical-Bayes base-risk index", t_index)

    def t_features() -> str:
        g = _toy_graph(edges=120, seed=13)
        e = g.edges[0]
        w = {"rain_mm_hr": 20.0, "api_3d": 120.0, "soil_saturation": 0.7}
        v = edge_features(e, w)
        assert len(v) == len(FEATURES), "feature vector length mismatch"
        assert all(x == x for x in v), "NaN in feature vector"
        assert abs(v[0] - 20.0) < 1e-9 and abs(v[2] - e.slope_deg) < 1e-9
        assert abs(v[5] - math.tan(math.radians(e.slope_deg)) * 20.0) < 1e-9
        return f"{len(v)} features: {', '.join(FEATURES[:5])}, ..."
    check("feature engineering schema", t_features)

    # -- learning -----------------------------------------------------------
    def t_metrics() -> str:
        y = [0, 0, 1, 1]
        p = [0.1, 0.4, 0.35, 0.8]
        assert abs(roc_auc(y, p) - 0.75) < 1e-9, f"AUC {roc_auc(y, p)}"
        assert roc_auc([0, 1], [0.2, 0.9]) == 1.0
        cm = confusion_at([1, 0, 1, 0], [0.9, 0.8, 0.2, 0.1], 0.5)
        assert (cm["tp"], cm["fp"], cm["fn"], cm["tn"]) == (1, 1, 1, 1)
        assert abs(log_loss([1, 0], [0.5, 0.5]) - math.log(2)) < 1e-9
        assert abs(brier_score([1, 0], [1.0, 0.0])) < 1e-12
        assert len(reliability_bins(y, p, 5)) == 5
        folds = stratified_kfold([1] * 20 + [0] * 80, k=5, seed=1)
        assert len(folds) == 5
        for tr, te in folds:
            assert not (set(tr) & set(te))
            assert mean([1] * 0 + [1 if i < 20 else 0 for i in te]) > 0
        return "AUC / PR / logloss / folds verified"
    check("metric implementations", t_metrics)

    def t_pure_learns() -> str:
        X, y, _pt = _toy_dataset(seed=21)
        m = PureGBDT(n_estimators=60, max_depth=3, learning_rate=0.12, seed=21).fit(X, y)
        p = m.predict_proba(X)
        auc = roc_auc(y, p)
        assert auc > 0.88, f"pure-Python GBDT AUC {auc:.3f} <= 0.88"
        imp = m.feature_importances()
        assert imp, "no feature importances from pure backend"
        d = json.loads(json.dumps(m.to_dict()))
        m2 = PureGBDT.from_dict(d)
        p2 = m2.predict_proba(X)
        assert max(abs(a - b) for a, b in zip(p, p2)) < 1e-12, "serialisation is lossy"
        return f"AUC {auc:.3f}, {len(m.trees)} trees, serialisation lossless"
    check("pure-Python Newton boosting learns", t_pure_learns)

    def t_platt() -> str:
        rng = random.Random(3)
        z, y = [], []
        for _ in range(1400):
            t = 1 if rng.random() < 0.2 else 0
            zz = rng.gauss(2.2 * t - 1.4, 1.1)
            z.append(zz)
            y.append(1 if rng.random() < sigmoid(0.7 * zz - 0.9) else 0)
        raw = [sigmoid(v) for v in z]
        a, b = fit_platt(z, y)
        cal = [sigmoid(a * v + b) for v in z]
        assert a > 0.0
        e0, e1 = expected_calibration_error(y, raw), expected_calibration_error(y, cal)
        assert e1 <= e0 + 0.01, f"Platt made calibration worse ({e0:.4f} -> {e1:.4f})"
        return f"a={a:.3f} b={b:.3f}, ECE {e0:.4f} -> {e1:.4f}"
    check("Platt calibration layer", t_platt)

    def t_model_wrapper() -> str:
        X, y, p_true = _toy_dataset(n=1400, seed=31)
        ceiling = roc_auc(y, p_true)
        m = HazardRiskModel(backend="pure", seed=31,
                            hyperparams={"n_estimators": 160, "learning_rate": 0.12,
                                         "max_depth": 3})
        mets = m.fit(X, y, folds=3)
        auc = mets["cv"]["roc_auc"]
        # judged against the Bayes ceiling of the generator, not a magic constant
        assert auc > 0.70, f"CV AUC {auc:.3f} is below any useful discriminator"
        assert auc >= 0.88 * ceiling, \
            f"CV AUC {auc:.3f} recovers <88% of the achievable ceiling {ceiling:.3f}"
        assert 0.0 <= mets["cv"]["ece"] <= 1.0
        path = os.path.join(OUT_DIR, "_selftest_model.json")
        m.save(path)
        m2 = HazardRiskModel.load(path)
        p1, p2 = m.predict_proba(X[:120]), m2.predict_proba(X[:120])
        assert max(abs(a - b) for a, b in zip(p1, p2)) < 1e-9, "save/load drift"
        os.remove(path)
        return (f"CV AUC {auc:.3f} vs Bayes ceiling {ceiling:.3f} "
                f"({100 * auc / ceiling:.0f}% recovered), ECE {mets['cv']['ece']:.4f}, "
                f"save/load lossless")
    check("HazardRiskModel fit / save / load", t_model_wrapper)

    # -- cost & routing -----------------------------------------------------
    def t_cost() -> str:
        e = RoadEdge(segment_id="S1", u="a", v="b", coords=[[91.0, 26.0], [91.005, 26.005]],
                     length_m=700.0, elev_from=300.0, elev_to=360.0, slope_deg=8.0,
                     speed_kmh=40.0, elevation_m=330.0)
        pol = CostPolicy()
        c0, t0, h0 = pol.cost(e, 0.0)
        c1, _, _ = pol.cost(e, 0.5)
        c2, _, _ = pol.cost(e, 1.0)
        assert c0 < c1 < c2, "cost is not monotonically increasing in P_disruption"
        assert abs(t0 - 700.0 / (40.0 / 3.6)) < 1e-6, f"transit time {t0}"
        assert abs(h0 - math.tan(math.radians(8.0)) * 700.0 / V_REF) < 1e-6
        assert abs(c0 - (t0 + pol.beta * h0 + pol.gamma * 1.0 * CARGO_OVERHEAD_S)) < 1e-6
        haz = CostPolicy(cargo="hazmat")
        per = CostPolicy(cargo="perishable")
        assert haz.cost(e, 0.3)[0] > per.cost(e, 0.3)[0] > pol.cost(e, 0.3)[0]
        blk = CostPolicy(block_threshold=0.7)
        assert blk.cost(e, 0.9)[0] == math.inf, "hard-blocked segment is not impassable"
        assert blk.cost(e, 0.5)[0] < math.inf
        mul = CostPolicy(cargo_mode="multiplied", cargo="hazmat")
        assert mul.cost(e, 0.2)[0] > pol.cost(e, 0.2)[0]
        return f"C(P=0)={c0:.1f}s C(P=0.5)={c1:.1f}s C(P=1)={c2:.1f}s"
    check("dynamic cost function C(e)", t_cost)

    def t_tiny_dijkstra() -> str:
        g = RoadGraph()
        for nid, (la, lo) in {"A": (26.0, 91.0), "B": (26.01, 91.0),
                              "C": (26.02, 91.0), "D": (26.01, 91.02)}.items():
            g.add_node(nid, la, lo)
        for i, (u, v, L, sp) in enumerate([("A", "B", 1000.0, 36.0), ("B", "C", 1000.0, 36.0),
                                           ("A", "D", 1500.0, 36.0), ("D", "C", 1500.0, 36.0)]):
            g.add_edge(RoadEdge(segment_id=f"E{i}", u=u, v=v,
                                coords=[[g.nodes[u]["lon"], g.nodes[u]["lat"]],
                                        [g.nodes[v]["lon"], g.nodes[v]["lat"]]],
                                length_m=L, elev_from=0.0, elev_to=0.0, slope_deg=0.0,
                                speed_kmh=sp))
        for e in g.edges:
            e.base_cost_s = e.transit_s = e.length_m / (e.speed_kmh / 3.6)
            e.cost_s = e.base_cost_s
            e.p_disruption = 0.0
            e.risk_band = "LOW"
        r = dijkstra(g, "A", "C", lambda e: e.cost_s)
        assert r is not None, "no path found"
        cost, nodes, eidx = r
        assert nodes == ["A", "B", "C"], f"path {nodes}"
        assert abs(cost - 200.0) < 1e-6, f"cost {cost}"
        a = astar(g, "A", "C", lambda e: e.cost_s)
        assert a is not None and abs(a[0] - cost) < 1e-6, "A* disagrees with Dijkstra"
        g.edges[0].cost_s = math.inf
        r2 = dijkstra(g, "A", "C", lambda e: e.cost_s)
        assert r2 is not None and r2[1] == ["A", "D", "C"], "blocked edge not avoided"
        return "A-B-C = 200.0 s; A* matches; blocked edge forces detour"
    check("Dijkstra / A* correctness", t_tiny_dijkstra)

    def t_engine_reweight() -> str:
        g = _toy_graph(edges=200, seed=17)
        rows = build_historical_hazard_log(g, seed=17)
        idx = HistoricalHazardIndex(rows)
        idx.attach_to_graph(g)
        X, y, _ = build_dataset(rows, idx)
        m = HazardRiskModel(backend="pure", seed=17,
                            hyperparams={"n_estimators": 40, "max_depth": 3})
        m.fit(X, y, folds=3)
        eng = HazardEngine(m, g, CostPolicy(), index=idx, records=rows)
        g2, w, el = eng.reweight(g, scenario="heavy", seed=17)
        ps = [e.p_disruption for e in g2.edges]
        assert all(0.0 <= p <= 1.0 for p in ps), "P_disruption outside [0,1]"
        assert max(ps) > min(ps), "weather field produced no risk spread"
        g3, w3, _ = eng.reweight(g, scenario="extreme", seed=17)
        assert mean([e.p_disruption for e in g3.edges]) > mean(ps), \
            "extreme scenario is not riskier than heavy"
        assert w3.rain_mm_hr > w.rain_mm_hr
        r = eng.compare_routes(g3, "Guwahati", "Shillong")
        assert r["baseline"]["found"], "baseline route not found"
        assert r["risk_aware"]["found"], "risk-aware route not found"
        assert r["risk_aware"]["impassable_segments"] <= r["baseline"]["impassable_segments"], \
            "hazard-aware route crosses more impassable segments than the static one"
        if r["baseline"]["impassable_segments"] == 0 and not r["severed"]:
            assert r["risk_aware"]["risk_exposure"] <= r["baseline"]["risk_exposure"] + 1e-9, \
                "risk-aware route is riskier than a passable baseline"
        return (f"{len(g2.edges)} edges scored in {el:.3f}s, "
                f"P mean {mean(ps):.3f}, corridor {r['corridor']}")
    check("engine re-weight + rerouting", t_engine_reweight)

    def t_sla() -> str:
        g = build_mock_osm_graph(target_edges=1000, seed=DEFAULT_SEED)
        rows = build_historical_hazard_log(g, seed=DEFAULT_SEED)
        idx = HistoricalHazardIndex(rows)
        idx.attach_to_graph(g)
        X, y, _ = build_dataset(rows, idx)
        backend = resolve_backend("auto")
        hp = {"n_estimators": 45, "max_depth": 3} if backend == "pure" else {"n_estimators": 90}
        m = HazardRiskModel(backend=backend, seed=DEFAULT_SEED, hyperparams=hp)
        m.fit(X, y, folds=3)
        eng = HazardEngine(m, g, CostPolicy(), index=idx)
        best = min(eng.reweight(g, scenario="heavy", seed=DEFAULT_SEED)[2] for _ in range(2))
        assert best < 2.0, f"re-weighting {len(g.edges)} edges took {best:.2f}s (SLA 2.0s)"
        return f"{len(g.edges)} edges re-weighted in {best:.3f}s with backend '{backend}'"
    check("real-time SLA (<2 s / 1,000 edges)", t_sla)

    def t_bands() -> str:
        assert risk_band(0.0)[0] == "LOW"
        assert risk_band(0.1499)[0] == "LOW"
        assert risk_band(0.15)[0] == "MODERATE"
        assert risk_band(0.35)[0] == "HIGH"
        assert risk_band(0.6)[0] == "SEVERE"
        assert risk_band(0.8)[0] == "CRITICAL"
        assert risk_band(1.0)[0] == "CRITICAL"
        return "5 bands, boundaries verified"
    check("risk band assignment", t_bands)

    # -- report -------------------------------------------------------------
    n_pass = sum(1 for _n, ok, _m in results if ok)
    n_fail = len(results) - n_pass
    if not quiet:
        print()
        print(paint.hdr(" SELF-TEST SUITE — SIH26002 Hazard Prediction Engine"))
        print(" " + "-" * 78)
        for name, ok, note in results:
            mark = paint.c("green", "[PASS]") if ok else paint.c("red", "[FAIL]")
            print(f" {mark} {name}")
            if note:
                print(f"        {paint.c('grey', note)}")
        print(" " + "-" * 78)
        verdict = paint.c("green", "ALL CHECKS PASSED") if n_fail == 0 \
            else paint.c("red", f"{n_fail} CHECK(S) FAILED")
        print(f" {n_pass}/{len(results)} passed — {verdict}")
        print()
    return 0 if n_fail == 0 else 1


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hazard_prediction_engine.py",
        description="SIH26002 - Segment Disruption Risk scoring and monsoon-aware "
                    "re-weighting of OpenStreetMap road graphs.",
        epilog="Examples:\n"
               "  python hazard_prediction_engine.py\n"
               "  python hazard_prediction_engine.py --scenario heavy --cargo hazmat\n"
               "  python hazard_prediction_engine.py --alpha 4 --beta 1.2 --gamma 2\n"
               "  python hazard_prediction_engine.py --backend pure --benchmark\n"
               "  python hazard_prediction_engine.py --corridor 'Guwahati->Silchar' --json\n"
               "  python hazard_prediction_engine.py --selftest\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    io_ = p.add_argument_group("inputs / outputs")
    io_.add_argument("--graph", default=DEFAULT_GRAPH_PATH,
                     help="OSM-style GeoJSON road graph (generated if absent)")
    io_.add_argument("--hazards", default=DEFAULT_HAZARD_PATH,
                     help="historical hazard log CSV/JSON (generated if absent)")
    io_.add_argument("--model", default=None,
                     help="model card path (default: <out>/hazard_model.json)")
    io_.add_argument("--out", default=OUT_DIR, help="output directory")
    io_.add_argument("--regenerate-data", action="store_true",
                     help="rebuild data/ artifacts even if they exist")
    io_.add_argument("--register", metavar="SCENARIO", default=None,
                     help="also write a flat per-segment risk CSV for this scenario")

    d_ = p.add_argument_group("data generation")
    d_.add_argument("--edges", type=int, default=1000, help="target edge count (default 1000)")
    d_.add_argument("--seed", type=int, default=DEFAULT_SEED, help="global PRNG seed")

    m_ = p.add_argument_group("model")
    m_.add_argument("--backend", default="auto", choices=("auto",) + BACKEND_ORDER,
                    help="ML backend (default: best available)")
    m_.add_argument("--retrain", action="store_true", help="ignore the cached model card")
    m_.add_argument("--no-calibrate", action="store_true", help="disable Platt calibration")
    m_.add_argument("--folds", type=int, default=5, help="CV folds (default 5)")
    m_.add_argument("--threshold", type=float, default=0.5, help="disruption decision threshold")
    m_.add_argument("--audit-dgp", action="store_true",
                    help="validate the model against the synthetic ground-truth DGP")
    m_.add_argument("--estimators", type=int, default=None, help="override n_estimators")
    m_.add_argument("--max-depth", type=int, default=None, help="override tree max_depth")

    c_ = p.add_argument_group("cost function  C(e)=t(e)[1+a*P]+b*H(e)+g*S(e)*C0")
    c_.add_argument("--alpha", type=float, default=2.5, help="risk amplification (default 2.5)")
    c_.add_argument("--beta", type=float, default=0.8, help="terrain penalty (default 0.8)")
    c_.add_argument("--gamma", type=float, default=1.2, help="cargo penalty (default 1.2)")
    c_.add_argument("--cargo", default="standard", choices=tuple(CARGO_PROFILES),
                    help="cargo sensitivity profile")
    c_.add_argument("--cargo-mode", default="additive", choices=("additive", "multiplied"),
                    help="additive = documented form; multiplied = S(e) also scales t(e)")
    c_.add_argument("--cargo-overhead", type=float, default=CARGO_OVERHEAD_S,
                    help="C0 seconds of handling overhead per edge (default 60)")
    c_.add_argument("--vref", type=float, default=V_REF,
                    help="V_REF m/s normalising slope friction to seconds (default 10)")
    c_.add_argument("--rain-derate", type=float, default=0.0,
                    help="fraction of free-flow speed lost at P=1 (default 0 = off)")
    c_.add_argument("--block-threshold", type=float, default=0.90,
                    help="P above which a segment is impassable (default 0.90)")
    c_.add_argument("--no-hard-block", action="store_true",
                    help="keep every segment routable, however risky")

    r_ = p.add_argument_group("routing")
    r_.add_argument("--scenario", default=",".join(SCENARIO_ORDER),
                    help="comma-separated scenarios (light,moderate,heavy,extreme)")
    r_.add_argument("--corridor", action="append", default=None, metavar="A->B",
                    help="route corridor, repeatable (default: 3 NER flagships)")
    r_.add_argument("--algorithm", default="astar", choices=("astar", "dijkstra", "both"),
                    help="shortest-path algorithm")

    o_ = p.add_argument_group("reporting")
    o_.add_argument("--benchmark", action="store_true", help="run the latency/throughput SLA test")
    o_.add_argument("--no-benchmark", action="store_true", help="skip the default SLA check")
    o_.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")
    o_.add_argument("--quiet", action="store_true", help="suppress the console report")
    o_.add_argument("--no-color", action="store_true", help="disable ANSI colours")
    o_.add_argument("--info", action="store_true", help="print environment/capability report")
    o_.add_argument("--selftest", action="store_true", help="run the built-in test suite")
    o_.add_argument("--version", action="store_true", help="print version and exit")
    return p


def _parse_corridors(raw: Optional[List[str]]) -> List[Tuple[str, str]]:
    if not raw:
        return list(DEFAULT_CORRIDORS)
    out = []
    for item in raw:
        for sep in ("->", "-->", ">>", "to"):
            if sep in item:
                a, b = item.split(sep, 1)
                out.append((a.strip(), b.strip()))
                break
        else:
            raise ValueError(f"could not parse corridor '{item}' (expected 'Origin->Destination')")
    return out


def environment_info() -> Dict[str, Any]:
    return {
        "engine": f"SIH26002 Hazard Prediction Engine v{__version__}",
        "team": __team__,
        "problem_id": __problem__,
        "python": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "backend_auto": resolve_backend("auto"),
        "backends_available": available_backends(),
        "optional_packages": {k: v for k, v in _HAS.items()},
        "features": list(FEATURES),
        "feature_units": FEATURE_UNITS,
        "scenarios": {k: v for k, v in SCENARIOS.items()},
        "cargo_profiles": CARGO_PROFILES,
        "risk_bands": [{"band": b, "max_p": m, "colour": c, "action": a}
                       for m, b, c, a in RISK_BANDS],
        "cost_defaults": {"alpha": 2.5, "beta": 0.8, "gamma": 1.2,
                          "C0_seconds": CARGO_OVERHEAD_S, "V_REF_ms": V_REF},
        "paths": {"data_dir": DATA_DIR, "out_dir": OUT_DIR,
                  "graph": DEFAULT_GRAPH_PATH, "hazards": DEFAULT_HAZARD_PATH,
                  "model": DEFAULT_MODEL_PATH},
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    paint = Painter(enabled=(not args.no_color) and (not args.json)
                    and sys.stdout.isatty())

    if args.version:
        print(f"SIH26002 Hazard Prediction Engine v{__version__} ({__team__})")
        print(f"backend(auto)={resolve_backend('auto')} available={available_backends()}")
        return 0
    if args.selftest:
        return run_selftest(paint, quiet=args.quiet)
    if args.info:
        print(json.dumps(environment_info(), indent=2))
        return 0

    scenarios = [s.strip().lower() for s in args.scenario.split(",") if s.strip()]
    for s in scenarios:
        if s not in SCENARIOS:
            print(f"error: unknown scenario '{s}' (choose from {', '.join(SCENARIO_ORDER)})",
                  file=sys.stderr)
            return 2
    corridors = _parse_corridors(args.corridor)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    if not args.model:
        args.model = os.path.join(out_dir, os.path.basename(DEFAULT_MODEL_PATH))

    hp: Dict[str, Any] = {}
    if args.estimators:
        hp["n_estimators"] = args.estimators
    if args.max_depth:
        hp["max_depth"] = args.max_depth

    policy = CostPolicy(alpha=args.alpha, beta=args.beta, gamma=args.gamma,
                        cargo=args.cargo, cargo_mode=args.cargo_mode,
                        cargo_overhead_s=args.cargo_overhead, v_ref=args.vref,
                        rain_derate=args.rain_derate,
                        block_threshold=args.block_threshold,
                        hard_block=not args.no_hard_block)

    t_start = time.perf_counter()
    engine = HazardEngine.bootstrap(
        graph_path=args.graph, hazard_path=args.hazards, model_path=args.model,
        backend=args.backend, seed=args.seed, target_edges=args.edges,
        folds=args.folds, policy=policy, regenerate=args.regenerate_data,
        retrain=args.retrain, calibrate=not args.no_calibrate,
        threshold=args.threshold, verbose=False)
    engine.policy = policy

    g = engine.graph
    states = sorted({e.state for e in g.edges if e.state})
    graph_stats = {
        "n_edges": len(g.edges), "n_nodes": len(g.nodes),
        "total_length_km": round(g.total_length_km(), 2),
        "n_cross_links": int(g.metadata.get("n_cross_links", 0)),
        "n_corridors": len(NER_CORRIDORS), "n_states": len(states),
        "states": states, "bbox": [round(x, 4) for x in g.bbox()],
        "sha1": g.metadata.get("sha1"),
    }

    summaries: List[Dict[str, Any]] = []
    artifacts: List[str] = []
    for s in scenarios:
        summ = run_scenario(engine, s, policy, out_dir, seed=args.seed,
                            corridors=corridors, algorithm=args.algorithm,
                            write=True, paint=paint)
        summaries.append(summ)
        artifacts.extend(summ["artifacts"])
        if args.register and args.register.lower() == s:
            rp = write_risk_csv(g, os.path.join(out_dir, "segment_risk_register.csv"), s)
            artifacts.append(os.path.relpath(rp, HERE))
    if args.register and not any(a.endswith("segment_risk_register.csv") for a in artifacts):
        rp = write_risk_csv(g, os.path.join(out_dir, "segment_risk_register.csv"),
                            args.register.lower())
        artifacts.append(os.path.relpath(rp, HERE))

    benchmark = None
    if args.benchmark or not args.no_benchmark:
        benchmark = run_benchmark(engine, policy, scenario=scenarios[-1])

    audit = None
    if args.audit_dgp:
        sidecar = os.path.splitext(args.hazards)[0] + ".dgp.json"
        overrides = None
        if os.path.exists(sidecar):
            with open(sidecar, "r", encoding="utf-8") as fh:
                overrides = json.load(fh).get("dgp")
        audit = audit_dgp(engine.records, engine.model, overrides, engine.index)

    ctx: Dict[str, Any] = {
        "graph_path": args.graph, "hazard_path": args.hazards,
        "model_path": args.model, "graph_stats": graph_stats,
        "hazard_stats": engine.index.summary() if engine.index else {},
        "backend": engine.model.backend, "backend_stack": available_backends(),
        "packages": _HAS, "seed": args.seed,
        "model": engine.model.metrics, "importance": engine.model.importance,
        "platt": engine.model.platt, "calibrated": not args.no_calibrate,
        "train_seconds": engine.model.train_seconds,
        "policy": policy.to_dict(), "scenarios": summaries,
        "algorithm": args.algorithm, "benchmark": benchmark, "audit": audit,
        "artifacts": sorted(set(artifacts)),
        "total_seconds": round(time.perf_counter() - t_start, 2),
    }

    manifest = {
        "problem_id": __problem__, "engine_version": __version__, "team": __team__,
        "generated_utc": utcnow_iso(), "seed": args.seed,
        "backend": ctx["backend"], "backend_stack": ctx["backend_stack"],
        "optional_packages": {k: v for k, v in _HAS.items()},
        "python": sys.version.split()[0], "platform": platform.platform(),
        "inputs": {"graph": graph_stats, "hazard_log": ctx["hazard_stats"],
                   "graph_path": os.path.relpath(args.graph, HERE),
                   "hazard_path": os.path.relpath(args.hazards, HERE)},
        "model": engine.model.metrics,
        "feature_importance": engine.model.importance,
        "cost_policy": policy.to_dict(),
        "corridors": [f"{a} -> {b}" for a, b in corridors],
        "scenarios": summaries,
        "benchmark": benchmark,
        "dgp_audit": audit,
        "artifacts": ctx["artifacts"],
        "total_seconds": ctx["total_seconds"],
    }
    mp = os.path.join(out_dir, "summary.json")
    save_json(manifest, mp)
    ctx["artifacts"] = sorted(set(ctx["artifacts"]) | {os.path.relpath(mp, HERE)})
    manifest["artifacts"] = ctx["artifacts"]
    save_json(manifest, mp)

    if args.json:
        print(json.dumps(manifest, indent=2, default=str))
    elif not args.quiet:
        print(render_report(ctx, paint))
    return 0


# --------------------------------------------------------------------------- #
#  ENTRY POINT                                                                 #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:                              # pragma: no cover
        print("\ninterrupted.", file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:                                # pragma: no cover
        sys.exit(0)
    except Exception as _exc:                              # pragma: no cover
        import traceback
        print(f"\nerror: {type(_exc).__name__}: {_exc}", file=sys.stderr)
        if os.environ.get("HPE_DEBUG"):
            traceback.print_exc()
        else:
            print("hint: set HPE_DEBUG=1 for a full traceback.", file=sys.stderr)
        sys.exit(1)
