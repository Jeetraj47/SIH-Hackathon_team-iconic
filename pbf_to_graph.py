#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  PBF → GeoJSON Converter                                                   ║
║  Extracts road network from OpenStreetMap PBF files                        ║
║                                                                            ║
║  Produces a GeoJSON FeatureCollection compatible with                      ║
║  hazard_prediction_engine.py                                               ║
╚══════════════════════════════════════════════════════════════════════════════╝

Usage:
    python pbf_to_graph.py data/north-eastern-zone-260911.osm.pbf
    python pbf_to_graph.py data/north-eastern-zone-260911.osm.pbf --max-edges 5000
    python pbf_to_graph.py data/north-eastern-zone-260911.osm.pbf --output data/real_osm_graph.geojson
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

try:
    import pyrosm
except ImportError:
    print("ERROR: pyrosm is required. Install with:  pip install pyrosm")
    sys.exit(1)

try:
    import geopandas as gpd
except ImportError:
    print("ERROR: geopandas is required. Install with:  pip install geopandas")
    sys.exit(1)

import geojson


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

RANDOM_SEED = 42

# OSM highway tag → engine road_type mapping
# NH = National Highway, SH = State Highway, MDR = Major District Road,
# ODR = Other District Road, VR = Village Road
HIGHWAY_TO_ROAD_TYPE: Dict[str, str] = {
    "motorway":       "NH",
    "motorway_link":  "NH",
    "trunk":          "NH",
    "trunk_link":     "NH",
    "primary":        "SH",
    "primary_link":   "SH",
    "secondary":      "MDR",
    "secondary_link": "MDR",
    "tertiary":       "MDR",
    "tertiary_link":  "MDR",
    "unclassified":   "ODR",
    "residential":    "ODR",
    "living_street":  "VR",
    "service":        "VR",
    "track":          "VR",
}

# Default speed (km/h) by highway class — used when OSM maxspeed is missing
DEFAULT_SPEED_KMH: Dict[str, int] = {
    "motorway":       100,
    "motorway_link":  60,
    "trunk":          80,
    "trunk_link":     50,
    "primary":        60,
    "primary_link":   40,
    "secondary":      50,
    "secondary_link": 35,
    "tertiary":       40,
    "tertiary_link":  30,
    "unclassified":   30,
    "residential":    25,
    "living_street":  15,
    "service":        20,
    "track":          15,
}

# Highway types to include (driving-relevant roads only, no footpaths/cycleways)
DRIVING_HIGHWAY_TYPES = set(HIGHWAY_TO_ROAD_TYPE.keys())


# ═══════════════════════════════════════════════════════════════════════════════
# GEOMETRY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Compute geodesic distance in metres between two WGS-84 points."""
    R = 6_371_000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _linestring_length_m(coords: List) -> float:
    """Compute total geodesic length of a LineString from its coordinate list."""
    total = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i][0], coords[i][1]
        lon2, lat2 = coords[i + 1][0], coords[i + 1][1]
        total += _haversine_m(lon1, lat1, lon2, lat2)
    return total


def _parse_maxspeed(val: Any) -> Optional[int]:
    """
    Parse OSM maxspeed tag value to integer km/h.

    Handles formats like: "50", "50 km/h", "30 mph", etc.
    Returns None if unparseable.
    """
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    s = str(val).strip().lower()
    if not s or s in ("none", "signals", "walk", "variable"):
        return None
    # Remove units
    s = s.replace("km/h", "").replace("kmh", "").replace("kph", "").strip()
    if "mph" in s:
        s = s.replace("mph", "").strip()
        try:
            return int(round(float(s) * 1.60934))
        except ValueError:
            return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# CORE: PBF → GeoJSON FeatureCollection
# ═══════════════════════════════════════════════════════════════════════════════


