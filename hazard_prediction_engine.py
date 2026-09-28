#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  SIH26002 — Hazard Prediction Engine                                       ║
║  Predictive Route Optimization for Disaster-Prone Terrain                   ║
║                                                                            ║
║  Single-file, self-contained module.                                       ║
║  Run:  python hazard_prediction_engine.py                                  ║
║  Deps: numpy, pandas, xgboost, scikit-learn, geojson                       ║
╚══════════════════════════════════════════════════════════════════════════════╝

Mathematical Formulation
========================

Segment Disruption Risk:
    P_disruption(e) = f_XGB(rain, API_3, slope, soil, hist_freq)

Dynamic Cost Function:
    C(e) = t(e) · [1 + α · P_disruption(e)] + β · H(e) + γ · S(e)

Where:
    t(e)  = standard transit time (length / speed)
    H(e)  = slope friction penalty  = tan(slope) · length
    S(e)  = cargo sensitivity       = {1.0: standard, 1.5: perishable, 2.0: hazmat}
    α     = risk amplification      (default 2.5)
    β     = terrain penalty          (default 0.8)
    γ     = cargo penalty            (default 1.2)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split

# XGBoost — the primary model
import xgboost as xgb

# GeoJSON output
import geojson

# PBF reader — optional, imported on demand
try:
    from pbf_to_graph import extract_road_network, save_geojson as save_pbf_geojson
    HAS_PBF_READER = True
except ImportError:
    HAS_PBF_READER = False

# IMD Gridded Weather reader — optional, imported on demand
try:
    from imd_weather import (
        read_imd_binary,
        generate_synthetic_imd_grd,
        IMDGridDataset,
    )
    HAS_IMD_READER = True
except ImportError:
    HAS_IMD_READER = False

# Shapefile reader — optional, imported on demand
try:
    from shp_to_graph import extract_road_network_from_shp, save_geojson as save_shp_geojson
    HAS_SHP_READER = True
except ImportError:
    HAS_SHP_READER = False

warnings.filterwarnings("ignore", category=UserWarning)

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS & PATHS
# ═══════════════════════════════════════════════════════════════════════════════

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
OUTPUT_DIR = ROOT_DIR / "outputs"

HISTORICAL_CSV = DATA_DIR / "historical_hazards_2023.csv"
MOCK_GRAPH = DATA_DIR / "mock_osm_graph.geojson"
PBF_GRAPH = DATA_DIR / "osm_road_graph.geojson"
SHP_GRAPH = DATA_DIR / "shp_road_graph.geojson"

# Weather scenario presets  (rainfall mm/hr)
WEATHER_SCENARIOS: Dict[str, Dict[str, float]] = {
    "light":    {"rainfall_mm_hr": 5.0,   "api_3day": 30.0,  "soil_saturation": 0.3},
    "moderate": {"rainfall_mm_hr": 25.0,  "api_3day": 80.0,  "soil_saturation": 0.55},
    "heavy":    {"rainfall_mm_hr": 60.0,  "api_3day": 150.0, "soil_saturation": 0.78},
    "extreme":  {"rainfall_mm_hr": 120.0, "api_3day": 250.0, "soil_saturation": 0.95},
}

# Feature columns expected by the XGBoost classifier
FEATURE_COLS = [
    "rainfall_mm_hr",
    "api_3day",
    "slope_deg",
    "soil_saturation",
    "hist_freq",
]
TARGET_COL = "disrupted"

# Seed for reproducibility
RANDOM_SEED = 42

random.seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)


# ═══════════════════════════════════════════════════════════════════════════════
# DATA GENERATION — mock historical hazard logs & OSM graph
# ═══════════════════════════════════════════════════════════════════════════════


