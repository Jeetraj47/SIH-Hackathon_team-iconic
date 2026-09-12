#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIH26002 data-ingestion suite — real source formats -> engine training table.

    python -m unittest discover -s tests -v
    python tests/test_ingestion.py

Needs no third-party packages and no network: every "download" is a file written
into a temp dir in the exact container the real portal serves (big-endian
`.hgt`, ESRI `.asc`, OSM-tagged GeoJSON, GSI/IMD/ERA5-Land CSV, integer-encoded
ISRIC JSON).  Anything that would need rasterio is skipped, not failed.

What is covered: DEM readers and tile georeferencing, void handling, Horn slope,
column sniffing across portal dialects, date parsing, the rainfall index and its
API decay, grid nearest-neighbour lookup, the documented district fallback,
ISRIC unit decoding, soil texture -> engine vocabulary, porosity and degree of
saturation, the bucket model's mass balance, OSM way splitting AT junctions (the
T-junction trap that silently fragments a network), MultiLineString support, tag
mapping, the event-to-road spatial join including mid-segment hits, the
case-control fusion design, expanding-window label leakage, city-name snapping,
provenance accounting, end-to-end CLI determinism, and the zero-dependency
guarantee.

Also covered: state/bbox resolution for a live fetch (both bbox orderings in
circulation), Overpass and OSMnx replies converted to the engine's FeatureCollection
without geopandas or shapely, window-cropping a fetch to a budget versus
row-sampling it (which fragments the network), the offline cache contract,
pre-computed slope rasters in degrees or percent, and the logging switch.
"""
from __future__ import annotations

import array
import contextlib
import datetime as dt
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import data_ingestion as D          # noqa: E402  (sys.path set up above)
import hazard_prediction_engine as H  # noqa: E402

INGEST = os.path.join(ROOT, "data_ingestion.py")


def jload(path):
    """json.load without leaking a file handle (the suite runs -W error)."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def jread(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()
SEED = 26002


def write_hgt(path: str, side: int, values) -> None:
    """Write a big-endian int16 SRTM tile, row-major from the NORTH-west corner."""
    buf = array.array("h", [int(v) for v in values])
    assert len(buf) == side * side
    if sys.byteorder == "little":
        buf.byteswap()
    with open(path, "wb") as fh:
        fh.write(buf.tobytes())


def write_asc(path: str, ncols: int, nrows: int, xll: float, yll: float,
              cell: float, rows, nodata: float = -9999.0) -> None:
    with open(path, "w") as fh:
        fh.write(f"ncols        {ncols}\n")
        fh.write(f"nrows        {nrows}\n")
        fh.write(f"xllcorner    {xll}\n")
        fh.write(f"yllcorner    {yll}\n")
        fh.write(f"cellsize     {cell}\n")
        fh.write(f"NODATA_value  {nodata}\n")
        for r in rows:                       # rows are north -> south
            fh.write(" ".join(str(v) for v in r) + "\n")


def write_csv(path: str, header, rows) -> None:
    import csv
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def write_geojson(path: str, features) -> None:
    with open(path, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh)


def line(coords, **props):
    return {"type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": props}


# --------------------------------------------------------------------------- #
#  DEM READERS
# --------------------------------------------------------------------------- #
class TestDemReaders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ing-dem-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hgt_tile_corner_is_south_west(self):
        self.assertEqual(D._hgt_tile_corner("N26E091"), (26.0, 91.0))
        self.assertEqual(D._hgt_tile_corner("N27E092"), (27.0, 92.0))
        self.assertEqual(D._hgt_tile_corner("S05W071"), (-5.0, -71.0))
        self.assertEqual(D._hgt_tile_corner("n26e091"), (26.0, 91.0))
        self.assertIsNone(D._hgt_tile_corner("dem_tile"))
        self.assertIsNone(D._hgt_tile_corner(""))

    def test_hgt_is_big_endian_and_georeferenced_from_the_file_name(self):
        side = 5
        # 1234 is deliberately asymmetric: little-endian misreading gives 53764
        vals = [1234] * (side * side)
        vals[0] = 2000                        # north-west corner cell
        p = os.path.join(self.tmp, "N26E091.hgt")
        write_hgt(p, side, vals)
        g = D.load_hgt(p)
        self.assertEqual((g.west, g.south, g.east, g.north),
                         (91.0, 26.0, 92.0, 27.0))
        self.assertEqual((g.ncols, g.nrows), (side, side))
        # north-west corner -> the 2000 m cell
        self.assertAlmostEqual(g.elevation(27.0, 91.0), 2000.0, delta=1.0)
        # south-east corner -> the uniform 1234 m
        self.assertAlmostEqual(g.elevation(26.0, 92.0), 1234.0, delta=1.0)
        self.assertAlmostEqual(g.elevation(26.5, 91.5), 1234.0, delta=60.0)

    def test_hgt_rejects_a_name_that_encodes_no_corner(self):
        p = os.path.join(self.tmp, "download.hgt")
        write_hgt(p, 3, [100] * 9)
        with self.assertRaises(ValueError) as cm:
            D.load_hgt(p)
        self.assertIn("south-west corner", str(cm.exception))

    def test_hgt_rejects_a_non_square_sample_count(self):
        p = os.path.join(self.tmp, "N26E091.hgt")
        with open(p, "wb") as fh:
            fh.write(b"\x00" * 14)            # 7 samples
        with self.assertRaises(ValueError) as cm:
            D.load_hgt(p)
        self.assertIn("not a square grid", str(cm.exception))

    def test_asc_row_order_is_north_first(self):
        # the single most common .asc bug: writing rows south -> north
        rows = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        p = os.path.join(self.tmp, "tiny.asc")
        write_asc(p, 3, 3, xll=91.0, yll=26.0, cell=0.5, rows=rows)
        g = D.load_asc(p)
        self.assertEqual((g.west, g.south, g.east, g.north),
                         (91.0, 26.0, 92.0, 27.0))
        self.assertAlmostEqual(g.elevation(27.0, 91.0), 1.0, delta=1e-6)
        self.assertAlmostEqual(g.elevation(27.0, 92.0), 3.0, delta=1e-6)
        self.assertAlmostEqual(g.elevation(26.0, 91.0), 7.0, delta=1e-6)
        self.assertAlmostEqual(g.elevation(26.0, 92.0), 9.0, delta=1e-6)
        self.assertAlmostEqual(g.elevation(26.5, 91.5), 5.0, delta=1e-6)

    def test_asc_xllcenter_is_shifted_by_half_a_cell(self):
        p = os.path.join(self.tmp, "centred.asc")
        with open(p, "w") as fh:
            fh.write("ncols 2\nnrows 2\nxllcenter 91.5\nyllcenter 26.5\n"
                     "cellsize 1.0\nNODATA_value -9999\n10 10\n10 10\n")
        g = D.load_asc(p)
        self.assertAlmostEqual(g.west, 91.0, places=6)
        self.assertAlmostEqual(g.south, 26.0, places=6)

    def test_a_void_cell_is_never_returned_as_an_elevation(self):
        # SRTM over the NER is full of 3-arc-second voids over water and cloud;
        # a void must degrade to its neighbours, never to -32768 m
        rows = [[500.0, 500.0, 500.0],
                [500.0, -32768.0, 500.0],
                [500.0, 500.0, 500.0]]
        p = os.path.join(self.tmp, "voids.asc")
        write_asc(p, 3, 3, 91.0, 26.0, 0.5, rows, nodata=-32768)
        g = D.load_asc(p)
        self.assertEqual(g.nodata, -32768.0)
        self.assertIsNone(g._cell(1, 1))
        mid = g.elevation(26.5, 91.5)
        self.assertIsNotNone(mid)
        self.assertAlmostEqual(mid, 500.0, delta=1.0)
        # an all-void window has nothing to fall back on
        write_asc(p, 2, 2, 91.0, 26.0, 1.0,
                  [[-32768.0, -32768.0], [-32768.0, -32768.0]], nodata=-32768)
        self.assertIsNone(D.load_asc(p).elevation(26.5, 91.5))

    def test_outside_coverage_returns_none(self):
        p = os.path.join(self.tmp, "tiny.asc")
        write_asc(p, 3, 3, 91.0, 26.0, 0.5, [[1] * 3] * 3)
        g = D.load_asc(p)
        self.assertIsNone(g.elevation(10.0, 10.0))
        self.assertIsNone(g.slope_deg(10.0, 10.0))

    def test_horn_slope_recovers_a_known_gradient(self):
        # a plane rising 100 m per 0.01 deg of latitude (~111 m) => ~42 deg
        n, cell = 9, 0.01
        rows = [[100.0 * (n - 1 - r) for _ in range(n)] for r in range(n)]
        p = os.path.join(self.tmp, "plane.asc")
        write_asc(p, n, n, 91.0, 26.0, cell, rows)
        g = D.load_asc(p)
        mid_lat = 26.0 + cell * (n - 1) / 2.0
        mid_lon = 91.0 + cell * (n - 1) / 2.0
        s = g.slope_deg(mid_lat, mid_lon)
        self.assertIsNotNone(s)
        self.assertAlmostEqual(s, math.degrees(math.atan(100.0 / 1111.95)),
                               delta=1.5)
        # a flat plane has no slope at all
        write_asc(p, n, n, 91.0, 26.0, cell, [[7.0] * n] * n)
        self.assertAlmostEqual(D.load_asc(p).slope_deg(mid_lat, mid_lon), 0.0,
                               delta=1e-9)

    def test_relief_is_peak_to_valley_in_the_window(self):
        rows = [[10.0, 900.0, 10.0], [10.0, 10.0, 10.0], [10.0, 10.0, 10.0]]
        p = os.path.join(self.tmp, "peak.asc")
        write_asc(p, 3, 3, 91.0, 26.0, 0.01, rows)
        g = D.load_asc(p)
        r = g.relief_m(26.02, 91.01, radius_px=1)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r, 890.0, delta=1.0)

    def test_mosaic_picks_the_tile_that_contains_the_point(self):
        m = D.DemMosaic()
        a = os.path.join(self.tmp, "N26E091.hgt")
        write_hgt(a, 3, [111] * 9)
        b = os.path.join(self.tmp, "N27E091.hgt")
        write_hgt(b, 3, [222] * 9)
        for f in (a, b):
            m.add(D.load_hgt(f))
        self.assertEqual(len(m), 2)
        self.assertEqual(m.bbox(), (91.0, 26.0, 92.0, 28.0))
        self.assertAlmostEqual(m.elevation(26.5, 91.5), 111.0, delta=1e-6)
        self.assertAlmostEqual(m.elevation(27.5, 91.5), 222.0, delta=1e-6)
        self.assertIsNone(m.elevation(25.5, 91.5))
        self.assertEqual(m.summary()["tiles"], 2)

    def test_load_dem_accepts_a_directory_a_file_and_a_gzip(self):
        import gzip
        d = os.path.join(self.tmp, "tiles")
        os.makedirs(d)
        write_asc(os.path.join(d, "a.asc"), 3, 3, 91.0, 26.0, 0.5, [[5] * 3] * 3)
        write_hgt(os.path.join(d, "N28E091.hgt"), 3, [6] * 9)
        m = D.load_dem(d, quiet=True)
        self.assertEqual(len(m), 2)
        self.assertEqual(len(D.load_dem(os.path.join(d, "a.asc"), quiet=True)), 1)
        raw = os.path.join(d, "N29E091.hgt")
        write_hgt(raw, 3, [7] * 9)
        with open(raw, "rb") as fi, gzip.open(raw + ".gz", "wb") as fo:
            fo.write(fi.read())
        self.assertEqual(len(D.load_dem(raw + ".gz", quiet=True)), 1)

    def test_load_dem_on_a_directory_with_no_rasters_says_so(self):
        # silently returning an empty mosaic would zero out slope and elevation
        # across the whole network without a word
        d = os.path.join(self.tmp, "nothing")
        os.makedirs(d)
        with self.assertRaises(FileNotFoundError) as cm:
            D.load_dem(d, quiet=True)
        self.assertIn("no DEM tiles", str(cm.exception))
        with self.assertRaises(FileNotFoundError):
            D.load_dem(os.path.join(self.tmp, "does-not-exist"), quiet=True)


