#!/usr/bin/env python3
"""
Unit tests for shp_to_graph.py
"""

import tempfile
import unittest
from pathlib import Path

from shp_to_graph import (
    FCLASS_TO_ROAD_TYPE,
    DEFAULT_SPEED_KMH,
    _haversine_m,
    _linestring_length_m,
    extract_road_network_from_shp,
)


class TestShpToGraph(unittest.TestCase):

    def test_haversine(self):
        # 1 degree of latitude is roughly 111 km
        dist = _haversine_m(91.0, 26.0, 91.0, 27.0)
        self.assertAlmostEqual(dist / 1000.0, 111.19, places=0)

    def test_linestring_length(self):
        coords = [(91.0, 26.0), (91.0, 26.5), (91.0, 27.0)]
        dist = _linestring_length_m(coords)
        self.assertAlmostEqual(dist / 1000.0, 111.19, places=0)

    def test_road_mappings(self):
        self.assertEqual(FCLASS_TO_ROAD_TYPE["motorway"], "NH")
        self.assertEqual(FCLASS_TO_ROAD_TYPE["primary"], "SH")
        self.assertEqual(FCLASS_TO_ROAD_TYPE["secondary"], "MDR")
        self.assertEqual(FCLASS_TO_ROAD_TYPE["residential"], "ODR")

    def test_extract_from_shp_dir(self):
        shp_dir = Path("data/north_eastern_zone_shp")
        if not shp_dir.exists():
            self.skipTest("Shapefile directory not found")

        fc = extract_road_network_from_shp(shp_dir, max_edges=25)
        self.assertEqual(fc["type"], "FeatureCollection")
        self.assertGreaterEqual(len(fc["features"]), 1)
        self.assertLessEqual(len(fc["features"]), 25)

        first_props = fc["features"][0]["properties"]
        self.assertIn("edge_id", first_props)
        self.assertIn("road_type", first_props)
        self.assertIn("length_m", first_props)
        self.assertIn("speed_kmh", first_props)
        self.assertIn("slope_deg", first_props)
        self.assertIn("hist_freq", first_props)


if __name__ == "__main__":
    unittest.main()