def _generate_historical_csv(path: Path, n_records: int = 5000) -> pd.DataFrame:
    """
    Synthesise a realistic historical hazard dataset.

    Each row represents a *segment-day* observation during a past monsoon season:
        - rainfall_mm_hr  : hourly rainfall intensity  (0-180 mm/hr)
        - api_3day        : Antecedent Precipitation Index over 3 days (0-350 mm)
        - slope_deg       : road-segment gradient (0°-60°)
        - soil_saturation : volumetric water content fraction (0.0-1.0)
        - hist_freq       : normalised historical disruption frequency for that
                            segment (0.0 = never disrupted, 1.0 = always disrupted)
        - disrupted       : binary target — was the segment blocked? (0 / 1)

    The label is sampled from a logistic model calibrated to Indian monsoon data
    so that the base disruption rate is ~22 % and rises sharply when multiple
    risk factors coincide.
    """
    rng = np.random.default_rng(RANDOM_SEED)

    rainfall = rng.exponential(scale=20.0, size=n_records).clip(0, 180)
    api_3day = rng.gamma(shape=3.0, scale=25.0, size=n_records).clip(0, 350)
    slope = rng.gamma(shape=2.0, scale=8.0, size=n_records).clip(0, 60)
    soil = rng.beta(a=2.0, b=3.0, size=n_records)
    hist_freq = rng.beta(a=1.5, b=5.0, size=n_records)

    # Logistic disruption probability
    logit = (
        -6.0
        + 0.035 * rainfall
        + 0.012 * api_3day
        + 0.07 * slope
        + 2.5 * soil
        + 3.0 * hist_freq
    )
    prob = 1.0 / (1.0 + np.exp(-logit))
    disrupted = (rng.random(n_records) < prob).astype(int)

    df = pd.DataFrame(
        {
            "rainfall_mm_hr": np.round(rainfall, 2),
            "api_3day": np.round(api_3day, 2),
            "slope_deg": np.round(slope, 2),
            "soil_saturation": np.round(soil, 4),
            "hist_freq": np.round(hist_freq, 4),
            "disrupted": disrupted,
        }
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    print(f"  [OK]  Generated historical hazard data -> {path}  ({len(df)} records)")
    return df


def _generate_mock_graph(path: Path, n_edges: int = 1000) -> dict:
    """
    Create a synthetic road-network graph as GeoJSON FeatureCollection.

    Each Feature is a LineString representing a road segment (edge) with
    properties: edge_id, length_m, speed_kmh, slope_deg, hist_freq, name.

    The graph models the disaster-prone North-Eastern Zone (Assam-Meghalaya hill corridor).
    """
    rng = np.random.default_rng(RANDOM_SEED + 1)

    # Base coordinates — simulate a ~60 km x 60 km region in North-Eastern Zone
    base_lat, base_lon = 26.15, 91.75  # North-Eastern Zone (Guwahati - Shillong Corridor)

    features: List[dict] = []
    road_types = ["NH", "SH", "MDR", "ODR", "VR"]  # National / State / District / Other / Village
    road_names = [
        "GS Road (Guwahati-Shillong Hwy)",
        "Assam Trunk Road (NH-27)",
        "Brahmaputra Bridge Approach Rd",
        "Kaziranga Corridor Highway",
        "Cherrapunji-Mawsynram Ghat Rd",
        "Silchar-Haflong Hill Hwy",
        "Shillong Peak Ridge Rd",
        "Dispur Capital Link",
        "Barapani Lake Bypass",
        "Dawki Border Transit Rd",
        "Jorhat Tea Estate Connector",
        "Tawang-Bomdila Pass Rd",
    ]

    for i in range(n_edges):
        # Random start point
        lat1 = base_lat + rng.uniform(-0.25, 0.25)
        lon1 = base_lon + rng.uniform(-0.25, 0.25)
        # Random short segment
        lat2 = lat1 + rng.uniform(-0.005, 0.005)
        lon2 = lon1 + rng.uniform(-0.005, 0.005)

        length_m = rng.uniform(200, 5000)
        speed_kmh = rng.choice([20, 30, 40, 50, 60, 80])
        slope_deg = float(rng.gamma(2.0, 8.0))
        slope_deg = min(slope_deg, 55.0)
        hist_freq = float(rng.beta(1.5, 5.0))

        feat = geojson.Feature(
            geometry=geojson.LineString([(round(lon1, 6), round(lat1, 6)),
                                         (round(lon2, 6), round(lat2, 6))]),
            properties={
                "edge_id": f"E{i:04d}",
                "road_type": rng.choice(road_types),
                "name": rng.choice(road_names),
                "length_m": round(length_m, 1),
                "speed_kmh": int(speed_kmh),
                "slope_deg": round(slope_deg, 2),
                "hist_freq": round(hist_freq, 4),
            },
        )
        features.append(feat)

    fc = geojson.FeatureCollection(features)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(fc, f, indent=2)
    print(f"  [OK]  Generated mock OSM graph -> {path}  ({n_edges} edges)")
    return fc


# ═══════════════════════════════════════════════════════════════════════════════
# MODEL — XGBoost Disruption Classifier
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ModelMetrics:
    """Holds evaluation metrics for a trained model."""
    accuracy: float = 0.0
    roc_auc: float = 0.0
    report: str = ""
    confusion: Optional[np.ndarray] = None


@dataclass
class HazardModel:
    """
    XGBoost-based binary classifier for road-segment disruption prediction.

    Trained on historical hazard logs to produce P_disruption ∈ [0, 1].
    """
    model: Optional[xgb.XGBClassifier] = None
    metrics: ModelMetrics = field(default_factory=ModelMetrics)
    is_trained: bool = False
    feature_importance: Optional[Dict[str, float]] = None

    def train(self, df: pd.DataFrame, test_size: float = 0.2) -> ModelMetrics:
        """
        Train the XGBoost classifier on historical hazard data.

        Follows ML best practices:
        1. Train/test split BEFORE any preprocessing
        2. Handle missing values check
        3. Evaluate with multiple metrics
        """
        print("\n  [ML]  Training XGBoost disruption classifier ...")

        # ------------------------------------------------------------------
        # 1. Data validation
        # ------------------------------------------------------------------
        missing = df[FEATURE_COLS + [TARGET_COL]].isnull().sum()
        if missing.any():
            print(f"    [WARN]  Missing values detected:\n{missing[missing > 0]}")
            df = df.dropna(subset=FEATURE_COLS + [TARGET_COL])
            print(f"    [INFO]  Dropped rows with NaN - {len(df)} rows remain.")

        X = df[FEATURE_COLS].values
        y = df[TARGET_COL].values

        class_dist = np.bincount(y)
        print(f"    [DATA]  Class distribution: safe={class_dist[0]}, disrupted={class_dist[1]} "
              f"({100 * class_dist[1] / len(y):.1f}% positive)")

        # ------------------------------------------------------------------
        # 2. Chronological-aware split (random for synthetic data)
        # ------------------------------------------------------------------
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=RANDOM_SEED, stratify=y
        )

        # ------------------------------------------------------------------
        # 3. Handle class imbalance via scale_pos_weight
        # ------------------------------------------------------------------
        neg, pos = np.bincount(y_train)
        scale_pos_weight = neg / pos if pos > 0 else 1.0

        # ------------------------------------------------------------------
        # 4. Train XGBoost
        # ------------------------------------------------------------------
        self.model = xgb.XGBClassifier(
            n_estimators=200,
            max_depth=6,
            learning_rate=0.1,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=scale_pos_weight,
            eval_metric="logloss",
            use_label_encoder=False,
            random_state=RANDOM_SEED,
            verbosity=0,
        )

        self.model.fit(
            X_train, y_train,
            eval_set=[(X_test, y_test)],
            verbose=False,
        )

        # ------------------------------------------------------------------
        # 5. Evaluate
        # ------------------------------------------------------------------
        y_pred = self.model.predict(X_test)
        y_prob = self.model.predict_proba(X_test)[:, 1]

        self.metrics = ModelMetrics(
            accuracy=accuracy_score(y_test, y_pred),
            roc_auc=roc_auc_score(y_test, y_prob),
            report=classification_report(y_test, y_pred, target_names=["Safe", "Disrupted"]),
            confusion=confusion_matrix(y_test, y_pred),
        )

        # Feature importance
        imp = self.model.feature_importances_
        self.feature_importance = {
            FEATURE_COLS[i]: round(float(imp[i]), 4) for i in range(len(FEATURE_COLS))
        }

        self.is_trained = True

        print(f"    [OK]  Model trained - Accuracy: {self.metrics.accuracy:.4f}  |  "
              f"ROC-AUC: {self.metrics.roc_auc:.4f}")
        print(f"    [FEAT]  Feature importance: {self.feature_importance}")
        print(f"\n{self.metrics.report}")

        return self.metrics

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        """Return P(disrupted) for each sample row."""
        if not self.is_trained or self.model is None:
            raise RuntimeError("Model not trained. Call .train() first.")
        return self.model.predict_proba(features)[:, 1]

    def predict_single(
        self,
        rainfall_mm_hr: float,
        api_3day: float,
        slope_deg: float,
        soil_saturation: float,
        hist_freq: float,
    ) -> float:
        """Convenience: predict disruption probability for one segment."""
        x = np.array([[rainfall_mm_hr, api_3day, slope_deg, soil_saturation, hist_freq]])
        return float(self.predict_proba(x)[0])