# --------------------------------------------------------------------------- #
#  COLUMN SNIFFING / DATES
# --------------------------------------------------------------------------- #
class TestColumnSniffing(unittest.TestCase):
    def test_gsi_bhukosh_style_header(self):
        cm = D.guess_columns(["Sl_No", "Landslide_ID", "Latitude", "Longitude",
                              "Event_Date", "District", "State",
                              "Landslide_Type", "Severity", "Rainfall_mm"])
        self.assertEqual(cm["lat"], "Latitude")
        self.assertEqual(cm["lon"], "Longitude")
        self.assertEqual(cm["date"], "Event_Date")
        self.assertEqual(cm["district"], "District")
        self.assertEqual(cm["hazard_type"], "Landslide_Type")
        self.assertEqual(cm["severity"], "Severity")
        self.assertEqual(cm["rainfall"], "Rainfall_mm")
        self.assertEqual(cm["id"], "Landslide_ID")

    def test_units_embedded_in_the_rainfall_header_still_match(self):
        for h in ("rainfall_mm", "RainfallMM", "RAINFALL (MM)", "rain_mm",
                  "daily_rainfall", "precipitation"):
            self.assertEqual(D.guess_columns(["date", "district", h])["rainfall"],
                             h, h)

    def test_era5land_soil_moisture_column_is_found_by_substring(self):
        hdr = ["date", "lat", "lon", "volumetric_soil_water_layer_1"]
        self.assertEqual(D.guess_columns(hdr)["lat"], "lat")
        # the loader's own detector must pick the moisture column
        self.assertTrue(any("volumetricsoilwater" in D._norm_key(h)
                            for h in hdr))

    def test_unrecognised_header_maps_to_none_not_a_guess(self):
        cm = D.guess_columns(["foo", "bar", "baz"])
        self.assertIsNone(cm["lat"])
        self.assertIsNone(cm["lon"])
        self.assertIsNone(cm["date"])

    def test_norm_key_strips_units_punctuation_and_case(self):
        self.assertEqual(D._norm_key("Rainfall (mm)"), "rainfallmm")
        self.assertEqual(D._norm_key("  LATITUDE_deg "), "latitudedeg")

    def test_parse_date_accepts_the_dialects_indian_portals_use(self):
        cases = {
            "2023-07-20": dt.date(2023, 7, 20),
            "20/07/2023": dt.date(2023, 7, 20),
            "20-07-2023": dt.date(2023, 7, 20),
            "20.07.2023": dt.date(2023, 7, 20),
            "2023/07/20": dt.date(2023, 7, 20),
            "20 Jul 2023": dt.date(2023, 7, 20),
            "2023-07-20T00:00:00Z": dt.date(2023, 7, 20),
            "20230720": dt.date(2023, 7, 20),
        }
        for s, want in cases.items():
            self.assertEqual(D.parse_date(s), want, s)

    def test_parse_date_rejects_junk_and_ambiguous_nothing(self):
        for bad in ("", None, "not a date", "99/99/9999", "   "):
            self.assertIsNone(D.parse_date(bad), repr(bad))

    def test_read_csv_rows_survives_a_bom_and_a_tsv(self):
        tmp = tempfile.mkdtemp(prefix="ing-csv-")
        try:
            p = os.path.join(tmp, "bom.csv")
            with open(p, "w", encoding="utf-8-sig") as fh:
                fh.write("lat,lon\n26.1,91.7\n")
            hdr, rows = D.read_csv_rows(p)
            self.assertEqual(hdr[0], "lat")          # BOM stripped
            self.assertEqual(rows[0]["lat"], "26.1")
            q = os.path.join(tmp, "tab.tsv")
            with open(q, "w") as fh:
                fh.write("lat\tlon\n26.1\t91.7\n")
            hdr2, rows2 = D.read_csv_rows(q)
            self.assertEqual(hdr2, ["lat", "lon"])
            self.assertEqual(rows2[0]["lon"], "91.7")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  RAINFALL / MOISTURE INDICES
# --------------------------------------------------------------------------- #
class TestRainfallIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ing-rain-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_district_table_long_layout_and_api_decay(self):
        p = os.path.join(self.tmp, "imd.csv")
        rows = []
        for i, mm in enumerate((100.0, 0.0, 0.0, 50.0)):
            d = dt.date(2023, 7, 10 + i)
            rows.append([d.isoformat(), "Cachar", "Assam", mm])
        write_csv(p, ["date", "district", "state", "rainfall_mm"], rows)
        idx = D.load_rainfall(p, quiet=True)
        self.assertEqual(idx.key_kind, "district")
        self.assertAlmostEqual(idx.mm(dt.date(2023, 7, 10), "Cachar"), 100.0)
        # day 0 IS the observation date: api(13 Jul) = 50*1 + 0*k + 0*k^2
        api = idx.api(dt.date(2023, 7, 13), days=3, k=0.85, district="Cachar")
        self.assertAlmostEqual(api, 50.0, delta=0.05)
        # and the decay really is exponential over the antecedent days
        api2 = idx.api(dt.date(2023, 7, 12), days=3, k=0.85, district="Cachar")
        self.assertAlmostEqual(api2, 100.0 * 0.85 ** 2, delta=0.05)
        self.assertGreater(idx.api(dt.date(2023, 7, 11), days=3, k=0.85,
                                   district="Cachar"), api2)
        # a date with no record at all is a genuine zero, not a fallback
        self.assertEqual(idx.mm(dt.date(2019, 1, 1), "Cachar"), 0.0)

    def test_unknown_district_falls_back_to_the_daily_mean_and_is_counted(self):
        # a silent 0.0 here would look exactly like a real dry day and poison
        # the training set, so the fallback is deliberate and counted
        p = os.path.join(self.tmp, "imd.csv")
        d = dt.date(2023, 7, 10)
        write_csv(p, ["date", "district", "rainfall_mm"],
                  [[d.isoformat(), "Cachar", 60.0],
                   [d.isoformat(), "Aizawl", 20.0]])
        idx = D.load_rainfall(p, quiet=True)
        self.assertEqual(idx.fallbacks, 0)
        self.assertAlmostEqual(idx.mm(d, "Nowhere District"), 40.0)
        self.assertEqual(idx.fallbacks, 1)
        self.assertAlmostEqual(idx.mm(d, "Cachar"), 60.0)
        self.assertEqual(idx.fallbacks, 1)          # exact hits do not count

    def test_grid_layout_uses_nearest_neighbour(self):
        p = os.path.join(self.tmp, "grid.csv")
        d = dt.date(2023, 7, 10)
        write_csv(p, ["date", "lat", "lon", "rainfall_mm"],
                  [[d.isoformat(), 26.0, 91.0, 10.0],
                   [d.isoformat(), 26.0, 93.0, 90.0]])
        idx = D.load_rainfall(p, quiet=True)
        self.assertEqual(idx.key_kind, "grid")
        self.assertAlmostEqual(idx.mm(d, "", 26.05, 92.9), 90.0)
        self.assertAlmostEqual(idx.mm(d, "", 26.05, 91.1), 10.0)

    def test_wide_layout_one_column_per_day(self):
        p = os.path.join(self.tmp, "wide.csv")
        write_csv(p, ["district", "2023-07-10", "2023-07-11"],
                  [["Cachar", 12.0, 34.0]])
        idx = D.load_rainfall(p, quiet=True)
        self.assertAlmostEqual(idx.mm(dt.date(2023, 7, 11), "Cachar"), 34.0)

    def test_moisture_loader_detects_the_era5_variable_name(self):
        p = os.path.join(self.tmp, "era5.csv")
        d = dt.date(2023, 7, 10)
        write_csv(p, ["date", "lat", "lon", "volumetric_soil_water_layer_1"],
                  [[d.isoformat(), 26.0, 91.0, 0.31],
                   [d.isoformat(), 26.0, 92.0, 0.42]])
        idx, info = D.load_moisture(p, quiet=True)
        self.assertIsNotNone(idx)
        self.assertEqual(idx.key_kind, "grid")
        self.assertIn("volumetric_soil_water_layer_1", str(info))
        self.assertAlmostEqual(idx.mm(d, "", 26.0, 92.0), 0.42, places=6)

    def test_moisture_loader_rejects_a_table_without_coordinates(self):
        p = os.path.join(self.tmp, "bad.csv")
        write_csv(p, ["date", "district", "volumetric_soil_water_layer_1"],
                  [["2023-07-10", "Cachar", 0.3]])
        with self.assertRaises(ValueError):
            D.load_moisture(p, quiet=True)

    def test_missing_moisture_file_is_not_an_error(self):
        idx, info = D.load_moisture(None, quiet=True)
        self.assertIsNone(idx)
        self.assertFalse(info.get("source"))