def extract_road_network(
    pbf_path: str | Path,
    max_edges: Optional[int] = None,
) -> dict:
    """
    Extract the drivable road network from an OSM PBF file and return
    a GeoJSON FeatureCollection with the same schema as the engine's
    mock_osm_graph.geojson.

    Parameters
    ----------
    pbf_path : str | Path
        Path to the .osm.pbf file.
    max_edges : int, optional
        Maximum number of road segments to include. If None, include all.

    Returns
    -------
    dict
        A GeoJSON FeatureCollection with LineString features, each having
        properties: edge_id, road_type, name, length_m, speed_kmh,
        slope_deg, hist_freq.
    """
    pbf_path = Path(pbf_path)
    if not pbf_path.exists():
        raise FileNotFoundError(f"PBF file not found: {pbf_path}")

    print(f"\n  [FILE]  Reading PBF file: {pbf_path}  ({pbf_path.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"          This may take a minute for large files ...")

    t0 = time.perf_counter()

    # ------------------------------------------------------------------
    # 1. Parse PBF with pyrosm — extract driving network
    # ------------------------------------------------------------------
    osm = pyrosm.OSM(str(pbf_path))

    print(f"  [WAIT]  Extracting driving network ...")
    # Get the road network as a GeoDataFrame
    # net_type="driving" filters to car-drivable roads
    try:
        gdf = osm.get_network(network_type="driving")
    except Exception as e:
        print(f"  ⚠  get_network('driving') failed: {e}")
        print(f"      Falling back to extracting all roads …")
        gdf = osm.get_network(network_type="all")

    if gdf is None or len(gdf) == 0:
        raise RuntimeError("No road segments found in the PBF file.")

    print(f"  [OK]  Extracted {len(gdf)} road segments in {time.perf_counter() - t0:.1f}s")

    # ------------------------------------------------------------------
    # 2. Filter to driving-relevant highway types
    # ------------------------------------------------------------------
    if "highway" in gdf.columns:
        # highway can be a list in some rows; handle that
        def _normalise_highway(h):
            if isinstance(h, list):
                return h[0] if h else "unclassified"
            return str(h) if h is not None else "unclassified"

        gdf["highway_norm"] = gdf["highway"].apply(_normalise_highway)
        mask = gdf["highway_norm"].isin(DRIVING_HIGHWAY_TYPES)
        gdf = gdf[mask].copy()
        print(f"  [FILTER]  After filtering to driving types: {len(gdf)} segments")

    if len(gdf) == 0:
        raise RuntimeError("No driving-relevant road segments after filtering.")

    # ------------------------------------------------------------------
    # 3. Limit edges if requested
    # ------------------------------------------------------------------
    if max_edges is not None and len(gdf) > max_edges:
        # Sample to get a representative subset
        gdf = gdf.sample(n=max_edges, random_state=RANDOM_SEED).reset_index(drop=True)
        print(f"  [SAMPLE]  Sampled down to {max_edges} edges")

    # ------------------------------------------------------------------
    # 4. Build GeoJSON features with the engine-compatible schema
    # ------------------------------------------------------------------
    rng = np.random.default_rng(RANDOM_SEED)
    features: List[dict] = []

    print(f"  [CONVERT]  Converting to engine-compatible GeoJSON ...")

    for idx, row in gdf.iterrows():
        geom = row.geometry

        # Extract coordinates
        if geom is None:
            continue

        if geom.geom_type == "LineString":
            coords = list(geom.coords)
        elif geom.geom_type == "MultiLineString":
            # Flatten: take the longest sub-linestring
            longest = max(geom.geoms, key=lambda g: g.length)
            coords = list(longest.coords)
        else:
            continue  # skip non-line geometries

        if len(coords) < 2:
            continue

        # Round coordinates for output
        coords_rounded = [[round(c[0], 6), round(c[1], 6)] for c in coords]

        # -- edge_id --
        edge_id = f"E{len(features):06d}"

        # -- highway type --
        highway = row.get("highway_norm", row.get("highway", "unclassified"))
        if isinstance(highway, list):
            highway = highway[0] if highway else "unclassified"
        highway = str(highway) if highway is not None else "unclassified"

        # -- road_type --
        road_type = HIGHWAY_TO_ROAD_TYPE.get(highway, "ODR")

        # -- name --
        name = row.get("name", None)
        if name is None or (isinstance(name, float) and math.isnan(name)):
            name = "Unnamed Road"
        name = str(name)

        # -- length_m (geodesic) --
        length_m = _linestring_length_m(coords)
        if length_m < 1.0:
            continue  # skip degenerate segments

        # -- speed_kmh --
        maxspeed_raw = row.get("maxspeed", None)
        speed_kmh = _parse_maxspeed(maxspeed_raw)
        if speed_kmh is None or speed_kmh <= 0 or speed_kmh > 200:
            speed_kmh = DEFAULT_SPEED_KMH.get(highway, 30)

        # -- slope_deg (synthetic — no DEM data available from OSM) --
        # Use same Gamma(2, 8) distribution as the mock generator
        slope_deg = float(rng.gamma(2.0, 8.0))
        slope_deg = min(slope_deg, 55.0)

        # -- hist_freq (synthetic — no historical hazard data in OSM) --
        # Use same Beta(1.5, 5) distribution as the mock generator
        hist_freq = float(rng.beta(1.5, 5.0))

        feat = geojson.Feature(
            geometry=geojson.LineString(coords_rounded),
            properties={
                "edge_id": edge_id,
                "road_type": road_type,
                "name": name,
                "length_m": round(length_m, 1),
                "speed_kmh": int(speed_kmh),
                "slope_deg": round(slope_deg, 2),
                "hist_freq": round(hist_freq, 4),
            },
        )
        features.append(feat)

    fc = geojson.FeatureCollection(features)

    elapsed = time.perf_counter() - t0
    print(f"  [OK]  Conversion complete: {len(features)} edges in {elapsed:.1f}s")

    # Print road type distribution
    type_counts: Dict[str, int] = {}
    for f in features:
        rt = f["properties"]["road_type"]
        type_counts[rt] = type_counts.get(rt, 0) + 1
    print(f"  [STATS]  Road type distribution: {type_counts}")

    return fc


def save_geojson(fc: dict, output_path: str | Path) -> Path:
    """Save a GeoJSON FeatureCollection to disk."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(fc, f, indent=2)
    size_mb = output_path.stat().st_size / 1024 / 1024
    print(f"  [SAVE]  Saved -> {output_path}  ({size_mb:.1f} MB)")
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pbf_to_graph",
        description="Convert an OSM PBF file to engine-compatible GeoJSON road graph.",
    )
    parser.add_argument(
        "pbf_file",
        type=str,
        help="Path to the .osm.pbf file",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Output GeoJSON path (default: data/osm_road_graph.geojson)",
    )
    parser.add_argument(
        "--max-edges",
        type=int,
        default=None,
        help="Maximum number of road edges to extract (default: all)",
    )

    args = parser.parse_args()

    # Default output path
    if args.output is None:
        pbf = Path(args.pbf_file)
        output = pbf.parent / "osm_road_graph.geojson"
    else:
        output = Path(args.output)

    print()
    print("+====================================================================+")
    print("|  PBF -> GeoJSON Road Graph Converter                                |")
    print("+====================================================================+")

    fc = extract_road_network(args.pbf_file, max_edges=args.max_edges)
    save_geojson(fc, output)

    print()
    print(f"  [DONE]  Graph is ready for hazard_prediction_engine.py")
    print(f"          Run:  python hazard_prediction_engine.py --graph {output}")
    print()


if __name__ == "__main__":
    main()