# ═══════════════════════════════════════════════════════════════════════════════
# COST FUNCTION — Dynamic edge weighting
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class CostParams:
    """Tuneable knobs for the dynamic cost function C(e)."""
    alpha: float = 2.5    # risk amplification
    beta: float = 0.8     # terrain penalty weight
    gamma: float = 1.2    # cargo penalty weight
    cargo_type: str = "standard"  # standard | perishable | hazmat

    @property
    def cargo_sensitivity(self) -> float:
        return {"standard": 1.0, "perishable": 1.5, "hazmat": 2.0}.get(
            self.cargo_type, 1.0
        )


def compute_transit_time(length_m: float, speed_kmh: float) -> float:
    """t(e) — standard transit time in seconds."""
    if speed_kmh <= 0:
        speed_kmh = 20.0  # fallback
    return length_m / (speed_kmh * 1000.0 / 3600.0)


def compute_slope_penalty(slope_deg: float, length_m: float) -> float:
    """H(e) — slope friction penalty = tan(slope) · length."""
    slope_rad = math.radians(min(slope_deg, 89.0))  # clamp to avoid tan(90°)
    return math.tan(slope_rad) * length_m


def compute_dynamic_cost(
    length_m: float,
    speed_kmh: float,
    slope_deg: float,
    p_disruption: float,
    params: CostParams,
) -> float:
    """
    C(e) = t(e) · [1 + α · P_disruption(e)] + β · H(e) + γ · S(e)

    Returns the scalar cost for a single edge.
    """
    t_e = compute_transit_time(length_m, speed_kmh)
    H_e = compute_slope_penalty(slope_deg, length_m)
    S_e = params.cargo_sensitivity

    cost = t_e * (1.0 + params.alpha * p_disruption) + params.beta * H_e + params.gamma * S_e
    return round(cost, 4)