# --------------------------------------------------------------------------- #
#  SOILGRIDS DECODE / PEDOTRANSFER
# --------------------------------------------------------------------------- #
class TestSoil(unittest.TestCase):
    RAW = {"properties": {"layers": [
        {"name": "clay", "depths": [{"label": "0-5cm", "values": {"mean": 340}}]},
        {"name": "sand", "depths": [{"label": "0-5cm", "values": {"mean": 300}}]},
        {"name": "silt", "depths": [{"label": "0-5cm", "values": {"mean": 360}}]},
        {"name": "soc", "depths": [{"label": "0-5cm", "values": {"mean": 14}}]},
        {"name": "bdod", "depths": [{"label": "0-5cm", "values": {"mean": 138}}]},
        {"name": "wv0033", "depths": [{"label": "0-5cm", "values": {"mean": 310}}]},
        {"name": "wv1500", "depths": [{"label": "0-5cm", "values": {"mean": 140}}]},
        {"name": "phh2o", "depths": [{"label": "0-5cm", "values": {"mean": 52}}]},
    ]}}

    def test_isric_integer_encoding_is_decoded_to_physical_units(self):
        out = D.SoilGridsClient._parse(self.RAW)
        self.assertAlmostEqual(out["clay"], 34.0)          # g/kg / 10 -> %
        self.assertAlmostEqual(out["sand"], 30.0)
        self.assertAlmostEqual(out["bdod"], 1.38)          # cg/cm3 / 100 -> g/cm3
        self.assertAlmostEqual(out["wv0033"], 31.0)        # /10 -> vol%
        self.assertAlmostEqual(out["wv1500"], 14.0)
        self.assertAlmostEqual(out["phh2o"], 5.2)
        self.assertAlmostEqual(out["soc"], 1.4)

    def test_parse_falls_back_to_the_median_when_mean_is_absent(self):
        doc = {"properties": {"layers": [
            {"name": "clay", "depths": [{"values": {"Q0.5": 250}}]}]}}
        self.assertAlmostEqual(D.SoilGridsClient._parse(doc)["clay"], 25.0)

    def test_parse_ignores_layers_without_values(self):
        self.assertEqual(D.SoilGridsClient._parse({"properties": {"layers": []}}), {})
        self.assertEqual(D.SoilGridsClient._parse({}), {})

    def test_cache_key_is_coarser_than_the_250_m_product(self):
        k = D.SoilGridsClient.key(26.123456, 91.765432)
        self.assertEqual(k, "26.123,91.765")
        self.assertEqual(k, D.SoilGridsClient.key(26.12349, 91.76549))

    def test_offline_client_never_dials_out_and_serves_the_nearest_point(self):
        tmp = tempfile.mkdtemp(prefix="ing-soil-")
        try:
            fp = os.path.join(tmp, "soilgrids.json")
            with open(fp, "w") as fh:
                json.dump({"26.100,91.700": {"clay": 34.0, "sand": 30.0,
                                             "silt": 36.0, "bdod": 1.38,
                                             "wv0033": 31.0, "wv1500": 14.0}}, fh)
            c = D.SoilGridsClient(cache_path=fp, offline=True, nearest_km=1.0,
                                  quiet=True)
            self.assertEqual(c.query(26.100, 91.700)["clay"], 34.0)
            self.assertEqual(c.hits, 1)
            # ~300 m away: exact key misses, nearest neighbour serves it
            got = c.query(26.1027, 91.7000)
            self.assertIsNotNone(got)
            self.assertEqual(c.near_hits, 1)
            # 200 km away: nothing to serve, and still no network call
            self.assertIsNone(c.query(24.0, 93.0))
            self.assertEqual(c.errors, 0)
            st = c.stats()
            self.assertTrue(st["offline"])
            self.assertEqual(st["cache_hits"], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_nearest_lookup_can_be_disabled(self):
        tmp = tempfile.mkdtemp(prefix="ing-soil2-")
        try:
            fp = os.path.join(tmp, "c.json")
            with open(fp, "w") as fh:
                json.dump({"26.100,91.700": {"clay": 1.0}}, fh)
            c = D.SoilGridsClient(cache_path=fp, offline=True, nearest_km=0.0,
                                  quiet=True)
            self.assertIsNone(c.query(26.1010, 91.7010))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_texture_triangle_maps_onto_the_engine_vocabulary(self):
        self.assertIn(D.soil_type_from_texture(34, 30, 36), H.SOIL_NAMES)
        self.assertEqual(D.soil_type_from_texture(55, 10, 35), "clay")
        self.assertEqual(D.soil_type_from_texture(45, 30, 25), "clay_loam")
        self.assertEqual(D.soil_type_from_texture(5, 85, 10), "sandy_loam")
        self.assertEqual(D.soil_type_from_texture(15, 25, 60), "silt_loam")

    def test_porosity_from_bulk_density_is_physical(self):
        # n = 1 - bd/2.65
        self.assertAlmostEqual(D.porosity_from_bdod(1.38), 47.9, delta=0.2)
        self.assertAlmostEqual(D.porosity_from_bdod(2.65), 25.0)   # clamped
        self.assertEqual(D.porosity_from_bdod(None), 48.0)         # default
        self.assertEqual(D.porosity_from_bdod(0.0), 48.0)

    def test_saturation_is_bounded_and_uses_porosity_not_field_capacity(self):
        # at the wilting point -> 0, at porosity -> 1
        self.assertAlmostEqual(D.saturation_from_theta(0.14, 14.0, 48.0), 0.0)
        self.assertAlmostEqual(D.saturation_from_theta(0.48, 14.0, 48.0), 1.0)
        self.assertAlmostEqual(D.saturation_from_theta(0.31, 14.0, 48.0), 0.5,
                               delta=0.02)
        # a monsoon soil above porosity must clip, not exceed 1
        self.assertEqual(D.saturation_from_theta(0.90, 14.0, 48.0), 1.0)
        self.assertEqual(D.saturation_from_theta(0.01, 14.0, 48.0), 0.0)
        # degenerate span must not divide by zero
        self.assertTrue(0.0 <= D.saturation_from_theta(0.3, 30.0, 30.0) <= 1.0)

    def test_derive_soil_features_from_real_isric_values(self):
        f = D.derive_soil_features(D.SoilGridsClient._parse(self.RAW))
        self.assertEqual(f["source"], "soilgrids")
        self.assertIn(f["soil_type"], H.SOIL_NAMES)
        self.assertAlmostEqual(f["field_capacity_pct"], 31.0)
        self.assertAlmostEqual(f["wilting_point_pct"], 14.0)
        self.assertAlmostEqual(f["awc_pct"], 17.0)
        self.assertAlmostEqual(f["porosity_pct"], 47.9, delta=0.2)
        self.assertTrue(0.0 <= f["drainage"] <= 1.0)

    def test_derive_soil_features_without_a_source_is_flagged_imputed(self):
        f = D.derive_soil_features(None)
        self.assertEqual(f["source"], "imputed")
        self.assertIsNone(f["field_capacity_pct"])
        self.assertEqual(f["porosity_pct"], 48.0)
        self.assertIn(f["soil_type"], H.SOIL_NAMES)

    def test_bucket_never_exceeds_porosity_and_drains_without_rain(self):
        b = D.SoilBucket(fc_pct=31.0, wp_pct=14.0, drainage=0.5)
        self.assertLessEqual(b.saturation, 1.0)
        for _ in range(40):                      # 40 days of 200 mm
            b.step(200.0)
            self.assertTrue(0.0 <= b.saturation <= 1.0)
        peak = b.saturation
        for _ in range(30):                      # then a dry spell
            b.step(0.0)
        self.assertLess(b.saturation, peak)
        self.assertGreaterEqual(b.saturation, 0.0)

    def test_a_poorly_draining_soil_stays_wetter_than_a_free_draining_one(self):
        wet = D.SoilBucket(fc_pct=40.0, wp_pct=15.0, drainage=0.15)
        dry = D.SoilBucket(fc_pct=40.0, wp_pct=15.0, drainage=0.90)
        for _ in range(5):
            wet.step(40.0)
            dry.step(40.0)
        self.assertGreater(wet.saturation, dry.saturation)


# --------------------------------------------------------------------------- #
#  ROAD NETWORK INGESTION
# --------------------------------------------------------------------------- #
class TestRoadIngestion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ing-roads-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, feats, name="roads.geojson"):
        p = os.path.join(self.tmp, name)
        write_geojson(p, feats)
        return p

    def test_osm_tags_map_onto_engine_edge_fields(self):
        coords = [[91.0, 26.0], [91.05, 26.0], [91.10, 26.0]]
        p = self._write([line(coords, highway="trunk", ref="NH-10",
                              name="NH-10 Guwahati-Shillong", surface="paved",
                              lanes=2, maxspeed=60, cutting="yes",
                              embankment="no", state="Assam",
                              district="Kamrup")])
        g, info = D.roads_from_geojson(p, quiet=True)
        self.assertEqual(len(g.edges), 1)
        e = g.edges[0]
        self.assertEqual(e.road_class, "trunk")
        self.assertEqual(e.highway, "NH-10")          # ref wins when it is an NH
        self.assertEqual(e.surface, "paved")
        self.assertEqual(e.lanes, 2)
        self.assertEqual(e.speed_kmh, 60.0)
        self.assertEqual(e.cut_slope, 1)
        self.assertEqual(e.state, "Assam")
        self.assertEqual(e.district, "Kamrup")
        self.assertEqual(info["edges"], 1)
        self.assertIn("cutting", info["osm_tags_used"])

    def test_maxspeed_is_clamped_and_a_missing_one_uses_the_class_default(self):
        coords = [[91.0, 26.0], [91.10, 26.0]]
        p = self._write([line(coords, highway="motorway", maxspeed=900),
                         line([[92.0, 26.0], [92.1, 26.0]], highway="track")])
        g, _ = D.roads_from_geojson(p, quiet=True)
        self.assertLessEqual(g.edges[0].speed_kmh, 100.0)
        self.assertEqual(g.edges[1].speed_kmh, D.HIGHWAY_SPEED["track"])

    def test_long_ways_are_split_to_at_most_max_length(self):
        n = 40
        coords = [[91.0 + i * 0.01, 26.0] for i in range(n)]   # ~39 km
        p = self._write([line(coords, highway="trunk", name="long")])
        g, info = D.roads_from_geojson(p, max_length_m=5000.0, quiet=True)
        self.assertGreater(len(g.edges), 1)
        for e in g.edges:
            self.assertLessEqual(e.length_m, 5600.0)   # one vertex overshoot
        self.assertGreater(info["split_count"], 0)
        # the chain stays connected end to end
        self.assertEqual(len(self._components(g)), 1)

    def test_a_t_junction_survives_length_based_splitting(self):
        # THE trap: a feeder meeting a trunk mid-way.  Splitting by length alone
        # leaves the junction as an interior vertex and the feeder becomes its
        # own connected component - the road looks connected in QGIS and every
        # route through it reports NO PATH.
        trunk = [[91.0 + i * 0.02, 26.0] for i in range(11)]    # ~20 km
        join = trunk[5]                                          # mid-way vertex
        feeder = [[join[0], join[1]], [join[0] + 0.02, join[1] + 0.03],
                  [join[0] + 0.05, join[1] + 0.05]]
        p = self._write([line(trunk, highway="trunk", name="trunk"),
                         line(feeder, highway="secondary", name="feeder")])
        g, info = D.roads_from_geojson(p, max_length_m=4000.0, quiet=True)
        self.assertGreater(info["junction_vertices"], 0)
        self.assertGreater(info["junction_splits"], 0)
        self.assertEqual(len(self._components(g)), 1,
                         "feeder road was detached from the trunk")

    def test_ways_touching_only_geometrically_are_reported_as_fragmented(self):
        # no shared vertex => genuinely disconnected, and the inspector must say so
        a = [[91.0, 26.0], [91.10, 26.0]]
        b = [[91.100001, 26.0], [91.20, 26.0]]
        p = self._write([line(a, highway="trunk"), line(b, highway="trunk")])
        g, info = D.roads_from_geojson(p, quiet=True)
        self.assertEqual(info["junction_splits"], 0)
        self.assertEqual(len(self._components(g)), 2)

    def test_multilinestring_features_are_not_discarded(self):
        multi = {"type": "Feature",
                 "geometry": {"type": "MultiLineString", "coordinates": [
                     [[91.0, 26.0], [91.05, 26.0]],
                     [[91.05, 26.0], [91.10, 26.0]]]},
                 "properties": {"highway": "primary", "name": "multi"}}
        p = self._write([multi])
        g, info = D.roads_from_geojson(p, quiet=True)
        self.assertEqual(info["line_parts"], 2)
        self.assertEqual(len(g.edges), 2)
        self.assertEqual(len(self._components(g)), 1)

    def test_degenerate_and_tagless_features_are_skipped_not_fatal(self):
        feats = [line([[91.0, 26.0]], highway="trunk"),          # 1 vertex
                 {"type": "Feature", "geometry": {"type": "Point",
                                                 "coordinates": [91, 26]},
                  "properties": {}},
                 {"type": "Feature", "properties": {}},           # no geometry
                 line([[91.0, 26.0], [91.00001, 26.0]], highway="trunk")]  # 1 m
        p = self._write(feats)
        g, info = D.roads_from_geojson(p, min_length_m=50.0, quiet=True)
        self.assertEqual(len(g.edges), 0)
        self.assertEqual(info["skipped_no_geom"], 3)
        self.assertEqual(info["skipped_short"], 1)

    def test_state_is_inferred_from_coordinates_when_the_tag_is_absent(self):
        p = self._write([line([[92.7, 24.9], [92.8, 24.95]], highway="trunk")])
        g, _ = D.roads_from_geojson(p, quiet=True)
        self.assertEqual(g.edges[0].state, D.state_of(24.9, 92.7))
        self.assertIn(g.edges[0].state, D.NER_STATE_BOXES)

    def test_sinuosity_is_length_over_chord(self):
        straight = [[91.0, 26.0], [91.10, 26.0]]
        zig = [[91.0, 26.0], [91.05, 26.05], [91.10, 26.0]]
        p = self._write([line(straight, highway="trunk", name="s"),
                         line(zig, highway="trunk", name="z")])
        # max_length_m high enough that the zig is not split: sinuosity is a
        # property of the whole way, and a split half of it is straight
        g, _ = D.roads_from_geojson(p, max_length_m=50000.0, quiet=True)
        by_name = {e.name: e for e in g.edges}
        self.assertAlmostEqual(by_name["s"].sinuosity, 1.0, delta=0.01)
        self.assertGreater(by_name["z"].sinuosity, 1.15)

    def test_densify_never_moves_the_original_vertices(self):
        coords = [[91.0, 26.0], [91.5, 26.0]]
        out = D.densify(coords, every_m=1000.0)
        self.assertEqual(out[0], [91.0, 26.0])
        self.assertEqual(out[-1], [91.5, 26.0])
        self.assertGreater(len(out), 2)
        step = H.haversine_m(out[0][1], out[0][0], out[1][1], out[1][0])
        self.assertLessEqual(step, 1000.0 * 1.05)

    def test_round_trip_through_the_engine_geojson_preserves_topology(self):
        coords = [[91.0 + i * 0.01, 26.0] for i in range(6)]
        p = self._write([line(coords, highway="trunk", name="rt")])
        g, _ = D.roads_from_geojson(p, quiet=True)
        gj = g.to_geojson()
        g2 = H.RoadGraph.from_geojson(json.loads(json.dumps(gj)))
        self.assertEqual(len(g2.edges), len(g.edges))
        self.assertEqual(len(g2.nodes), len(g.nodes))
        self.assertEqual(len(self._components(g2)), 1)

    @staticmethod
    def _components(g):
        adj = {}
        for e in g.edges:
            adj.setdefault(e.u, set()).add(e.v)
            adj.setdefault(e.v, set()).add(e.u)
        seen, out = set(), []
        for nid in g.nodes:
            if nid in seen:
                continue
            stack, comp = [nid], []
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                comp.append(x)
                stack.extend(adj.get(x, ()) - seen)
            out.append(comp)
        return out


class TestCityAnchors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ing-city-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _graph_with_node_at(self, lat, lon):
        # a short way whose ENDPOINTS (the only nodes) straddle the target point
        p = os.path.join(self.tmp, "r.geojson")
        write_geojson(p, [line([[lon - 0.002, lat], [lon + 0.002, lat]],
                               highway="trunk", name="x")])
        g, _ = D.roads_from_geojson(p, quiet=True)
        return g

    def test_a_city_inside_the_radius_becomes_resolvable_by_name(self):
        glat, glon = H.NER_CITIES["Guwahati"][0], H.NER_CITIES["Guwahati"][1]
        g = self._graph_with_node_at(glat + 0.001, glon)         # ~110 m off
        self.assertIsNone(g.resolve_endpoint("Guwahati"))        # before
        info = D.attach_city_names(g, radius_km=5.0, quiet=True)
        self.assertIn("Guwahati", info["attachment"])
        self.assertIsNotNone(g.resolve_endpoint("Guwahati"))     # after
        self.assertLess(info["attachment"]["Guwahati"]["snap_m"], 350.0)

    def test_a_city_outside_the_radius_is_not_claimed(self):
        # Agartala is nowhere near a road drawn at Guwahati
        g = self._graph_with_node_at(H.NER_CITIES["Guwahati"][0],
                                     H.NER_CITIES["Guwahati"][1])
        info = D.attach_city_names(g, radius_km=5.0, quiet=True)
        self.assertIn("Agartala", info["cities_out_of_range"])
        self.assertIsNone(g.resolve_endpoint("Agartala"))

    def test_attachment_never_moves_a_node(self):
        glat, glon = H.NER_CITIES["Guwahati"][0], H.NER_CITIES["Guwahati"][1]
        g = self._graph_with_node_at(glat, glon)
        before = {k: (v["lat"], v["lon"]) for k, v in g.nodes.items()}
        D.attach_city_names(g, radius_km=5.0, quiet=True)
        after = {k: (v["lat"], v["lon"]) for k, v in g.nodes.items()}
        self.assertEqual(before, after)

    def test_can_be_disabled(self):
        glat, glon = H.NER_CITIES["Guwahati"][0], H.NER_CITIES["Guwahati"][1]
        g = self._graph_with_node_at(glat, glon)
        info = D.attach_city_names(g, radius_km=0.0, quiet=True)
        self.assertEqual(info["cities_attached"], 0)