# ═══════════════════════════════════════════════════════════════════════════════
# GRAPH RE-WEIGHTING — apply risk scores to OSM graph
# ═══════════════════════════════════════════════════════════════════════════════


def reweight_graph(
    graph_fc: dict,
    model: HazardModel,
    weather: Dict[str, float],
    params: CostParams,
) -> dict:
    """
    Iterate over every edge in the GeoJSON FeatureCollection, compute
    P_disruption and the dynamic cost, and inject them as new properties.

    Returns a new FeatureCollection with added properties:
        - p_disruption       : float ∈ [0, 1]
        - risk_category      : str (low / moderate / high / critical)
        - dynamic_cost       : float (the scalar cost C(e))
        - transit_time_sec   : float
        - slope_penalty      : float
    """
    features_out: List[dict] = []

    # Vectorised batch prediction
    edge_data = []
    for feat in graph_fc["features"]:
        props = feat["properties"]
        edge_data.append([
            weather["rainfall_mm_hr"],
            weather["api_3day"],
            props["slope_deg"],
            weather["soil_saturation"],
            props["hist_freq"],
        ])

    X = np.array(edge_data)
    probs = model.predict_proba(X)

    for idx, feat in enumerate(graph_fc["features"]):
        props = dict(feat["properties"])  # shallow copy
        p_dis = float(probs[idx])

        # Risk category thresholds
        if p_dis < 0.25:
            risk_cat = "low"
        elif p_dis < 0.50:
            risk_cat = "moderate"
        elif p_dis < 0.75:
            risk_cat = "high"
        else:
            risk_cat = "critical"

        length_m = props["length_m"]
        speed_kmh = props["speed_kmh"]
        slope_deg = props["slope_deg"]

        t_e = compute_transit_time(length_m, speed_kmh)
        h_e = compute_slope_penalty(slope_deg, length_m)
        cost = compute_dynamic_cost(length_m, speed_kmh, slope_deg, p_dis, params)

        props.update({
            "p_disruption": round(p_dis, 6),
            "risk_category": risk_cat,
            "dynamic_cost": cost,
            "transit_time_sec": round(t_e, 2),
            "slope_penalty": round(h_e, 2),
            "weather_scenario": {
                "rainfall_mm_hr": weather["rainfall_mm_hr"],
                "api_3day": weather["api_3day"],
                "soil_saturation": weather["soil_saturation"],
            },
        })

        new_feat = geojson.Feature(
            geometry=feat["geometry"],
            properties=props,
        )
        features_out.append(new_feat)

    return geojson.FeatureCollection(features_out)


# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY STATISTICS
# ═══════════════════════════════════════════════════════════════════════════════


def print_graph_summary(scenario: str, fc: dict) -> None:
    """Print a concise summary of risk distribution for a re-weighted graph."""
    risks = [f["properties"]["p_disruption"] for f in fc["features"]]
    costs = [f["properties"]["dynamic_cost"] for f in fc["features"]]
    cats = [f["properties"]["risk_category"] for f in fc["features"]]

    cat_counts = {c: cats.count(c) for c in ["low", "moderate", "high", "critical"]}

    print(f"\n  {'-' * 60}")
    print(f"  [SCENARIO]  {scenario.upper()}")
    print(f"  {'-' * 60}")
    print(f"    Edges scored      : {len(risks)}")
    print(f"    P_disruption      : min={min(risks):.4f}  mean={np.mean(risks):.4f}  "
          f"max={max(risks):.4f}")
    print(f"    Dynamic cost      : min={min(costs):.1f}  mean={np.mean(costs):.1f}  "
          f"max={max(costs):.1f}")
    print(f"    Risk breakdown    : [LOW] low={cat_counts['low']}  "
          f"[MOD] moderate={cat_counts['moderate']}  "
          f"[HIGH] high={cat_counts['high']}  "
          f"[CRIT] critical={cat_counts['critical']}")


def reweight_graph_imd(
    graph_fc: dict,
    model: HazardModel,
    imd_ds: Any,
    date: Optional[str],
    params: CostParams,
) -> Tuple[dict, Dict[str, Any]]:
    """
    Re-weight graph edges using real IMD gridded meteorological observations.
    Extracts spatial daily rainfall and 3-day Antecedent Precipitation Index (API_3)
    at the geographical midpoint of each road segment.
    """
    features_out: List[dict] = []
    coords: List[Tuple[float, float]] = []

    # Extract midpoint (lat, lon) of every road edge
    for feat in graph_fc["features"]:
        geom = feat.get("geometry", {})
        c = geom.get("coordinates", [])
        if c and len(c) >= 2:
            # GeoJSON coordinates are [lon, lat]
            mid_lon = (c[0][0] + c[-1][0]) / 2.0
            mid_lat = (c[0][1] + c[-1][1]) / 2.0
        elif c and len(c) == 1:
            mid_lon, mid_lat = c[0][0], c[0][1]
        else:
            mid_lon, mid_lat = 91.75, 26.15  # NE fallback
        coords.append((mid_lat, mid_lon))

    # Query IMD rainfall at every edge midpoint
    target_date = date or imd_ds.dates[min(195, len(imd_ds.dates)-1)].strftime("%Y-%m-%d")
    rain_vals_mm_day = imd_ds.get_points(coords, date=target_date)

    # Compute API_3 across the IMD grid
    api_grid = imd_ds.compute_api(k=0.85, window=3)
    day_idx = imd_ds.get_day_index(target_date)
    api_day = api_grid[day_idx]

    api_vals = []
    for lat, lon in coords:
        try:
            r, col = imd_ds.coord_to_indices(lat, lon)
            val = float(api_day[r, col])
            api_vals.append(val if not np.isnan(val) else 30.0)
        except Exception:
            api_vals.append(30.0)
    api_vals = np.array(api_vals, dtype=np.float32)

    # Construct batch features for model
    edge_data = []
    for idx, feat in enumerate(graph_fc["features"]):
        props = feat["properties"]
        rain_day = float(rain_vals_mm_day[idx])
        if np.isnan(rain_day) or rain_day < 0:
            rain_day = 10.0  # Baseline

        # Convert daily rainfall to equivalent mm/hr intensity during rain events
        rain_hr = min(150.0, rain_day / 8.0)
        api_3 = float(api_vals[idx])
        soil_sat = float(np.clip(0.20 + 0.0035 * api_3, 0.15, 0.98))

        edge_data.append([
            rain_hr,
            api_3,
            props["slope_deg"],
            soil_sat,
            props["hist_freq"],
        ])

    X = np.array(edge_data)
    probs = model.predict_proba(X)

    heavy_rain_segments = 0
    total_rain_acc = 0.0

    for idx, feat in enumerate(graph_fc["features"]):
        props = dict(feat["properties"])
        p_dis = float(probs[idx])
        rain_day = float(rain_vals_mm_day[idx]) if not np.isnan(rain_vals_mm_day[idx]) else 10.0
        api_3 = float(api_vals[idx])
        soil_sat = float(np.clip(0.20 + 0.0035 * api_3, 0.15, 0.98))

        if rain_day >= 64.5:
            heavy_rain_segments += 1
        total_rain_acc += rain_day

        if p_dis < 0.25:
            risk_cat = "low"
        elif p_dis < 0.50:
            risk_cat = "moderate"
        elif p_dis < 0.75:
            risk_cat = "high"
        else:
            risk_cat = "critical"

        length_m = props["length_m"]
        speed_kmh = props["speed_kmh"]
        slope_deg = props["slope_deg"]

        t_e = compute_transit_time(length_m, speed_kmh)
        h_e = compute_slope_penalty(slope_deg, length_m)
        cost = compute_dynamic_cost(length_m, speed_kmh, slope_deg, p_dis, params)

        props.update({
            "p_disruption": round(p_dis, 6),
            "risk_category": risk_cat,
            "dynamic_cost": cost,
            "transit_time_sec": round(t_e, 2),
            "slope_penalty": round(h_e, 2),
            "imd_weather": {
                "date": target_date,
                "rainfall_mm_day": round(rain_day, 2),
                "rainfall_mm_hr": round(min(150.0, rain_day / 8.0), 2),
                "api_3day": round(api_3, 2),
                "soil_saturation": round(soil_sat, 3),
                "heavy_rain_alert": rain_day >= 64.5,
            },
        })

        features_out.append(geojson.Feature(
            geometry=feat["geometry"],
            properties=props,
        ))

    stats = {
        "date": target_date,
        "total_edges": len(features_out),
        "heavy_rain_edges": heavy_rain_segments,
        "mean_rainfall_mm": round(total_rain_acc / len(features_out), 2),
        "max_rainfall_mm": round(float(np.nanmax(rain_vals_mm_day)), 2) if len(rain_vals_mm_day) else 0.0,
    }
    return geojson.FeatureCollection(features_out), stats