# --------------------------------------------------------------------------- #
#  EVENT -> ROAD SPATIAL JOIN
# --------------------------------------------------------------------------- #
class TestSpatialJoin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ing-join-")
        coords = [[91.0, 26.0], [91.05, 26.0], [91.10, 26.0]]   # ~10 km way
        p = os.path.join(self.tmp, "r.geojson")
        write_geojson(p, [line(coords, highway="trunk", name="j")])
        self.g, _ = D.roads_from_geojson(p, max_length_m=4000.0, quiet=True)
        self.seg = self.g.edges[0]
        self.mid_lat, self.mid_lon = 26.0, 91.05

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _event(self, lat, lon, d=dt.date(2023, 7, 15), **kw):
        return D.Event(lat=lat, lon=lon, date=d, district=kw.get("district", ""),
                       state=kw.get("state", ""),
                       hazard_type=kw.get("hazard_type", "landslide"),
                       severity=kw.get("severity", "High"), rainfall_mm=None,
                       source_id=kw.get("source_id", "X"), raw={})

    def test_point_to_polyline_distance_is_perpendicular_not_to_an_endpoint(self):
        # 0.001 deg of latitude ~ 111 m north of the way's midpoint
        d = D._point_to_polyline_m(26.001, 91.05, self.seg.coords)
        self.assertAlmostEqual(d, 111.0, delta=6.0)
        self.assertAlmostEqual(D._point_to_polyline_m(26.0, 91.05,
                                                      self.seg.coords), 0.0,
                               delta=1e-6)

    def test_an_event_mid_segment_matches_even_though_the_centroid_is_far(self):
        # the bug this guards: indexing only the segment centroid hides a hit
        # that is metres from the carriageway but kilometres from the centroid
        ev = self._event(26.0004, 91.05)                 # ~45 m off the way
        m, info = D.join_events_to_segments([ev], self.g, buffer_m=100.0,
                                            quiet=True)
        self.assertIn(self.seg.segment_id, m)
        self.assertEqual(info["events_matched"], 1)
        self.assertLess(info["closest_m"], 100.0)

    def test_buffer_is_respected(self):
        near = self._event(26.0004, 91.05)               # ~45 m
        far = self._event(26.0040, 91.05)                # ~445 m
        m, _ = D.join_events_to_segments([near, far], self.g, buffer_m=100.0,
                                         quiet=True)
        matched = {ei for v in m.values() for ei, _d in v}
        self.assertEqual(matched, {0})
        m2, info2 = D.join_events_to_segments([near, far], self.g,
                                              buffer_m=500.0, quiet=True)
        self.assertEqual({ei for v in m2.values() for ei, _d in v}, {0, 1})
        self.assertEqual(info2["events_matched"], 2)

    def test_events_far_from_any_road_are_reported_not_dropped_silently(self):
        ev = self._event(27.5, 93.5)
        m, info = D.join_events_to_segments([ev], self.g, buffer_m=100.0,
                                            quiet=True)
        self.assertEqual(m, {})
        self.assertEqual(info["events_matched"], 0)
        self.assertEqual(info["events_total"], 1)

    def test_one_event_can_match_several_segments_and_keeps_its_distance(self):
        ev = self._event(26.0004, 91.05)
        m, _ = D.join_events_to_segments([ev], self.g, buffer_m=2000.0,
                                         quiet=True)
        self.assertGreaterEqual(len(m), 1)
        for hits in m.values():
            for ei, dist in hits:
                self.assertEqual(ei, 0)
                self.assertLessEqual(dist, 2000.0)

    def test_inventory_loading_normalises_types_and_counts_undated_rows(self):
        p = os.path.join(self.tmp, "inv.csv")
        write_csv(p, ["Sl_No", "Landslide_ID", "Latitude", "Longitude",
                      "Event_Date", "District", "State", "Landslide_Type",
                      "Severity"],
                  [[1, "A1", "26.10", "91.70", "20/07/2023", "Kamrup", "Assam",
                    "Debris Flow", "High"],
                   [2, "A2", "26.20", "91.80", "", "Kamrup", "Assam",
                    "Landslide", "Low"],
                   [3, "A3", "bad", "91.80", "20/07/2023", "Kamrup", "Assam",
                    "Rock Fall", "High"]])
        events, info = D.load_events(p, quiet=True)
        self.assertEqual(info["rows_read"], 3)
        self.assertEqual(len(events), 2)                  # the bad-lat row is out
        self.assertEqual(info["kept"], 2)
        self.assertEqual(info["with_date"], 1)
        self.assertEqual(info["no_date"], 1)
        self.assertEqual(events[0].hazard_type, "debris_flow")
        self.assertIn("debris_flow", info["type_mix"])
        self.assertEqual(events[0].date, dt.date(2023, 7, 20))

    def test_hazard_types_normalise_onto_the_engine_vocabulary(self):
        for raw in ("Landslide", "LANDSLIDE", "land slide", "Debris Flow",
                    "debris_flow", "Rock Avalanche"):
            self.assertIn(D.normalise_hazard_type(raw), H.HAZARD_TYPES)
        self.assertEqual(D.normalise_hazard_type(""), "landslide")


# --------------------------------------------------------------------------- #
#  FUSION -> TRAINING TABLE
# --------------------------------------------------------------------------- #
class TestFusion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ing-fuse-")
        cls.paths = D.fabricate_demo_inputs(cls.tmp, seed=SEED, quiet=True)
        cls.graph, cls.road_info = D.roads_from_geojson(cls.paths["roads"],
                                                        quiet=True)
        cls.events, cls.ev_info = D.load_events(cls.paths["landslides"],
                                                quiet=True)
        cls.rain = D.load_rainfall(cls.paths["rainfall"], quiet=True)
        cls.moist, cls.moist_info = D.load_moisture(cls.paths["moisture"],
                                                    quiet=True)
        cls.dem = D.load_dem(cls.paths["dem"], quiet=True)
        D.enrich_graph_with_dem(cls.graph, cls.dem, None, quiet=True)
        soil = D.SoilGridsClient(cache_path=cls.paths["soilgrids_cache"],
                                 offline=True, quiet=True)
        cls.soil_info = D.enrich_graph_with_soil(cls.graph, soil, quiet=True)
        cls.retention = cls.soil_info.pop("retention", {})
        cls.matches, cls.join_info = D.join_events_to_segments(
            cls.events, cls.graph, buffer_m=100.0, quiet=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _fuse(self, **kw):
        args = dict(year=2023, obs_per_segment=4, positive_window_days=3,
                    max_positives_per_segment=6, seed=SEED, quiet=True)
        args.update(kw)
        return D.build_training_rows(self.graph, self.events, self.matches,
                                     self.rain, self.moist, self.retention,
                                     **args)

    def test_fabricated_demo_inputs_are_in_the_real_source_formats(self):
        self.assertTrue(os.path.exists(self.paths["roads"]))
        self.assertTrue(any(f.endswith(".hgt") for f in
                            os.listdir(os.path.join(self.tmp, "srtm"))))
        self.assertTrue(any(f.endswith(".asc") for f in
                            os.listdir(os.path.join(self.tmp, "srtm"))))
        self.assertEqual(len(self.events), 180)
        self.assertEqual(self.ev_info["with_date"], 180)

    def test_every_row_carries_exactly_the_engine_csv_columns(self):
        rows, _ = self._fuse()
        self.assertTrue(rows)
        want = set(H.HAZARD_CSV_COLUMNS)
        for r in rows[:25]:
            self.assertTrue(want.issubset(r), sorted(want - set(r)))

    def test_case_control_design_puts_positives_at_event_dates(self):
        rows, info = self._fuse()
        self.assertGreater(info["positive"], 0)
        self.assertEqual(info["positive"] + info["negative"], info["rows"])
        self.assertEqual(len(rows), info["rows"])
        event_dates = {e.date.isoformat() for e in self.events if e.date}
        for r in rows:
            if r["disrupted"]:
                self.assertIn(r["timestamp"][:10], event_dates,
                              "a positive row must sit on a real event date")
                self.assertNotEqual(r["hazard_type"], "none")
                self.assertGreater(r["closure_hours"], 0.0)
                self.assertIn(r["source"],
                              {e.source_id for e in self.events} | {"GSI_Bhukosh"})
            else:
                self.assertEqual(r["hazard_type"], "none")
                self.assertEqual(r["closure_hours"], 0.0)
                self.assertEqual(r["source"], "imputed_negative")

    def test_no_negative_row_sits_inside_a_positive_window(self):
        rows, _ = self._fuse(positive_window_days=3)
        by_seg = {}
        for r in rows:
            by_seg.setdefault(r["segment_id"], []).append(r)
        for sid, rs in by_seg.items():
            pos = [dt.date.fromisoformat(r["timestamp"][:10]) for r in rs
                   if r["disrupted"]]
            for r in rs:
                if r["disrupted"]:
                    continue
                d = dt.date.fromisoformat(r["timestamp"][:10])
                for pd in pos:
                    self.assertGreater(abs((d - pd).days), 3,
                                       f"{sid}: negative at {d} inside the "
                                       f"positive window of {pd}")

    def test_positives_per_segment_are_capped(self):
        rows, _ = self._fuse(max_positives_per_segment=2)
        counts = {}
        for r in rows:
            if r["disrupted"]:
                counts[r["segment_id"]] = counts.get(r["segment_id"], 0) + 1
        self.assertTrue(counts)
        self.assertLessEqual(max(counts.values()), 2)

    def test_obs_per_segment_controls_the_negative_count(self):
        _, two = self._fuse(obs_per_segment=2)
        _, six = self._fuse(obs_per_segment=6)
        self.assertGreater(six["negative"], two["negative"])

    def test_hist_freq_is_an_expanding_window_and_never_leaks_the_label(self):
        rows, info = self._fuse()
        self.assertIn("expanding-window", info["hist_freq_source"])
        by_seg = {}
        for r in rows:
            by_seg.setdefault(r["segment_id"], []).append(r)
        # every event date per segment, straight from the inventory
        ev_dates = {}
        for sid, hits in self.matches.items():
            for ei, _d in hits:
                if self.events[ei].date:
                    ev_dates.setdefault(sid, []).append(self.events[ei].date)
        checked = 0
        for sid, rs in by_seg.items():
            dates = sorted(set(ev_dates.get(sid, [])))
            if not dates:
                continue
            for r in rs:
                d = dt.date.fromisoformat(r["timestamp"][:10])
                expected = sum(1 for x in dates if x < d)     # STRICTLY before
                got = r["hist_freq_per_km"]
                if expected == 0:
                    self.assertEqual(got, 0.0, f"{sid} {d}")
                else:
                    self.assertGreater(got, 0.0, f"{sid} {d}")
                checked += 1
        self.assertGreater(checked, 10)
        # and a first-ever event must not already know about itself
        for sid, rs in by_seg.items():
            dates = sorted(set(ev_dates.get(sid, [])))
            if not dates:
                continue
            first = [r for r in rs if r["disrupted"]
                     and dt.date.fromisoformat(r["timestamp"][:10]) == dates[0]]
            for r in first:
                self.assertEqual(r["hist_freq_per_km"], 0.0,
                                 "the first event on a segment leaked into its "
                                 "own history feature")

    def test_hist_freq_is_normalised_by_segment_length(self):
        rows, _ = self._fuse()
        for r in rows:
            self.assertGreaterEqual(r["hist_freq_per_km"], 0.0)
            self.assertLess(r["hist_freq_per_km"], 1e4)

    def test_weather_features_are_populated_from_the_rainfall_table(self):
        rows, info = self._fuse()
        self.assertGreater(info["rainfall_days_in_season"], 0)
        self.assertFalse(info["rainfall_climatology_used"])
        wet = [r for r in rows if r["rain_24h_mm"] > 0]
        self.assertGreater(len(wet), len(rows) * 0.3)
        for r in rows:
            self.assertGreaterEqual(r["api_3d_mm"], 0.0)
            self.assertTrue(0.0 <= r["soil_saturation"] <= 1.0)
            self.assertGreaterEqual(r["rain_mm_hr"], 0.0)
            self.assertIn(r["season_phase"],
                          ("pre_monsoon", "onset", "peak", "late", "post"))

    def test_observations_stay_inside_the_monsoon_season(self):
        rows, info = self._fuse(year=2023)
        s0, s1 = D.season_of_year(2023)
        for r in rows:
            d = dt.date.fromisoformat(r["timestamp"][:10])
            self.assertTrue(s0 <= d <= s1, f"{d} outside {s0}..{s1}")
        self.assertEqual(info["season"], [s0.isoformat(), s1.isoformat()])

    def test_missing_rainfall_falls_back_to_climatology_and_says_so(self):
        rows, info = D.build_training_rows(self.graph, self.events, self.matches,
                                           None, None, self.retention, year=2023,
                                           seed=SEED, quiet=False)
        self.assertTrue(info["rainfall_climatology_used"])
        self.assertEqual(info["rainfall_days_in_season"], 0)
        self.assertTrue(rows)
        self.assertGreater(sum(r["rain_24h_mm"] for r in rows), 0.0)

    def test_a_rainfall_table_from_the_wrong_year_falls_back_too(self):
        tmp = tempfile.mkdtemp(prefix="ing-oldrain-")
        try:
            p = os.path.join(tmp, "old.csv")
            write_csv(p, ["date", "district", "rainfall_mm"],
                      [["2011-07-10", "Cachar", 80.0],
                       ["2011-07-11", "Cachar", 90.0]])
            old = D.load_rainfall(p, quiet=True)
            self.assertTrue(old.by_date)                  # it parsed fine
            rows, info = D.build_training_rows(self.graph, self.events,
                                               self.matches, old, None,
                                               self.retention, year=2023,
                                               seed=SEED, quiet=True)
            self.assertTrue(info["rainfall_climatology_used"])
            self.assertTrue(rows)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_without_moisture_the_bucket_model_still_produces_saturation(self):
        with_m, _ = self._fuse()
        without, _ = D.build_training_rows(self.graph, self.events, self.matches,
                                           self.rain, None, self.retention,
                                           year=2023, seed=SEED, quiet=True)
        self.assertEqual(len(with_m), len(without))
        a = {r["record_id"]: r["soil_saturation"] for r in with_m}
        b = {r["record_id"]: r["soil_saturation"] for r in without}
        self.assertNotEqual(a, b, "ERA5-Land moisture changed nothing")
        for v in list(a.values()) + list(b.values()):
            self.assertTrue(0.0 <= v <= 1.0)

    def test_fusion_is_deterministic_for_a_fixed_seed(self):
        r1, i1 = self._fuse()
        r2, i2 = self._fuse()
        self.assertEqual(i1, i2)
        self.assertEqual(json.dumps(r1, sort_keys=True, default=str),
                         json.dumps(r2, sort_keys=True, default=str))

    def test_a_different_seed_changes_the_negatives_but_not_the_design(self):
        r1, i1 = self._fuse(seed=SEED)
        r2, i2 = self._fuse(seed=SEED + 1)
        self.assertEqual(i1["rows"], i2["rows"])
        self.assertEqual(i1["positive"], i2["positive"])
        self.assertNotEqual(json.dumps(r1, sort_keys=True, default=str),
                            json.dumps(r2, sort_keys=True, default=str))

    def test_the_engine_can_train_on_the_fused_table(self):
        rows, _ = self._fuse()
        out = os.path.join(self.tmp, "fused.csv")
        H.write_hazard_csv(rows, out)
        loaded = H.load_hazard_records(out)
        self.assertEqual(len(loaded), len(rows))
        self.assertEqual(sum(r["disrupted"] for r in loaded),
                         sum(r["disrupted"] for r in rows))
        # and the engine's own feature vector builds from every fused row
        for r in loaded[:25]:
            vec = H.record_features(r)
            self.assertEqual(len(vec), len(H.FEATURES))
            for v in vec:
                self.assertIsInstance(v, float)
                self.assertFalse(math.isnan(v), r["record_id"])

    def test_provenance_separates_real_columns_from_imputed_ones(self):
        rows, fuse = self._fuse()
        dem_info = D.enrich_graph_with_dem(self.graph, self.dem, None, quiet=True)
        city_info = D.attach_city_names(self.graph, radius_km=5.0, quiet=True)
        prov = D.provenance_report(dem_info, self.soil_info, self.rain,
                                   self.moist_info, self.ev_info,
                                   self.road_info, self.join_info, fuse,
                                   city_info)
        real = set(prov["columns_from_real_data"])
        imp = set(prov["columns_imputed"])
        self.assertFalse(real & imp)
        for c in ("slope_deg", "elevation_m", "rain_mm_hr", "api_3d_mm",
                  "soil_saturation", "hist_freq_per_km", "disrupted",
                  "soil_type", "drainage", "cut_slope"):
            self.assertIn(c, real | imp)
        # with every source present, only NDVI has no supplier
        self.assertEqual(imp, {"ndvi"})
        self.assertEqual(prov["sources"]["city_anchors"]["cities_attached"],
                         city_info["cities_attached"])
        json.dumps(prov, default=str)                 # must be serialisable

    def test_provenance_flags_a_missing_label_source(self):
        empty, fuse = D.build_training_rows(self.graph, [], {}, self.rain,
                                            self.moist, self.retention,
                                            year=2023, seed=SEED, quiet=True)
        self.assertEqual(fuse["positive"], 0)
        prov = D.provenance_report(None, {"soilgrids_points": 0}, None, {},
                                   {"kept": 0}, {}, {"events_matched": 0}, fuse)
        self.assertIn("disrupted", prov["columns_imputed"])
        self.assertIn("NO POSITIVE",
                      prov["column_provenance"]["disrupted"]["source"])
        self.assertFalse(prov["column_provenance"]["disrupted"]["real_data"])

    def test_zero_positive_rows_is_reported_loudly(self):
        # an inventory that covers a different area than the roads must not
        # quietly produce an all-negative training set
        rows, info = D.build_training_rows(self.graph, self.events, {},
                                           self.rain, self.moist,
                                           self.retention, year=2023,
                                           seed=SEED, quiet=False)
        self.assertEqual(info["positive"], 0)
        self.assertTrue(rows)


# --------------------------------------------------------------------------- #
#  END TO END / CLI
# --------------------------------------------------------------------------- #
class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ing-e2e-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, args, expect=0):
        r = subprocess.run([sys.executable, INGEST] + args, capture_output=True,
                           text=True, timeout=900, cwd=ROOT)
        self.assertEqual(r.returncode, expect,
                         f"args={args}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
        return r

    def test_demo_end_to_end_writes_the_three_artifacts(self):
        out = os.path.join(self.tmp, "e2e")
        r = self._run(["--demo", "--seed", str(SEED),
                       "--out", os.path.join(out, "hazards.csv"),
                       "--graph-out", os.path.join(out, "roads.geojson"),
                       "--report", os.path.join(out, "report.json")])
        self.assertIn("fused training rows", r.stdout)
        csv_p = os.path.join(out, "hazards.csv")
        gj_p = os.path.join(out, "roads.geojson")
        rp_p = os.path.join(out, "report.json")
        for p in (csv_p, gj_p, rp_p):
            self.assertTrue(os.path.exists(p), p)
        rows = H.load_hazard_records(csv_p)
        self.assertGreater(len(rows), 100)
        self.assertGreater(sum(x["disrupted"] for x in rows), 0)
        g = H.RoadGraph.from_geojson(jload(gj_p))
        self.assertGreater(len(g.edges), 50)
        self.assertIsNotNone(g.resolve_endpoint("Guwahati"))
        prov = jload(rp_p)
        self.assertIn("columns_from_real_data", prov)
        self.assertIn("columns_imputed", prov)
        # the demo must tell the truth about its own fabricated soil cache
        self.assertTrue(prov["sources"]["soil"].get("cache_is_fabricated"))

    def test_demo_is_bit_reproducible(self):
        a, b = os.path.join(self.tmp, "rep-a"), os.path.join(self.tmp, "rep-b")
        for d in (a, b):
            self._run(["--demo", "--seed", str(SEED), "--quiet",
                       "--out", os.path.join(d, "h.csv"),
                       "--graph-out", os.path.join(d, "g.geojson"),
                       "--report", os.path.join(d, "r.json")])
        self.assertEqual(jread(os.path.join(a, "h.csv")),
                         jread(os.path.join(b, "h.csv")))
        # the graph carries a generation timestamp, so compare geometry only
        ga = jload(os.path.join(a, "g.geojson"))
        gb = jload(os.path.join(b, "g.geojson"))
        self.assertEqual([f["geometry"] for f in ga["features"]],
                         [f["geometry"] for f in gb["features"]])
        self.assertEqual(ga["metadata"]["nodes"], gb["metadata"]["nodes"])

    def test_a_different_seed_changes_the_inventory(self):
        a = os.path.join(self.tmp, "seed-a")
        self._run(["--demo", "--seed", str(SEED), "--quiet",
                   "--out", os.path.join(a, "h.csv"),
                   "--graph-out", os.path.join(a, "g.geojson"),
                   "--report", os.path.join(a, "r.json")])
        b = os.path.join(self.tmp, "seed-b")
        self._run(["--demo", "--seed", "1234", "--quiet",
                   "--out", os.path.join(b, "h.csv"),
                   "--graph-out", os.path.join(b, "g.geojson"),
                   "--report", os.path.join(b, "r.json")])
        self.assertNotEqual(jread(os.path.join(a, "h.csv")),
                            jread(os.path.join(b, "h.csv")))

    def test_the_fused_table_retrains_the_engine_and_routes(self):
        out = os.path.join(self.tmp, "train")
        self._run(["--demo", "--seed", str(SEED), "--quiet",
                   "--out", os.path.join(out, "h.csv"),
                   "--graph-out", os.path.join(out, "g.geojson"),
                   "--report", os.path.join(out, "r.json")])
        art = os.path.join(out, "artifacts")
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "hazard_prediction_engine.py"),
             "--hazards", os.path.join(out, "h.csv"),
             "--graph", os.path.join(out, "g.geojson"),
             "--retrain", "--out", art, "--no-benchmark"],
            capture_output=True, text=True, timeout=1500, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        self.assertIn("ROC AUC", r.stdout)
        self.assertNotIn("NO PATH", r.stdout.split("[4/5]")[-1],
                         "an ingested, city-named network must still route")
        card = jload(os.path.join(art, "hazard_model.json"))
        self.assertGreater(card["metrics"]["cv"]["roc_auc"], 0.75)
        self.assertEqual(card["metrics"]["n_rows"],
                         len(H.load_hazard_records(os.path.join(out, "h.csv"))))
        self.assertEqual(card["features"], list(H.FEATURES))

    def test_train_flag_chains_straight_into_the_engine(self):
        out = os.path.join(self.tmp, "chain")
        r = self._run(["--demo", "--seed", str(SEED), "--quiet", "--train",
                       "--out", os.path.join(out, "h.csv"),
                       "--graph-out", os.path.join(out, "g.geojson"),
                       "--report", os.path.join(out, "r.json")])
        self.assertIn("[6] training the hazard engine", r.stdout)
        self.assertIn("ROC AUC", r.stdout)
        # the child's report must come AFTER our own banner, not before it
        self.assertLess(r.stdout.index("[5] result"),
                        r.stdout.index("SIH26002 - HAZARD PREDICTION ENGINE"))

    def test_missing_required_inputs_fail_with_a_useful_message(self):
        r = self._run(["--out", os.path.join(self.tmp, "x.csv")], expect=2)
        self.assertIn("required", r.stdout)
        self.assertIn("--roads", r.stdout)

    def test_inspect_recognises_each_format(self):
        demo = os.path.join(self.tmp, "insp")
        self._run(["--demo", "--seed", str(SEED), "--quiet",
                   "--out", os.path.join(demo, "h.csv"),
                   "--graph-out", os.path.join(demo, "g.geojson"),
                   "--report", os.path.join(demo, "r.json")])
        raw = os.path.join(ROOT, "data", "raw")
        r = self._run(["--inspect", os.path.join(raw,
                                                 "gsi_landslide_inventory.csv")])
        self.assertIn("hazard inventory", r.stdout)
        self.assertIn("lat<-Latitude", r.stdout)
        r = self._run(["--inspect", os.path.join(raw,
                                                 "imd_district_daily_rainfall.csv")])
        self.assertIn("rainfall by district", r.stdout)
        # a district rainfall table has no coordinates and that is NOT an error
        self.assertNotIn("required but unmapped", r.stdout)
        r = self._run(["--inspect", os.path.join(raw,
                                                 "era5land_soil_moisture.csv")])
        self.assertIn("soil-moisture grid", r.stdout)
        r = self._run(["--inspect", os.path.join(raw, "ner_roads.geojson")])
        self.assertIn("GeoJSON FeatureCollection", r.stdout)
        self.assertIn("connected component", r.stdout)
        self.assertIn("segments /", r.stdout)
        r = self._run(["--inspect", os.path.join(raw, "srtm")])
        self.assertIn("DEM mosaic", r.stdout)
        self.assertIn("elevation:", r.stdout)
        r = self._run(["--inspect", os.path.join(raw, "srtm", "N27E092.hgt")])
        self.assertIn("301x301", r.stdout)

    def test_inspect_a_file_that_does_not_exist(self):
        r = self._run(["--inspect", os.path.join(self.tmp, "nope.csv")],
                      expect=2)
        self.assertIn("no such file", r.stdout)

    def test_help_documents_the_sources(self):
        r = subprocess.run([sys.executable, INGEST, "--help"],
                           capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(r.returncode, 0)
        for flag in ("--roads", "--landslides", "--dem", "--rainfall",
                     "--moisture", "--soil", "--buffer", "--train", "--demo",
                     "--inspect", "--city-radius", "--soil-nearest-km"):
            self.assertIn(flag, r.stdout, flag)


class TestZeroDependency(unittest.TestCase):
    def test_module_runs_with_every_optional_package_blocked(self):
        script = (
            "import sys\n"
            "BLOCKED={'numpy','pandas','sklearn','scipy','xgboost','torch',"
            "'networkx','joblib','matplotlib','rasterio','geopandas','osmnx',"
            "'shapely','requests','pyproj','fiona','folium','affine'}\n"
            "class B:\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if fullname.split('.')[0] in BLOCKED:\n"
            "            raise ImportError('blocked: '+fullname)\n"
            "        return None\n"
            "sys.meta_path.insert(0, B())\n"
            "for m in list(sys.modules):\n"
            "    if m.split('.')[0] in BLOCKED: del sys.modules[m]\n"
            f"sys.path.insert(0, {ROOT!r})\n"
            "import data_ingestion as di\n"
            "assert not any(di.HAS.values()), di.HAS\n"
            "import hazard_prediction_engine as hpe\n"
            "assert hpe.resolve_backend('auto') == 'pure'\n"
            "print('INGEST-ZERO-DEP OK')\n"
        )
        r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                           text=True, timeout=300, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("INGEST-ZERO-DEP OK", r.stdout)

    def test_geotiff_without_rasterio_explains_the_workaround(self):
        if D.rasterio is not None:
            self.skipTest("rasterio is installed")
        tmp = tempfile.mkdtemp(prefix="ing-tif-")
        try:
            p = os.path.join(tmp, "dem.tif")
            open(p, "wb").write(b"not really a tiff")
            with self.assertRaises(RuntimeError) as cm:
                D.load_geotiff(p)
            self.assertIn("gdal_translate", str(cm.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)



# --------------------------------------------------------------------------- #
#  STATE / BBOX RESOLUTION  (--state, --bbox)
# --------------------------------------------------------------------------- #
class TestStateAndBbox(unittest.TestCase):
    def test_every_ner_state_resolves_to_itself(self):
        for st in D.NER_STATE_BOXES:
            self.assertEqual(D.resolve_state(st), st)

    def test_aliases_short_codes_and_case_all_resolve(self):
        for alias, canon in (("Arunachal", "Arunachal Pradesh"),
                             ("ARUNACHAL PRADESH", "Arunachal Pradesh"),
                             ("mz", "Mizoram"), ("MZ", "Mizoram"),
                             ("  tripura  ", "Tripura"),
                             ("ap", "Arunachal Pradesh"),
                             ("as", "Assam")):
            self.assertEqual(D.resolve_state(alias), canon, alias)

    def test_an_unknown_state_is_none_never_a_near_miss(self):
        for bad in ("Bihar", "Kerala", "", None, "North East", "West Bengal"):
            self.assertIsNone(D.resolve_state(bad), bad)

    def test_state_boxes_are_well_formed_and_inside_the_ner(self):
        for st, (w, s, e, n) in D.NER_STATE_BOXES.items():
            self.assertLess(w, e, st)
            self.assertLess(s, n, st)
            self.assertTrue(85.0 <= w and e <= 100.0, f"{st} longitude {w},{e}")
            self.assertTrue(20.0 <= s and n <= 32.0, f"{st} latitude {s},{n}")

    def test_both_bbox_orderings_give_one_answer(self):
        # S,W,N,E (Overpass / most GIS) and W,S,E,N (OSMnx) must not be
        # confusable: the loader detects the ordering from the magnitudes
        want = (91.5, 21.9, 93.5, 24.5)
        self.assertEqual(D.parse_bbox("21.9,91.5,24.5,93.5"), want)   # S,W,N,E
        self.assertEqual(D.parse_bbox("91.5,21.9,93.5,24.5"), want)   # W,S,E,N
        self.assertEqual(D.parse_bbox([21.9, 91.5, 24.5, 93.5]), want)
        self.assertEqual(D.parse_bbox((91.5, 21.9, 93.5, 24.5)), want)

    def test_bbox_rejects_a_wrong_count(self):
        for bad in ("21.9,91.5,24.5", "1,2,3,4,5", ""):
            with self.assertRaises(ValueError, msg=bad):
                D.parse_bbox(bad)

    def test_bbox_rejects_reversed_corners_rather_than_swapping_them(self):
        with self.assertRaises(ValueError) as cm:
            D.parse_bbox("93.5,21.9,91.5,24.5")     # east < west
        self.assertIn("empty box", str(cm.exception))

    def test_bbox_rejects_a_box_bigger_than_any_ner_state(self):
        # a whole-subcontinent box would pull hundreds of thousands of ways and
        # then be silently cropped, so it is refused up front
        with self.assertRaises(ValueError) as cm:
            D.parse_bbox("8.0,68.0,37.0,97.0")
        self.assertIn("larger than any NER state", str(cm.exception))

    def test_bbox_rejects_out_of_range_coordinates(self):
        for bad in ("21.9,911.5,24.5,93.5", "219.0,91.5,245.0,93.5"):
            with self.assertRaises(ValueError, msg=bad):
                D.parse_bbox(bad)


# --------------------------------------------------------------------------- #
#  LIVE ROAD FETCH  (Overpass / OSMnx -> engine GeoJSON)
# --------------------------------------------------------------------------- #
class _FakeLine:
    """Stand-in for a shapely LineString: only `.coords` is read."""

    def __init__(self, coords):
        self.coords = coords


class _FakeGraph:
    """Stand-in for an OSMnx MultiDiGraph, so the converter can be tested
    without geopandas, shapely, pyproj or osmnx installed."""

    def __init__(self, nodes, edges):
        self.nodes = nodes
        self._edges = edges

    def edges(self, keys=False, data=False):
        assert keys and data
        return iter(self._edges)

    def number_of_nodes(self):
        return len(self.nodes)

    def number_of_edges(self):
        return len(self._edges)


class TestRoadFetchers(unittest.TestCase):
    def test_overpass_reply_becomes_a_line_featurecollection(self):
        doc = {"elements": [
            {"type": "node", "id": 1, "lat": 23.7, "lon": 92.7},      # skipped
            {"type": "relation", "id": 9},                             # skipped
            {"type": "way", "id": 111, "tags": {"highway": "primary",
                                                "name": "NH54",
                                                "surface": "asphalt",
                                                "tiger:county": "x"},   # dropped
             "geometry": [{"lat": 23.70, "lon": 92.70},
                          {"lat": 23.72, "lon": 92.75},
                          {"lat": 23.75, "lon": 92.80}]},
            {"type": "way", "id": 222, "tags": {"highway": "track"},
             "geometry": [{"lat": 23.7, "lon": 92.7}]},                # <2 pts
        ]}
        gj = D.overpass_to_geojson(doc)
        self.assertEqual(gj["type"], "FeatureCollection")
        self.assertEqual(len(gj["features"]), 1)
        f = gj["features"][0]
        self.assertEqual(f["geometry"]["type"], "LineString")
        # Overpass gives lat/lon objects; GeoJSON wants [lon, lat] arrays
        self.assertEqual(f["geometry"]["coordinates"][0], [92.70, 23.70])
        self.assertEqual(f["properties"]["highway"], "primary")
        self.assertEqual(f["properties"]["name"], "NH54")
        self.assertEqual(f["properties"]["osm_id"], 111)
        self.assertNotIn("tiger:county", f["properties"])

    def test_osmnx_conversion_needs_neither_geopandas_nor_shapely(self):
        nodes = {1: {"x": 92.70, "y": 23.70}, 2: {"x": 92.75, "y": 23.72},
                 3: {"x": 92.80, "y": 23.75}}
        edges = [
            (1, 2, 0, {"highway": "primary", "length": 5200.0, "osmid": 111,
                       "name": "NH54"}),
            (2, 3, 0, {"highway": ["secondary", "track"], "osmid": 222,
                       "geometry": _FakeLine([(92.75, 23.72), (92.78, 23.74),
                                              (92.80, 23.75)])}),
        ]
        gj = D.osmnx_to_geojson(_FakeGraph(nodes, edges))
        self.assertEqual(len(gj["features"]), 2)
        f0, f1 = gj["features"]
        # a straight edge is rebuilt from its endpoint nodes
        self.assertEqual(f0["geometry"]["coordinates"],
                         [[92.70, 23.70], [92.75, 23.72]])
        # a curved edge keeps every vertex, so length and slope stay honest
        self.assertEqual(len(f1["geometry"]["coordinates"]), 3)
        # OSMnx stores multi-valued tags as lists; the loader wants one scalar
        self.assertEqual(f1["properties"]["highway"], "secondary")
        self.assertEqual(f0["properties"]["length"], 5200.0)

    def test_a_degenerate_osmnx_edge_is_skipped_not_emitted(self):
        nodes = {1: {"x": 92.70, "y": 23.70}, 2: {"x": 92.75, "y": 23.72}}
        edges = [(1, 2, 0, {"highway": "track",
                            "geometry": _FakeLine([(92.70, 23.70)])})]
        self.assertEqual(D.osmnx_to_geojson(_FakeGraph(nodes, edges))["features"],
                         [])

    def test_fetched_ways_load_into_the_engine_and_stay_connected(self):
        # the whole point of the conversion: shared OSM node ids must survive as
        # shared coordinates, or the loader rebuilds each way as its own island
        nodes = {i: {"x": 92.70 + 0.05 * i, "y": 23.70 + 0.02 * i}
                 for i in range(5)}
        edges = [(i, i + 1, 0, {"highway": "primary", "osmid": 100 + i})
                 for i in range(4)]
        gj = D.osmnx_to_geojson(_FakeGraph(nodes, edges))
        tmp = tempfile.mkdtemp(prefix="ing-ox-")
        try:
            p = os.path.join(tmp, "osm.geojson")
            with open(p, "w") as fh:
                json.dump(gj, fh)
            g, info = D.roads_from_geojson(p, quiet=True)
            self.assertEqual(len(g.edges), 4)
            self.assertEqual(H._largest_component(g), len(g.nodes),
                             "a fetched chain must be ONE component")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_osmnx_absent_raises_importerror_so_overpass_is_tried(self):
        if D.osmnx() is not None:
            self.skipTest("osmnx is installed")
        with self.assertRaises(ImportError):
            D.fetch_roads_osmnx((92.0, 23.0, 92.5, 23.5))


# --------------------------------------------------------------------------- #
#  CROPPING A FETCH TO A BUDGET
# --------------------------------------------------------------------------- #
def lattice(bbox=(92.0, 23.0, 92.8, 23.8), n=16):
    """A connected lattice of OSM ways covering a bbox, ~5.5 km per cell."""
    w, s, e, nn = bbox
    step = (e - w) / n
    feats = []
    for i in range(n + 1):
        for j in range(n):
            y = s + i * (nn - s) / n
            feats.append(line([[w + j * step, y], [w + (j + 1) * step, y]],
                              highway="primary", osm_id=1000 + len(feats)))
    for j in range(n + 1):
        for i in range(n):
            x = w + j * step
            feats.append(line([[x, s + i * (nn - s) / n],
                               [x, s + (i + 1) * (nn - s) / n]],
                              highway="secondary", osm_id=5000 + len(feats)))
    return {"type": "FeatureCollection", "features": feats}


class TestCropToBudget(unittest.TestCase):
    BBOX = (92.0, 23.0, 92.8, 23.8)

    def _largest_fraction(self, gj):
        tmp = tempfile.mkdtemp(prefix="ing-crop-")
        try:
            p = os.path.join(tmp, "g.geojson")
            with open(p, "w") as fh:
                json.dump(gj, fh)
            g, _ = D.roads_from_geojson(p, quiet=True)
            if not g.nodes:
                return 0.0
            return H._largest_component(g) / float(len(g.nodes))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_under_budget_is_returned_untouched(self):
        gj = lattice(self.BBOX, n=4)
        out, info = D.crop_to_budget(gj, self.BBOX, max_edges=10 ** 6)
        self.assertFalse(info["cropped"])
        self.assertIs(out, gj)

    def test_zero_budget_means_unlimited(self):
        gj = lattice(self.BBOX, n=4)
        out, info = D.crop_to_budget(gj, self.BBOX, max_edges=0)
        self.assertFalse(info["cropped"])
        self.assertEqual(len(out["features"]), len(gj["features"]))

    def test_cropping_keeps_the_network_connected_where_sampling_would_not(self):
        gj = lattice(self.BBOX, n=16)
        n = len(gj["features"])
        budget = n // 8
        cropped, info = D.crop_to_budget(gj, self.BBOX, budget)
        self.assertTrue(info["cropped"])
        self.assertLess(len(cropped["features"]), n)
        self.assertGreater(len(cropped["features"]), 0)
        # the window must be inside the request and centred on the inventory
        w2, s2, e2, n2 = info["window"]
        self.assertTrue(self.BBOX[0] - 1e-9 <= w2 < e2 <= self.BBOX[2] + 1e-9)
        self.assertTrue(self.BBOX[1] - 1e-9 <= s2 < n2 <= self.BBOX[3] + 1e-9)

        crop_frac = self._largest_fraction(cropped)
        import random as _random
        rng = _random.Random(0)
        sampled = {"type": "FeatureCollection",
                   "features": rng.sample(gj["features"], budget)}
        sample_frac = self._largest_fraction(sampled)
        # row-wise sampling is the obvious way to cap a network and it shreds
        # it: every surviving way loses its neighbours, so routing reports NO
        # PATH for every corridor while nothing raises an error
        self.assertGreater(crop_frac, 0.90,
                           f"cropped network fragmented: {crop_frac:.2f}")
        self.assertGreater(crop_frac, sample_frac,
                           f"crop {crop_frac:.2f} vs sample {sample_frac:.2f}")

    def test_the_crop_centres_on_the_inventory_not_the_box_centre(self):
        gj = lattice(self.BBOX, n=16)
        budget = len(gj["features"]) // 8
        _, info = D.crop_to_budget(gj, self.BBOX, budget, centre=(23.1, 92.1))
        self.assertEqual(info["centre"], [23.1, 92.1])
        w2, s2, e2, n2 = info["window"]
        # the window must sit in the south-west corner, not the middle
        self.assertLess(0.5 * (w2 + e2), 0.5 * (self.BBOX[0] + self.BBOX[2]))
        self.assertLess(0.5 * (s2 + n2), 0.5 * (self.BBOX[1] + self.BBOX[3]))

    def test_a_centre_outside_the_box_is_clamped_into_it(self):
        gj = lattice(self.BBOX, n=8)
        _, info = D.crop_to_budget(gj, self.BBOX, 10, centre=(80.0, 80.0))
        clat, clon = info["centre"]
        self.assertTrue(self.BBOX[1] <= clat <= self.BBOX[3])
        self.assertTrue(self.BBOX[0] <= clon <= self.BBOX[2])

    def test_inventory_centroid_is_a_median_so_outliers_cannot_move_it(self):
        d = dt.date(2023, 7, 15)
        evs = [D.Event(23.0 + 0.01 * i, 92.0 + 0.01 * i, d) for i in range(9)]
        evs.append(D.Event(-33.9, 151.2, d))     # a valid but mistyped point
        lat, lon = D.inventory_centroid(evs)
        self.assertAlmostEqual(lat, 23.035, places=6)
        self.assertAlmostEqual(lon, 92.045, places=6)
        # the mean would have been dragged 5.7 degrees south by that one row
        mean_lat = sum(e.lat for e in evs) / len(evs)
        self.assertLess(abs(lat - 23.04), abs(mean_lat - 23.04))

    def test_inventory_centroid_ignores_bad_and_missing_coordinates(self):
        d = dt.date(2023, 7, 15)
        self.assertIsNone(D.inventory_centroid([]))
        self.assertIsNone(D.inventory_centroid([D.Event(None, None, d)]))
        got = D.inventory_centroid([D.Event(999.0, 999.0, d),
                                    D.Event(24.0, 93.0, d)])
        self.assertEqual(got, (24.0, 93.0))


# --------------------------------------------------------------------------- #
#  acquire_roads: cache-first, offline-safe, honest about failure
# --------------------------------------------------------------------------- #
class TestAcquireRoads(unittest.TestCase):
    BBOX = (92.0, 23.0, 92.8, 23.8)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ing-fetch-")
        self._osmnx, self._overpass = D.fetch_roads_osmnx, D.fetch_roads_overpass
        self._env = os.environ.pop("SIH_IGNORE_ROAD_CACHE", None)

    def tearDown(self):
        D.fetch_roads_osmnx, D.fetch_roads_overpass = self._osmnx, self._overpass
        if self._env is not None:
            os.environ["SIH_IGNORE_ROAD_CACHE"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _no_network(self, *a, **k):
        raise AssertionError("the network must not be touched")

    def test_a_cache_hit_never_touches_the_network(self):
        cache = os.path.join(self.tmp, "osm.geojson")
        gj = lattice(self.BBOX, n=4)
        gj["metadata"] = {"bbox": list(self.BBOX)}
        with open(cache, "w") as fh:
            json.dump(gj, fh)
        D.fetch_roads_osmnx = self._no_network
        D.fetch_roads_overpass = self._no_network
        path, info = D.acquire_roads(self.BBOX, cache_path=cache, offline=True,
                                     quiet=True)
        self.assertEqual(path, cache)
        self.assertEqual(info["source"], "cache")
        self.assertTrue(info["cache_matches_request"])
        self.assertEqual(info["ways"], len(gj["features"]))

    def test_offline_without_a_cache_fails_with_a_way_forward(self):
        D.fetch_roads_osmnx = self._no_network
        with self.assertRaises(RuntimeError) as cm:
            D.acquire_roads(self.BBOX,
                            cache_path=os.path.join(self.tmp, "missing.geojson"),
                            offline=True, quiet=True)
        msg = str(cm.exception)
        self.assertIn("--offline", msg)
        self.assertIn("--roads", msg)

    def test_a_fetch_is_cached_so_the_next_run_is_offline(self):
        gj = lattice(self.BBOX, n=4)
        calls = []

        def fake_osmnx(bbox, network_type="drive", simplify=True, quiet=False):
            calls.append(bbox)
            return gj

        D.fetch_roads_osmnx = fake_osmnx
        cache = os.path.join(self.tmp, "nested", "osm.geojson")
        path, info = D.acquire_roads(self.BBOX, state="Mizoram",
                                     cache_path=cache, quiet=True)
        self.assertEqual(info["source"], "osmnx")
        self.assertEqual(path, cache)
        self.assertTrue(os.path.exists(cache))
        self.assertEqual(len(calls), 1)

        doc = jload(cache)
        self.assertEqual(doc["metadata"]["state"], "Mizoram")
        self.assertEqual(doc["metadata"]["bbox"], [round(v, 6) for v in self.BBOX])
        self.assertIn("fetched", doc["metadata"])

        # second run: offline, no fetch, same file
        D.fetch_roads_osmnx = self._no_network
        path2, info2 = D.acquire_roads(self.BBOX, cache_path=cache, offline=True,
                                       quiet=True)
        self.assertEqual(path2, cache)
        self.assertEqual(info2["source"], "cache")

    def test_osmnx_failure_falls_back_to_overpass(self):
        gj = lattice(self.BBOX, n=4)

        def boom(*a, **k):
            raise RuntimeError("overpass-api.de rate limited us")

        D.fetch_roads_osmnx = boom
        D.fetch_roads_overpass = lambda bbox, network_type="drive", timeout=180, \
            quiet=False: (gj, "https://overpass.kumi.systems/api/interpreter")
        _, info = D.acquire_roads(self.BBOX,
                                  cache_path=os.path.join(self.tmp, "o.geojson"),
                                  quiet=True)
        self.assertEqual(info["source"], "overpass")
        self.assertIn("rate limited", info["osmnx_error"])

    def test_an_empty_fetch_is_reported_not_silently_written(self):
        D.fetch_roads_osmnx = lambda *a, **k: {"type": "FeatureCollection",
                                               "features": []}
        D.fetch_roads_overpass = lambda *a, **k: (
            {"type": "FeatureCollection", "features": []}, "url")
        with self.assertRaises(RuntimeError) as cm:
            D.acquire_roads(self.BBOX,
                            cache_path=os.path.join(self.tmp, "o.geojson"),
                            quiet=True)
        # the most common cause is a transposed bbox, so say so
        self.assertIn("0 ways", str(cm.exception))
        self.assertIn("S,W,N,E", str(cm.exception))

    def test_a_cache_from_a_different_window_is_refetched(self):
        cache = os.path.join(self.tmp, "osm.geojson")
        other = (93.0, 24.0, 93.8, 24.8)
        gj = lattice(other, n=4)
        gj["metadata"] = {"bbox": list(other)}
        with open(cache, "w") as fh:
            json.dump(gj, fh)
        fresh = lattice(self.BBOX, n=4)
        D.fetch_roads_osmnx = lambda *a, **k: fresh
        path, info = D.acquire_roads(self.BBOX, cache_path=cache, quiet=True)
        self.assertEqual(info["source"], "osmnx")
        self.assertEqual(info["stale_cache_refreshed_from"], list(other))
        self.assertEqual(jload(path)["metadata"]["bbox"],
                         [round(v, 6) for v in self.BBOX])

    def test_offline_forces_a_mismatched_cache_and_says_so_loudly(self):
        cache = os.path.join(self.tmp, "osm.geojson")
        gj = lattice((93.0, 24.0, 93.8, 24.8), n=4)
        gj["metadata"] = {"bbox": [93.0, 24.0, 93.8, 24.8]}
        with open(cache, "w") as fh:
            json.dump(gj, fh)
        D.fetch_roads_osmnx = self._no_network
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _, info = D.acquire_roads(self.BBOX, cache_path=cache, offline=True)
        self.assertEqual(info["source"], "cache")
        self.assertFalse(info["cache_matches_request"])
        out = buf.getvalue()
        self.assertIn("not the requested", out)
        self.assertIn("--offline forces its use", out)

    def test_max_edges_is_applied_to_a_fetch(self):
        gj = lattice(self.BBOX, n=16)
        D.fetch_roads_osmnx = lambda *a, **k: gj
        _, info = D.acquire_roads(self.BBOX, max_edges=60,
                                  cache_path=os.path.join(self.tmp, "o.geojson"),
                                  quiet=True)
        self.assertTrue(info["crop"]["cropped"])
        self.assertLess(info["ways"], len(gj["features"]))


# --------------------------------------------------------------------------- #
#  --slope-tif: a pre-computed slope product, in degrees or percent
# --------------------------------------------------------------------------- #
def write_slope_asc(path, value, ncols=8, nrows=8, xll=92.0, yll=23.0,
                    cell=0.1, nodata=-9999.0):
    rows = [[value] * ncols for _ in range(nrows)]
    write_asc(path, ncols, nrows, xll, yll, cell, rows, nodata=nodata)


class TestSlopeRaster(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ing-slope-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _road(self, coords=((92.2, 23.3), (92.4, 23.5))):
        p = os.path.join(self.tmp, "road.geojson")
        write_geojson(p, [line([list(c) for c in coords], highway="primary")])
        g, _ = D.roads_from_geojson(p, quiet=True)
        self.assertEqual(len(g.edges), 1)
        return g

    def test_a_slope_product_in_degrees_is_read_as_degrees(self):
        p = os.path.join(self.tmp, "slope30.asc")
        write_slope_asc(p, 30.0)
        info = D.classify_slope_raster(D.load_dem(p, quiet=True))
        self.assertEqual(info["unit"], "degrees")
        self.assertEqual(info["max"], 30.0)
        self.assertGreater(info["samples"], 0)

    def test_a_slope_product_in_percent_is_detected_as_percent(self):
        p = os.path.join(self.tmp, "slope_pct.asc")
        write_slope_asc(p, 150.0)          # 150% cannot be degrees
        info = D.classify_slope_raster(D.load_dem(p, quiet=True))
        self.assertEqual(info["unit"], "percent")

    def test_a_nodata_sentinel_is_flagged_not_averaged_in(self):
        p = os.path.join(self.tmp, "slope_nodata.asc")
        write_slope_asc(p, -9999.0)
        info = D.classify_slope_raster(D.load_dem(p, quiet=True))
        # every cell is nodata, so nothing is readable at all
        self.assertEqual(info["samples"], 0)
        self.assertIn("no readable values", info.get("note", ""))

    def test_percent_gradient_converts_through_atan_not_by_dividing(self):
        # 100% gradient is 45 degrees; /100 would have said 1 degree
        p = os.path.join(self.tmp, "slope100.asc")
        write_slope_asc(p, 100.0)
        raster = D.load_dem(p, quiet=True)
        g = self._road()
        info = D.enrich_graph_with_dem(g, None, None, slope_raster=raster,
                                       quiet=True)
        self.assertEqual(info["slope_raster"]["unit"], "percent")
        self.assertAlmostEqual(g.edges[0].slope_deg, 45.0, places=1)
        self.assertEqual(info["slope_raster_segments"], 1)

    def test_a_degrees_raster_drives_slope_deg_with_no_dem_at_all(self):
        p = os.path.join(self.tmp, "slope22.asc")
        write_slope_asc(p, 22.0)
        raster = D.load_dem(p, quiet=True)
        g = self._road()
        info = D.enrich_graph_with_dem(g, None, None, slope_raster=raster,
                                       quiet=True)
        self.assertEqual(info["dem_hits"], 0)
        self.assertEqual(info["slope_raster_segments"], 1)
        self.assertAlmostEqual(g.edges[0].slope_deg, 22.0, places=1)
        self.assertAlmostEqual(info["slope_deg_mean"], 22.0, places=1)

    def test_the_raster_takes_precedence_over_dem_derived_slope(self):
        slope_p = os.path.join(self.tmp, "slope40.asc")
        write_slope_asc(slope_p, 40.0)
        dem_p = os.path.join(self.tmp, "flat_dem.asc")
        # a near-flat DEM would derive ~0 degrees; the raster must win
        write_asc(dem_p, 8, 8, 92.0, 23.0, 0.1,
                  [[100 + (0 if j < 4 else 1) for j in range(8)]
                   for _ in range(8)])
        g = self._road()
        info = D.enrich_graph_with_dem(g, D.load_dem(dem_p, quiet=True), None,
                                       slope_raster=D.load_dem(slope_p, quiet=True),
                                       quiet=True)
        self.assertEqual(info["dem_hits"], 1, "elevation must still come from the DEM")
        self.assertAlmostEqual(g.edges[0].slope_deg, 40.0, places=1)
        self.assertAlmostEqual(g.edges[0].elevation_m, 100.5, places=1)

    def test_a_raster_that_misses_every_segment_warns_instead_of_faking_it(self):
        p = os.path.join(self.tmp, "far_away.asc")
        write_slope_asc(p, 30.0, xll=80.0, yll=10.0)     # nowhere near the road
        g = self._road()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            info = D.enrich_graph_with_dem(g, None, None,
                                           slope_raster=D.load_dem(p, quiet=True))
        self.assertEqual(info["slope_raster_hits"], 0)
        self.assertEqual(info["slope_raster_segments"], 0)
        self.assertIn("covered NONE of the segments", buf.getvalue())

    def test_slope_tif_without_dem_says_what_stays_imputed(self):
        p = os.path.join(self.tmp, "slope22.asc")
        write_slope_asc(p, 22.0)
        g = self._road()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            D.enrich_graph_with_dem(g, None, None,
                                    slope_raster=D.load_dem(p, quiet=True))
        self.assertIn("--slope-tif without --dem", buf.getvalue())
        self.assertIn("elevation_m", buf.getvalue())


# --------------------------------------------------------------------------- #
#  logging output (--log-level / --log-file)
# --------------------------------------------------------------------------- #
class TestLoggingOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ing-log-")

    def tearDown(self):
        import logging
        D._LOGGING_ON = False
        # handlers must be CLOSED, not just dropped: a FileHandler left open
        # trips ResourceWarning, and this suite is run with -W error
        for h in list(D.log.handlers):
            try:
                h.close()
            except Exception:
                pass
            D.log.removeHandler(h)
        D.log.setLevel(logging.WARNING)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_output_is_plain_stdout_so_it_can_be_piped(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            D._log("  plain message")
        self.assertEqual(buf.getvalue(), "  plain message\n")

    def test_configure_logging_adds_timestamps_and_level(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # StreamHandler captures sys.stdout at construction time, so the
            # handler has to be built inside the redirect to be captured
            D.configure_logging("INFO")
            D._log("  routed message")
            D._warn("  a warning")
        out = buf.getvalue()
        self.assertIn("routed message", out)
        self.assertIn("INFO", out)
        self.assertIn("WARNING", out)
        self.assertRegex(out, r"\d\d:\d\d:\d\d \| ")

    def test_log_file_is_written_and_directories_are_created(self):
        lf = os.path.join(self.tmp, "deep", "nested", "ingest.log")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # configure_logging always keeps a stdout handler alongside the file
            D.configure_logging("INFO", lf)
            D._log("  to file")
        self.assertTrue(os.path.exists(lf))
        self.assertIn("to file", jread(lf))
        self.assertIn("to file", buf.getvalue())

    def test_log_level_filters_below_the_threshold(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            D.configure_logging("ERROR")
            D._log("  should be filtered")
            D.log.error("  but errors pass")
        out = buf.getvalue()
        self.assertNotIn("should be filtered", out)
        self.assertIn("but errors pass", out)

    def test_quiet_still_suppresses_everything(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            D.configure_logging("INFO")
            D._log("  silenced", quiet=True)
            D._warn("  silenced too", quiet=True)
            D._log("  not silenced")
        out = buf.getvalue()
        self.assertNotIn("silenced too", out)
        self.assertEqual(out.count("  silenced"), 0)
        self.assertIn("not silenced", out)

    def test_optional_packages_reports_osmnx_presence(self):
        self.assertIn("osmnx", D.HAS)
        self.assertIsInstance(D.HAS["osmnx"], bool)
        # presence is detected with find_spec, which must agree with importing
        self.assertEqual(D.HAS["osmnx"], D.osmnx() is not None)


# --------------------------------------------------------------------------- #
#  THE COMMAND LINES FROM THE DOCS, END TO END
# --------------------------------------------------------------------------- #
def asc_covering(path, bbox, value, cell=0.02, cap=400):
    """Write a constant-value ESRI .asc grid that fully covers a bbox."""
    w, s, e, n = bbox
    ncols = min(cap, max(4, int(math.ceil((e - w) / cell)) + 2))
    nrows = min(cap, max(4, int(math.ceil((n - s) / cell)) + 2))
    xll, yll = w - cell, s - cell
    write_asc(path, ncols, nrows, xll, yll, cell,
              [[value] * ncols for _ in range(nrows)])
    return path


class TestFetchCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ing-cli-")
        cls.fab = D.fabricate_demo_inputs(cls.tmp, seed=SEED, quiet=True)
        cls.inventory = cls.fab["landslides"]
        cls.roads = cls.fab["roads"]
        # the fabricated file is a raw OSM-style FeatureCollection with no
        # metadata block, so the extent has to be measured off the geometry
        doc = jload(cls.roads)

        def _pts(g):
            if g["type"] == "LineString":
                return g["coordinates"]
            return [c for part in g["coordinates"] for c in part]

        pts = [c for f in doc["features"] for c in _pts(f["geometry"])]
        cls.bbox = (min(p[0] for p in pts), min(p[1] for p in pts),
                    max(p[0] for p in pts), max(p[1] for p in pts))   # W,S,E,N
        # a network on disk that looks exactly like a completed fetch
        cls.cache = os.path.join(cls.tmp, "cached_fetch.geojson")
        doc["metadata"] = {"crs": "EPSG:4326", "source": "overpass",
                           "bbox": [round(v, 6) for v in cls.bbox],
                           "state": None, "network_type": "drive"}
        with open(cls.cache, "w") as fh:
            json.dump(doc, fh)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, args, expect=0, env=None):
        e = dict(os.environ)
        e.update(env or {})
        r = subprocess.run([sys.executable, INGEST] + args, capture_output=True,
                           text=True, timeout=900, cwd=ROOT, env=e)
        self.assertEqual(r.returncode, expect,
                         f"args={args}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
        return r

    def _out(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return [os.path.join(d, "hazards.csv"), os.path.join(d, "roads.geojson"),
                os.path.join(d, "report.json")]

    # -- argument validation -------------------------------------------------
    def test_unknown_state_exits_2_and_lists_the_known_ones(self):
        r = self._run(["--state", "Bihar"], expect=2)
        self.assertIn("unknown --state", r.stdout)
        self.assertIn("Mizoram", r.stdout)
        self.assertIn("Arunachal Pradesh", r.stdout)

    def test_a_bad_bbox_exits_2_with_the_reason(self):
        r = self._run(["--state", "Mizoram", "--bbox", "1,2,3"], expect=2)
        self.assertIn("--bbox", r.stdout)
        self.assertIn("expected 4 numbers", r.stdout)

    def test_missing_everything_still_names_both_ways_to_supply_roads(self):
        r = self._run(["--landslides", self.inventory], expect=2)
        self.assertIn("--roads", r.stdout)
        self.assertIn("--state/--bbox", r.stdout)

    def test_offline_with_no_cache_exits_4_and_points_at_a_manual_download(self):
        r = self._run(["--state", "Mizoram", "--offline",
                       "--landslides", self.inventory,
                       "--road-cache", os.path.join(self.tmp, "nope.geojson")],
                      expect=4)
        self.assertIn("could not obtain a road network", r.stdout)
        self.assertIn("--roads", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    # -- the cache path ------------------------------------------------------
    def test_a_cached_network_runs_the_whole_pipeline_offline(self):
        csv_p, gj_p, rp_p = self._out("cache-hit")
        w, s, e, n = self.bbox
        r = self._run(["--bbox", f"{s},{w},{n},{e}",          # S,W,N,E on purpose
                       "--offline", "--road-cache", self.cache,
                       "--landslides", self.inventory,
                       "--out", csv_p, "--graph-out", gj_p, "--report", rp_p])
        self.assertIn("road cache", r.stdout)
        prov = jload(rp_p)
        fetch = prov["sources"]["roads"]["fetch"]
        self.assertEqual(fetch["source"], "cache")
        self.assertTrue(fetch["cache_matches_request"])
        self.assertTrue(os.path.exists(csv_p))

    def test_a_cache_from_another_window_is_used_offline_but_announced(self):
        csv_p, gj_p, rp_p = self._out("cache-mismatch")
        r = self._run(["--state", "Mizoram", "--offline",
                       "--road-cache", self.cache,
                       "--landslides", self.inventory,
                       "--out", csv_p, "--graph-out", gj_p, "--report", rp_p])
        # routing over another region's roads must never be silent
        self.assertIn("not the requested", r.stdout)
        prov = jload(rp_p)
        self.assertFalse(prov["sources"]["roads"]["fetch"]["cache_matches_request"])
        self.assertEqual(prov["sources"]["roads"]["fetch"]["state"], "Mizoram")

    # -- --gsi-csv alias ----------------------------------------------------
    def test_gsi_csv_is_an_alias_for_landslides(self):
        csv_p, gj_p, rp_p = self._out("alias")
        self._run(["--roads", self.cache, "--gsi-csv", self.inventory,
                   "--out", csv_p, "--graph-out", gj_p, "--report", rp_p])
        self.assertGreater(len(H.load_hazard_records(csv_p)), 100)

    # -- the schema contract with the engine --------------------------------
    def test_the_fused_table_gives_the_engine_nonzero_weather_features(self):
        # the failure mode this guards: a table whose rain columns are named
        # rainfall_mm_hr / api_3day loads without error, then every row's
        # rain_mm_hr and api_3d come out 0.0 and the model trains blind to
        # weather while reporting a healthy AUC
        csv_p, gj_p, rp_p = self._out("schema")
        self._run(["--roads", self.cache, "--landslides", self.inventory,
                   "--dem", self.fab["dem"],
                   "--rainfall", self.fab["rainfall"],
                   "--moisture", self.fab["moisture"],
                   "--out", csv_p, "--graph-out", gj_p, "--report", rp_p])
        # the header must carry the ENGINE's column names, not near-misses: a
        # table called rainfall_mm_hr / api_3day loads without complaint and
        # then trains on zero rainfall
        header = jread(csv_p).splitlines()[0].split(",")
        for col in ("rain_mm_hr", "api_3d_mm", "slope_deg", "soil_saturation",
                    "hist_freq_per_km", "length_m", "disrupted"):
            self.assertIn(col, header)
        rows = H.load_hazard_records(csv_p)
        self.assertTrue(rows)
        feats = [H.record_features(r) for r in rows]
        names = list(H.FEATURES)
        i_rain, i_api, i_slope, i_len = (names.index(k) for k in
                                         ("rain_mm_hr", "api_3d", "slope_deg",
                                          "length_m"))
        for key, idx in (("rain_mm_hr", i_rain), ("api_3d", i_api),
                         ("slope_deg", i_slope), ("length_m", i_len)):
            nonzero = sum(1 for f in feats if f[idx] > 0)
            self.assertGreater(nonzero, 0.5 * len(feats),
                               f"{key} is zero for most rows - the column name "
                               f"did not reach the engine")
        # and the graph the engine reads back must be the same topology
        g = H.RoadGraph.from_geojson(jload(gj_p))
        self.assertGreater(len(g.edges), 50)
        self.assertEqual(H._largest_component(g), len(g.nodes),
                         "the emitted graph must stay ONE component")

    # -- --slope-tif end to end ---------------------------------------------
    def test_slope_tif_drives_slope_deg_and_is_recorded_in_provenance(self):
        csv_p, gj_p, rp_p = self._out("slope")
        slope_p = asc_covering(os.path.join(self.tmp, "slope33.asc"),
                               self.bbox, 33.0)
        self._run(["--roads", self.cache, "--landslides", self.inventory,
                   "--slope-tif", slope_p,
                   "--out", csv_p, "--graph-out", gj_p, "--report", rp_p])
        prov = jload(rp_p)
        self.assertIn("--slope-tif", prov["column_provenance"]["slope_deg"]["source"])
        self.assertTrue(prov["column_provenance"]["slope_deg"]["real_data"])
        g = H.RoadGraph.from_geojson(jload(gj_p))
        covered = [e for e in g.edges if e.slope_deg == 33.0]
        self.assertGreater(len(covered), 0.9 * len(g.edges),
                           f"only {len(covered)}/{len(g.edges)} segments took the "
                           f"raster value")

    # -- logging ------------------------------------------------------------
    def test_log_level_switches_to_timestamped_logging_and_still_finishes(self):
        csv_p, gj_p, rp_p = self._out("logging")
        logf = os.path.join(self.tmp, "run.log")
        r = self._run(["--roads", self.cache, "--landslides", self.inventory,
                       "--log-level", "INFO", "--log-file", logf,
                       "--out", csv_p, "--graph-out", gj_p, "--report", rp_p])
        self.assertRegex(r.stdout, r"\d\d:\d\d:\d\d \| INFO")
        self.assertIn("fused training rows", r.stdout)
        self.assertTrue(os.path.exists(logf))
        self.assertIn("fused training rows", jread(logf))

    def test_the_version_line_reports_the_live_fetch_backend(self):
        r = self._run(["--version"])
        self.assertIn("osmnx=", r.stdout)
        self.assertIn("rasterio=", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