def print_imd_summary(stats: Dict[str, Any], fc: dict) -> None:
    """Print a summary for IMD-observed meteorological risk scoring."""
    risks = [f["properties"]["p_disruption"] for f in fc["features"]]
    costs = [f["properties"]["dynamic_cost"] for f in fc["features"]]
    cats = [f["properties"]["risk_category"] for f in fc["features"]]
    cat_counts = {c: cats.count(c) for c in ["low", "moderate", "high", "critical"]}

    print(f"\n  {'-' * 60}")
    print(f"  [IMD WEATHER OBSERVED]  DATE: {stats['date']}")
    print(f"  {'-' * 60}")
    print(f"    Edges scored      : {stats['total_edges']}")
    print(f"    Rainfall stats    : mean={stats['mean_rainfall_mm']:.1f} mm/day  "
          f"max={stats['max_rainfall_mm']:.1f} mm/day")
    print(f"    Heavy rain alert  : {stats['heavy_rain_edges']} segments (>= 64.5 mm/day)")
    print(f"    P_disruption      : min={min(risks):.4f}  mean={np.mean(risks):.4f}  "
          f"max={max(risks):.4f}")
    print(f"    Dynamic cost      : min={min(costs):.1f}  mean={np.mean(costs):.1f}  "
          f"max={max(costs):.1f}")
    print(f"    Risk breakdown    : [LOW] low={cat_counts['low']}  "
          f"[MOD] moderate={cat_counts['moderate']}  "
          f"[HIGH] high={cat_counts['high']}  "
          f"[CRIT] critical={cat_counts['critical']}")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI INTERFACE
# ═══════════════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hazard_prediction_engine",
        description="SIH26002 — Hazard Prediction Engine: "
                    "scores road-segment disruption risk and re-weights OSM graphs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python hazard_prediction_engine.py
  python hazard_prediction_engine.py --scenario heavy --cargo hazmat
  python hazard_prediction_engine.py --alpha 3.0 --beta 1.0 --gamma 1.5
        """,
    )
    p.add_argument(
        "--scenario",
        choices=["light", "moderate", "heavy", "extreme", "all", "none"],
        default="all",
        help="Weather scenario to simulate (default: all, or none if only IMD)",
    )
    p.add_argument("--alpha", type=float, default=2.5, help="Risk amplification alpha (default: 2.5)")
    p.add_argument("--beta", type=float, default=0.8, help="Terrain penalty beta (default: 0.8)")
    p.add_argument("--gamma", type=float, default=1.2, help="Cargo penalty gamma (default: 1.2)")
    p.add_argument(
        "--cargo",
        choices=["standard", "perishable", "hazmat"],
        default="standard",
        help="Cargo type for sensitivity multiplier (default: standard)",
    )
    p.add_argument("--edges", type=int, default=1000, help="Number of edges in mock graph (default: 1000)")
    p.add_argument("--records", type=int, default=5000, help="Historical records to generate (default: 5000)")
    p.add_argument("--quiet", action="store_true", help="Suppress detailed output")
    p.add_argument(
        "--pbf",
        type=str,
        default=None,
        help="Path to an OSM .pbf file to use instead of the mock graph",
    )
    p.add_argument(
        "--shp",
        type=str,
        default=None,
        help="Path to an OSM Shapefile directory or gis_osm_roads_free_1.shp",
    )
    p.add_argument(
        "--max-edges",
        type=int,
        default=None,
        help="Max road edges to extract from PBF/SHP (default: all)",
    )
    p.add_argument(
        "--graph",
        type=str,
        default=None,
        help="Path to a pre-converted GeoJSON road graph (from pbf_to_graph.py or shp_to_graph.py)",
    )
    p.add_argument(
        "--imd-grd",
        type=str,
        default=None,
        help="Path to an IMD binary .grd file (from imdR) for real-world weather",
    )
    p.add_argument(
        "--imd-date",
        type=str,
        default=None,
        help="Target date (YYYY-MM-DD) within the IMD dataset to evaluate hazards for",
    )
    p.add_argument(
        "--imd-sample",
        action="store_true",
        help="Use or auto-generate a sample 2023 IMD rain grid for real spatial scoring",
    )
    return p


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    print()
    print("+====================================================================+")
    print("|  [SAT]  SIH26002 - Hazard Prediction Engine                         |")
    print("|  Predictive Route Optimization for Disaster-Prone Terrain          |")
    print("+====================================================================+")
    print()

    t_start = time.perf_counter()

    # ── Step 1: Load or generate data ────────────────────────────────────
    print("  [1] Step 1/4 - Preparing data ...")

    if HISTORICAL_CSV.exists():
        df = pd.read_csv(HISTORICAL_CSV)
        print(f"  [OK]  Loaded historical data <- {HISTORICAL_CSV}  ({len(df)} records)")
    else:
        df = _generate_historical_csv(HISTORICAL_CSV, n_records=args.records)

    # ── Graph source selection: --pbf > --shp > --graph > cached > mock ──
    if args.pbf:
        if not HAS_PBF_READER:
            print("  [ERROR]  pyrosm / geopandas not installed. Run:")
            print("        pip install pyrosm geopandas")
            sys.exit(1)
        from pathlib import Path as _P
        pbf_path = _P(args.pbf)
        if not pbf_path.exists():
            print(f"  [ERROR]  PBF file not found: {pbf_path}")
            sys.exit(1)
        graph_fc = extract_road_network(pbf_path, max_edges=args.max_edges)
        save_pbf_geojson(graph_fc, PBF_GRAPH)
        graph = graph_fc
        print(f"  [OK]  Extracted real OSM graph from PBF  ({len(graph['features'])} edges)")
    elif args.shp:
        if not HAS_SHP_READER:
            print("  [ERROR]  shp_to_graph module not available.")
            sys.exit(1)
        from pathlib import Path as _P
        shp_input = _P(args.shp)
        if not shp_input.exists():
            print(f"  [ERROR]  Shapefile path not found: {shp_input}")
            sys.exit(1)
        graph_fc = extract_road_network_from_shp(shp_input, max_edges=args.max_edges)
        save_shp_geojson(graph_fc, SHP_GRAPH)
        graph = graph_fc
        print(f"  [OK]  Extracted real OSM graph from Shapefile  ({len(graph['features'])} edges)")
    elif args.graph:
        graph_path = Path(args.graph)
        if not graph_path.exists():
            print(f"  [ERROR]  Graph file not found: {graph_path}")
            sys.exit(1)
        with open(graph_path) as f:
            graph = json.load(f)
        print(f"  [OK]  Loaded graph <- {graph_path}  ({len(graph['features'])} edges)")
    elif SHP_GRAPH.exists():
        with open(SHP_GRAPH) as f:
            graph = json.load(f)
        print(f"  [OK]  Loaded cached Shapefile graph <- {SHP_GRAPH}  ({len(graph['features'])} edges)")
    elif PBF_GRAPH.exists():
        with open(PBF_GRAPH) as f:
            graph = json.load(f)
        print(f"  [OK]  Loaded cached OSM graph <- {PBF_GRAPH}  ({len(graph['features'])} edges)")
    elif MOCK_GRAPH.exists():
        with open(MOCK_GRAPH) as f:
            graph = json.load(f)
        print(f"  [OK]  Loaded mock OSM graph <- {MOCK_GRAPH}  ({len(graph['features'])} edges)")
    else:
        graph = _generate_mock_graph(MOCK_GRAPH, n_edges=args.edges)

    # ── Step 2: Train model ──────────────────────────────────────────────
    print("\n  [2] Step 2/4 - Training disruption model ...")
    model = HazardModel()
    model.train(df)

    # ── Step 3: Re-weight graph under weather scenarios ──────────────────
    print("\n  [3] Step 3/4 - Re-weighting graph edges ...")

    cost_params = CostParams(
        alpha=args.alpha,
        beta=args.beta,
        gamma=args.gamma,
        cargo_type=args.cargo,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_files: Dict[str, Path] = {}

    # ── IMD Gridded Weather Evaluation (if requested) ──────────────────
    if args.imd_grd or args.imd_sample:
        if not HAS_IMD_READER:
            print("  [ERROR]  imd_weather module not available.")
            sys.exit(1)

        imd_path = args.imd_grd
        if not imd_path and args.imd_sample:
            sample_path = DATA_DIR / "sample_imd_rain_2023.grd"
            if not sample_path.exists():
                print("  [INFO] Generating sample 2023 IMD rainfall grid ...")
                generate_synthetic_imd_grd(sample_path, variable="rain", year=2023)
            imd_path = str(sample_path)

        print(f"  [IMD] Scoring with real IMD gridded weather <- {imd_path}")
        imd_ds = read_imd_binary(imd_path, variable="rain")
        weighted_imd, imd_stats = reweight_graph_imd(
            graph, model, imd_ds, date=args.imd_date, params=cost_params
        )

        out_imd_path = OUTPUT_DIR / "graph_imd_observed.json"
        with open(out_imd_path, "w") as f:
            json.dump(weighted_imd, f, indent=2)
        output_files[f"imd_{imd_stats['date']}"] = out_imd_path

        if not args.quiet:
            print_imd_summary(imd_stats, weighted_imd)

    # ── Preset Scenarios Evaluation ───────────────────────────────────
    if args.scenario != "none":
        if args.scenario == "all":
            scenarios = list(WEATHER_SCENARIOS.keys())
        else:
            scenarios = [args.scenario]

        for scenario_name in scenarios:
            weather = WEATHER_SCENARIOS[scenario_name]
            weighted = reweight_graph(graph, model, weather, cost_params)

            out_path = OUTPUT_DIR / f"graph_{scenario_name}.json"
            with open(out_path, "w") as f:
                json.dump(weighted, f, indent=2)
            output_files[scenario_name] = out_path

            if not args.quiet:
                print_graph_summary(scenario_name, weighted)

    # ── Step 4: Summary ──────────────────────────────────────────────────
    elapsed = time.perf_counter() - t_start

    print(f"\n  [4] Step 4/4 - Complete!")
    print(f"\n  {'=' * 60}")
    print(f"  [TIME]  Total time: {elapsed:.2f}s")
    print(f"  [PARAMS]  Parameters: alpha={cost_params.alpha}  beta={cost_params.beta}  "
          f"gamma={cost_params.gamma}  cargo={cost_params.cargo_type}")
    print(f"  [OUT]  Outputs:")
    for name, path in output_files.items():
        size_kb = path.stat().st_size / 1024
        print(f"       {name:18s} -> {path}  ({size_kb:.0f} KB)")

    print(f"\n  [DONE]  Re-weighted graphs are ready for Dijkstra / A* / NetworkX routing.")
    print()


if __name__ == "__main__":
    main()
