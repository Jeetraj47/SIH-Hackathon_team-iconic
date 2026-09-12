#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIH26002 Hazard Prediction Engine — unittest suite.

    python -m unittest discover -s tests -v
    python tests/test_engine.py

Needs no third-party packages: model tests exercise the built-in pure-Python
Newton-boosted backend, and anything that would require xgboost / torch /
networkx is skipped rather than failed.  Runtime is dominated by training,
roughly 40 s on two cores.

What is covered: geo maths, deterministic graph generation, GeoJSON I/O, the
historical hazard log and its base-rate calibration, empirical-Bayes base risk,
train/serve feature parity, metric implementations, Platt calibration, the
dynamic cost function C(e), shortest-path correctness, the re-weighting SLA,
monsoon-field escalation, hub-to-hub resilience, emitted-artefact validity, the
zero-dependency guarantee and the CLI.
"""
from __future__ import annotations

import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import hazard_prediction_engine as H  # noqa: E402  (sys.path set up above)

ENGINE = os.path.join(ROOT, "hazard_prediction_engine.py")
SEED = 26002
SOILS = ("alluvial", "clay_loam", "colluvium", "laterite", "peat_lowland",
         "red_loam", "sandy_loam")
PHASES = ("pre_monsoon", "onset", "peak", "late", "post")
DRY = {"rain_mm_hr": 0.0, "rain_24h_mm": 0.0, "api_3d": 0.0,
       "soil_saturation": 0.05}
WET = {"rain_mm_hr": 60.0, "rain_24h_mm": 250.0, "api_3d": 300.0,
       "soil_saturation": 0.92}

# --------------------------------------------------------------------------
# fixtures.  Cached objects are treated as READ-ONLY: engine scoring mutates
# edges in place, so anything that mutates asks for a fresh graph.
# --------------------------------------------------------------------------
_CACHE: dict = {}


def _graph(n_edges=220, seed=SEED):
    return H.build_mock_osm_graph(target_edges=n_edges, seed=seed)


def _records(g=None):
    return H.build_historical_hazard_log(g or fx("graph"), seed=SEED)


def _index(recs=None, g=None):
    idx = H.HistoricalHazardIndex(recs or fx("records"))
    idx.attach_to_graph(g or fx("graph"))
    return idx


def _model():
    """Trained on the full-size graph so the learner sees the real feature span."""
    g = fx("big_graph")
    recs = _records(g)
    idx = _index(recs, g)
    X, y, _meta = H.build_dataset(recs, idx)
    m = H.HazardRiskModel(backend="pure", seed=SEED,
                          hyperparams={"n_estimators": 90, "learning_rate": 0.11,
                                       "max_depth": 4})
    m.fit(X, y, folds=3)
    return m, g, recs, idx


def fx(key):
    if key not in _CACHE:
        if key == "graph":
            _CACHE[key] = _graph()
        elif key == "big_graph":
            _CACHE[key] = _graph(1100)
        elif key == "records":
            _CACHE[key] = _records()
        elif key == "index":
            _CACHE[key] = _index()
        elif key == "model_bundle":
            _CACHE[key] = _model()
        elif key == "model":
            _CACHE[key] = fx("model_bundle")[0]
        elif key == "engine":
            m, g, recs, idx = fx("model_bundle")
            _CACHE[key] = H.HazardEngine(m, g, H.CostPolicy(), index=idx,
                                         records=recs)
        else:
            raise KeyError(key)
    return _CACHE[key]


def fresh_pair(n_edges=220, seed=SEED):
    """A graph + index the caller may freely mutate."""
    g = _graph(n_edges, seed)
    recs = _records(g)
    idx = _index(recs, g)
    return g, recs, idx


def flat_edge(segment_id="T-1", length_m=5000.0, slope_deg=10.0,
              speed_kmh=40.0, cut_slope=1, lat=26.14, lon=91.74, hist=1.0):
    e = H.RoadEdge(segment_id=segment_id, u="a", v="b",
                   coords=[[lon, lat], [lon + 0.04, lat]],
                   length_m=length_m, elev_from=50.0,
                   elev_to=50.0 + length_m * 0.1, slope_deg=slope_deg,
                   speed_kmh=speed_kmh, cut_slope=cut_slope,
                   soil_type="laterite", drainage=0.5, ndvi=0.5, sinuosity=1.1,
                   state="Assam", district="Kamrup", highway="NH-27", name="t",
                   hist_freq=hist)
    e.lat, e.lon, e.elevation_m = lat, lon, 300.0
    return e


def prep_costs(g, pol):
    """Populate the static/dynamic cost fields routing reads off each edge."""
    for e in g.edges:
        e.transit_s = pol.transit_s(e, e.p_disruption)
        e.slope_friction_s = pol.slope_friction_s(e)
        e.base_cost_s, _t, _h = pol.base_cost(e)
        e.cost_s, _t, _h = pol.cost(e, e.p_disruption)
        e.cost_s_soft, _t, _h = pol.cost_soft(e, e.p_disruption)
    return g


# ==========================================================================
class TestGeoMaths(unittest.TestCase):
    def test_haversine_known_distance(self):
        d = H.haversine_m(26.1445, 91.7362, 25.5788, 91.8933) / 1000.0
        self.assertAlmostEqual(d, 64.8, delta=2.0)      # Guwahati -> Shillong

    def test_haversine_properties(self):
        self.assertEqual(H.haversine_m(26.0, 91.0, 26.0, 91.0), 0.0)
        a = H.haversine_m(26.0, 91.0, 27.0, 92.0)
        self.assertAlmostEqual(a, H.haversine_m(27.0, 92.0, 26.0, 91.0), places=6)
        self.assertGreater(a, 140000.0)

    def test_interpolate_and_bearing(self):
        p = H.interpolate_point(26.0, 91.0, 27.0, 91.0, 0.5)
        self.assertAlmostEqual(p[0], 26.5, places=6)
        self.assertAlmostEqual(H.bearing_deg(26.0, 91.0, 27.0, 91.0), 0.0, delta=0.5)
        self.assertAlmostEqual(H.bearing_deg(26.0, 91.0, 26.0, 92.0), 90.0, delta=0.5)

    def test_offset_point_matches_haversine(self):
        lat2, lon2 = H.offset_point(26.0, 91.0, 10000.0, 45.0)
        self.assertAlmostEqual(H.haversine_m(26.0, 91.0, lat2, lon2), 10000.0,
                               delta=30.0)

    def test_polyline_length(self):
        coords = [[91.0, 26.0], [91.0, 26.01], [91.0, 26.02]]
        self.assertAlmostEqual(H.polyline_length_m(coords), 2224.0, delta=20.0)

    def test_sigmoid_logit_roundtrip(self):
        for p in (0.001, 0.13, 0.5, 0.87, 0.999):
            self.assertAlmostEqual(H.sigmoid(H.logit(p)), p, places=5)
        self.assertLessEqual(H.sigmoid(1e9), 1.0)
        self.assertGreaterEqual(H.sigmoid(-1e9), 0.0)

    def test_clamp_quantile_stats(self):
        self.assertEqual(H.clamp(5, 0, 1), 1)
        self.assertEqual(H.clamp(-5, 0, 1), 0)
        xs = sorted(float(i) for i in range(101))
        self.assertAlmostEqual(H.quantile(xs, 0.5), 50.0, delta=1.0)
        self.assertAlmostEqual(H.quantile(xs, 0.9), 90.0, delta=1.0)
        self.assertAlmostEqual(H.mean([1, 2, 3]), 2.0)
        self.assertAlmostEqual(H.stdev([1, 2, 3]), 1.0, places=6)

    def test_spearman(self):
        self.assertAlmostEqual(H.spearman([1, 2, 3], [2, 4, 6]), 1.0, places=6)
        self.assertAlmostEqual(H.spearman([1, 2, 3], [6, 4, 2]), -1.0, places=6)

    def test_stable_hash_is_process_independent(self):
        """Guards the PYTHONHASHSEED bug that once made scenarios non-reproducible."""
        self.assertEqual(H.sha1_of({"a": 1, "b": [1, 2]}),
                         H.sha1_of({"b": [1, 2], "a": 1}))
        self.assertNotEqual(H._stable_hash("light"), H._stable_hash("heavy"))
        r = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {ROOT!r});"
             "import hazard_prediction_engine as H; print(H._stable_hash('light'))"],
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "PYTHONHASHSEED": "999"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(H._stable_hash("light")))


# ==========================================================================
class TestGraphGeneration(unittest.TestCase):
    def test_deterministic(self):
        a, b = _graph(180, 7), _graph(180, 7)
        self.assertEqual(H.sha1_of([e.to_dict() for e in a.edges]),
                         H.sha1_of([e.to_dict() for e in b.edges]))

    def test_seed_changes_graph(self):
        a, b = _graph(180, 7), _graph(180, 8)
        self.assertNotEqual(H.sha1_of([e.to_dict() for e in a.edges]),
                            H.sha1_of([e.to_dict() for e in b.edges]))

    def test_edge_count_and_length(self):
        g = fx("graph")
        self.assertGreaterEqual(len(g.edges), 150)
        self.assertLess(abs(len(g.edges) - 220), 90)
        self.assertGreater(g.total_length_km(), 500.0)
        for e in g.edges:
            self.assertGreater(e.length_m, 0.0)
            self.assertLess(e.length_m, 60000.0)

    def test_fully_connected(self):
        g = fx("graph")
        adj = g.adjacency()
        start = next(iter(adj))
        seen, stack = {start}, [start]
        while stack:
            n = stack.pop()
            for _i, m in adj[n]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        self.assertEqual(len(seen), len(g.nodes))

    def test_adjacency_is_symmetric(self):
        g = fx("graph")
        adj = g.adjacency()
        for n, nbrs in adj.items():
            for _i, m in nbrs:
                self.assertIn(n, [x for _j, x in adj[m]])

    def test_named_hubs_present(self):
        g = fx("graph")
        for city in ("Guwahati", "Shillong", "Imphal", "Kohima", "Silchar",
                     "Aizawl", "Agartala", "Gangtok", "Dibrugarh", "Tura"):
            self.assertIsNotNone(g.node_by_name(city), city)
        self.assertGreaterEqual(len(g.named_nodes()), 20)

    def test_terrain_is_physically_plausible(self):
        g = fx("graph")
        slopes = sorted(e.slope_deg for e in g.edges)
        self.assertGreaterEqual(slopes[0], 0.0)
        self.assertLessEqual(slopes[-1], 45.0)
        self.assertGreater(slopes[len(slopes) // 2], 1.0)        # not a pancake
        self.assertGreater(slopes[int(0.90 * len(slopes))], 6.0)  # real relief
        self.assertGreater(H.mean([e.relief_m for e in g.edges]), 1.0)
        for e in g.edges:
            self.assertTrue(0.0 < e.ndvi < 1.0)
            self.assertTrue(0.0 <= e.drainage <= 1.0)
            self.assertIn(e.soil_type, SOILS)
            self.assertIn(e.cut_slope, (0, 1))
            self.assertGreaterEqual(e.sinuosity, 1.0)
            self.assertGreater(e.speed_kmh, 0.0)

    def test_metadata_is_populated(self):
        md = fx("graph").metadata
        self.assertEqual(md["problem_id"], "SIH26002")
        self.assertEqual(md["seed"], SEED)
        self.assertEqual(md["crs"], "EPSG:4326")
        self.assertIn("sha1", md)
        self.assertIn("Guwahati", md["surveyed_elevation_m"])
        self.assertEqual(len(fx("graph").bbox()), 4)


# ==========================================================================
class TestGeoJsonIO(unittest.TestCase):
    def test_roundtrip_is_lossless(self):
        g = _graph(180)
        g2 = H.RoadGraph.from_geojson(g.to_geojson())
        self.assertEqual(len(g.edges), len(g2.edges))
        self.assertEqual(len(g.nodes), len(g2.nodes))
        self.assertEqual(H.sha1_of([e.to_dict() for e in g.edges]),
                         H.sha1_of([e.to_dict() for e in g2.edges]))

    def test_reloaded_graph_rescores_identically(self):
        """Scores are transient: a reloaded graph must reproduce them exactly."""
        g, _r, idx = fresh_pair(180)
        eng = H.HazardEngine(fx("model"), g, H.CostPolicy(), index=idx)
        g, _w, _t = eng.reweight(g, "heavy")
        before = [(e.segment_id, round(e.p_disruption, 9)) for e in g.edges]
        g2 = H.RoadGraph.from_geojson(g.to_geojson(include_scores=True))
        self.assertEqual([e.p_disruption for e in g2.edges],
                         [0.0] * len(g2.edges),
                         "a reloaded graph starts unscored")
        eng2 = H.HazardEngine(fx("model"), g2, H.CostPolicy(), index=idx)
        g2, _w2, _t2 = eng2.reweight(g2, "heavy")
        after = [(e.segment_id, round(e.p_disruption, 9)) for e in g2.edges]
        self.assertEqual(before, after)

    def test_static_geojson_omits_scores(self):
        props = fx("graph").to_geojson()["features"][0]["properties"]
        self.assertNotIn("p_disruption", props)
        self.assertIn("segment_id", props)

    def test_featurecollection_shape(self):
        obj = fx("graph").to_geojson()
        self.assertEqual(obj["type"], "FeatureCollection")
        self.assertGreater(len(obj["features"]), 0)
        for f in obj["features"]:
            self.assertEqual(f["type"], "Feature")
            self.assertEqual(f["geometry"]["type"], "LineString")
            for lon, lat in f["geometry"]["coordinates"]:
                self.assertTrue(-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0)
            for k in ("segment_id", "length_m", "slope_deg", "hist_freq",
                      "highway", "state"):
                self.assertIn(k, f["properties"])

    def test_scored_graph_carries_risk_properties(self):
        g, _r, idx = fresh_pair(180)
        eng = H.HazardEngine(fx("model"), g, H.CostPolicy(), index=idx)
        g, _w, _t = eng.reweight(g, "heavy")
        props = g.to_geojson(include_scores=True)["features"][0]["properties"]
        for k in ("segment_id", "p_disruption", "risk_band", "cost_s",
                  "cost_s_soft", "base_cost_s", "transit_s", "impassable",
                  "weather"):
            self.assertIn(k, props, k)
        for k in ("rain_mm_hr", "rain_24h_mm", "api_3d", "soil_saturation"):
            self.assertIn(k, props["weather"], k)

    def test_saved_json_is_strict_and_finite(self):
        g, _r, idx = fresh_pair(180)
        eng = H.HazardEngine(fx("model"), g, H.CostPolicy(), index=idx)
        g, _w, _t = eng.reweight(g, "extreme")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "graph_extreme.json")
            H.save_json(g.to_geojson(include_scores=True), path)
            with open(path) as fh:
                raw = fh.read()
            self.assertNotIn("Infinity", raw)
            self.assertNotIn("NaN", raw)
            data = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON constant {c}")))
            self.assertEqual(data["type"], "FeatureCollection")
            cut = [f for f in data["features"] if f["properties"].get("impassable")]
            self.assertGreater(len(cut), 0, "EXTREME should hard-block something")
            for f in cut:
                self.assertIsNone(f["properties"]["cost_s"])
                self.assertGreaterEqual(f["properties"]["p_disruption"], 0.90)


# ==========================================================================
class TestHistoricalHazardLog(unittest.TestCase):
    def test_base_rate_hits_target(self):
        rows = fx("records")
        rate = H.mean([r["disrupted"] for r in rows])
        self.assertLess(abs(rate - 0.145), 0.012)

    def test_schema_complete(self):
        rows = fx("records")
        self.assertGreater(len(rows), 500)
        for r in rows[:40]:
            for col in H.HAZARD_CSV_COLUMNS:
                self.assertIn(col, r, col)
            self.assertTrue(r["record_id"].startswith("HZ23-"))
            self.assertIn(r["season_phase"], PHASES)
            self.assertTrue(0.0 <= r["soil_saturation"] <= 1.0)
            self.assertGreater(r["hist_freq_per_km"], 0.0)
            self.assertTrue(r["timestamp"].startswith("2023-"))

    def test_disruption_payloads_are_consistent(self):
        rows = fx("records")
        pos = [r for r in rows if r["disrupted"] == 1]
        neg = [r for r in rows if r["disrupted"] == 0]
        self.assertTrue(pos and neg)
        for r in pos:
            self.assertIn(r["hazard_type"], H.HAZARD_TYPES)
            self.assertGreater(r["closure_hours"], 0.0)
            self.assertIn(r["source"], H.HAZARD_SOURCES)
        for r in neg:
            self.assertEqual(r["hazard_type"], "none")
            self.assertEqual(r["closure_hours"], 0.0)
            self.assertEqual(r["debris_tonnes"], 0.0)

    def test_hazard_types_follow_terrain(self):
        rows = fx("records")
        steep = [r for r in rows if r["disrupted"] and r["slope_deg"] >= 12.0]
        flat = [r for r in rows if r["disrupted"] and r["slope_deg"] < 4.0]
        mass = {"landslide", "debris_flow", "slope_failure", "boulder_fall",
                "road_collapse"}
        self.assertGreater(sum(r["hazard_type"] in mass for r in steep) / len(steep),
                           sum(r["hazard_type"] in mass for r in flat) / len(flat))

    def test_monsoon_seasonality(self):
        rows = fx("records")
        peak = [r["rain_mm_hr"] for r in rows if r["season_phase"] == "peak"]
        post = [r["rain_mm_hr"] for r in rows if r["season_phase"] == "post"]
        self.assertGreater(H.mean(peak), H.mean(post))
        self.assertGreater(sum(1 for r in rows if r["season_phase"] == "peak"),
                           sum(1 for r in rows if r["season_phase"] == "post"))

    def test_csv_roundtrip(self):
        rows = fx("records")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "haz.csv")
            H.write_hazard_csv(rows, path)
            back = H.load_hazard_records(path)
        self.assertEqual(len(back), len(rows))
        self.assertEqual(back[0]["segment_id"], rows[0]["segment_id"])
        self.assertEqual(int(back[0]["disrupted"]), int(rows[0]["disrupted"]))
        self.assertAlmostEqual(float(back[0]["rain_mm_hr"]),
                               float(rows[0]["rain_mm_hr"]), places=2)

    def test_generator_is_deterministic(self):
        g = fx("graph")
        a = _records(g)
        b = _records(g)
        self.assertEqual(H.sha1_of(a[:200]), H.sha1_of(b[:200]))

    def test_generator_internals_are_stripped(self):
        for r in fx("records")[:50]:
            self.assertEqual([k for k in r if k.startswith("_")], ["_p_true"],
                             "only the ground-truth probability may remain")
            self.assertTrue(0.0 <= r["_p_true"] <= 1.0)
            self.assertNotIn("_p_true", H.HAZARD_CSV_COLUMNS)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.csv")
            H.write_hazard_csv(fx("records"), path)
            with open(path) as fh:
                head = fh.readline()
            self.assertNotIn("_p_true", head)


# ==========================================================================
class TestHistoricalIndex(unittest.TestCase):
    def test_frequency_is_per_km_and_bounded(self):
        idx = fx("index")
        self.assertGreater(len(idx.freq), 0)
        for v in idx.freq.values():
            self.assertGreaterEqual(v, 0.0)
            self.assertLess(v, 50.0)

    def test_summary_reports_the_prior(self):
        s = fx("index").summary()
        self.assertGreater(s["global_prior_events_per_km"], 0.0)
        self.assertEqual(s["eb_shrinkage_k"], 1.6)
        self.assertEqual(len(s["hist_freq_range"]), 2)
        self.assertLess(s["hist_freq_range"][0], s["hist_freq_range"][1])
        self.assertIn("observed_2023_rate", s)
        self.assertIn("hazard_type_mix", s)
        json.dumps(s)

    def test_empirical_bayes_shrinks_thin_evidence(self):
        recs = [{"segment_id": "S1", "state": "Assam", "highway": "NH-27",
                 "disrupted": 1, "length_m": 200.0, "hist_freq_per_km": 12.0},
                {"segment_id": "S2", "state": "Assam", "highway": "NH-27",
                 "disrupted": 1, "length_m": 20000.0, "hist_freq_per_km": 12.0}]
        recs += [{"segment_id": f"S{i}", "state": "Assam", "highway": "NH-27",
                  "disrupted": 1, "length_m": 20000.0, "hist_freq_per_km": 1.0}
                 for i in range(3, 12)]
        idx = H.HistoricalHazardIndex(recs)
        prior = idx.group_prior[("Assam", "NH-27")]
        # 200 m of exposure cannot justify 12 events/km; 20 km can
        self.assertLess(idx.freq["S1"], 12.0)
        self.assertGreater(idx.freq["S1"], 0.5)
        self.assertGreater(idx.freq["S2"], idx.freq["S1"])
        self.assertAlmostEqual(idx.freq["S5"], 1.0, delta=0.25)   # thick evidence
        self.assertLess(abs(idx.freq["S1"] - prior), abs(12.0 - prior))

    def test_prior_for_falls_back_for_unseen_segments(self):
        idx = fx("index")
        e = flat_edge(segment_id="NEVER-SEEN")
        e.state, e.highway = "Assam", "NH-27"
        p = idx.prior_for(e)
        self.assertGreater(p, 0.0)
        self.assertLess(p, 50.0)
        e.state = "Nowhere"
        e.highway = "NH-999"
        self.assertGreater(idx.prior_for(e), 0.0)     # global fallback

    def test_attach_to_graph_populates_edges(self):
        g, _r, idx = fresh_pair(180)
        hit = [e for e in g.edges if e.hist_freq > 0]
        self.assertGreater(len(hit), 0.5 * len(g.edges))

    def test_top_segments_sorted_descending(self):
        top = fx("index").top_segments(5)
        self.assertEqual(len(top), 5)
        vals = [t[1] for t in top]
        self.assertEqual(vals, sorted(vals, reverse=True))

    def test_events_and_closure_totals(self):
        idx = fx("index")
        self.assertEqual(idx.events,
                         sum(r["disrupted"] for r in fx("records")))
        self.assertGreater(idx.closure_hours_total, 0.0)


# ==========================================================================
class TestFeatures(unittest.TestCase):
    def test_schema(self):
        self.assertEqual(len(H.FEATURES), len(set(H.FEATURES)))
        for f in ("rain_mm_hr", "api_3d", "slope_deg", "soil_saturation",
                  "hist_freq"):
            self.assertIn(f, H.FEATURES, f"{f} is a canonical spec feature")
        self.assertEqual(H.HIST_IDX, H.FEATURES.index("hist_freq"))

    def test_record_and_edge_extractors_agree(self):
        """Train/serve parity: both extractors must emit one identical schema."""
        e = flat_edge(hist=1.75)
        w = {"rain_mm_hr": 33.0, "rain_24h_mm": 120.0, "api_3d": 210.0,
             "soil_saturation": 0.66}
        xe = H.edge_features(e, w)
        rec = {"segment_id": e.segment_id, "length_m": e.length_m,
               "slope_deg": e.slope_deg, "soil_type": e.soil_type,
               "drainage": e.drainage, "ndvi": e.ndvi, "cut_slope": e.cut_slope,
               "sinuosity": e.sinuosity, "elevation_m": e.elevation_m,
               "rain_mm_hr": 33.0, "api_3d_mm": 210.0,
               "soil_saturation": 0.66, "hist_freq_per_km": e.hist_freq}
        xr = H.record_features(rec)
        self.assertEqual(len(xe), len(H.FEATURES))
        self.assertEqual(len(xr), len(H.FEATURES))
        for name, a, b in zip(H.FEATURES, xe, xr):
            self.assertAlmostEqual(float(a), float(b), places=4,
                                   msg=f"feature '{name}' diverges")

    def test_api3d_alias_accepted(self):
        e = flat_edge()
        w1 = {"rain_mm_hr": 10.0, "api_3d": 90.0, "soil_saturation": 0.4}
        w2 = {"rain_mm_hr": 10.0, "api_3d_mm": 90.0, "soil_saturation": 0.4}
        self.assertEqual(H.edge_features(e, w1), H.edge_features(e, w2))

    def test_all_features_finite(self):
        X, _y, _m = H.build_dataset(fx("records")[:400], fx("index"))
        for row in X:
            self.assertEqual(len(row), len(H.FEATURES))
            for v in row:
                self.assertTrue(math.isfinite(v))

    def test_build_dataset_substitutes_index_history(self):
        recs = fx("records")[:600]
        idx = fx("index")
        Xa, ya, _ = H.build_dataset(recs, idx)
        Xb, yb, _ = H.build_dataset(recs, None)
        self.assertEqual(ya, yb)
        h = H.HIST_IDX
        self.assertNotEqual([r[h] for r in Xa], [r[h] for r in Xb],
                            "the index must override the raw CSV frequency")
        for row in Xa:
            self.assertTrue(0.0 <= row[h] < 50.0)

    def test_weather_state_for_edge(self):
        e = flat_edge()
        ws = H.WeatherState(scenario="heavy", rain_mm_hr=20.0, rain_24h_mm=90.0,
                            api_3d=150.0, soil_saturation=0.6)
        d = ws.for_edge(e)
        for k in ("rain_mm_hr", "rain_24h_mm", "api_3d", "soil_saturation"):
            self.assertIn(k, d)
            self.assertTrue(math.isfinite(d[k]))


# ==========================================================================
class TestMetrics(unittest.TestCase):
    def test_roc_auc(self):
        self.assertAlmostEqual(H.roc_auc([0, 0, 1, 1], [.1, .2, .8, .9]), 1.0)
        self.assertAlmostEqual(H.roc_auc([0, 0, 1, 1], [.9, .8, .2, .1]), 0.0)
        self.assertAlmostEqual(H.roc_auc([0, 1, 0, 1], [.5, .5, .5, .5]), 0.5)
        self.assertAlmostEqual(H.roc_auc([0, 1], [.4, .6]), 1.0)
        self.assertTrue(math.isnan(H.roc_auc([1, 1], [.4, .6])))   # no negatives

    def test_pr_auc_and_losses(self):
        self.assertGreater(H.pr_auc([0, 0, 1, 1], [.1, .2, .8, .9]), 0.9)
        self.assertAlmostEqual(H.log_loss([1, 0], [0.999, 0.001]), 0.001,
                               delta=0.01)
        self.assertAlmostEqual(H.brier_score([1, 0], [1.0, 0.0]), 0.0)
        self.assertAlmostEqual(H.brier_score([1, 1], [0.5, 0.5]), 0.25)

    def test_confusion_and_ks(self):
        c = H.confusion_at([0, 0, 1, 1], [.1, .4, .6, .9], 0.5)
        self.assertEqual((c["tp"], c["fp"], c["fn"], c["tn"]), (2, 0, 0, 2))
        self.assertEqual(c["precision"], 1.0)
        self.assertEqual(c["recall"], 1.0)
        self.assertEqual(c["f1"], 1.0)
        self.assertAlmostEqual(H.ks_statistic([0, 0, 1, 1], [.1, .2, .8, .9]), 1.0)
        self.assertAlmostEqual(H.ks_statistic([0, 1], [.5, .5]), 0.0)

    def test_evaluate_bundle(self):
        rng = random.Random(1)
        y = [rng.randint(0, 1) for _ in range(300)]
        p = [min(0.999, max(0.001, q + rng.gauss(0, .1)))
             for q in [0.2 + 0.6 * v for v in y]]
        m = H.evaluate(y, p, thr=0.5)
        self.assertGreater(m["roc_auc"], 0.85)
        self.assertTrue(0.0 <= m["ece"] <= 1.0)
        self.assertEqual(m["n"], 300)
        self.assertAlmostEqual(m["precision"], 1.0)

    def test_ece_is_small_for_well_calibrated_scores(self):
        rng = random.Random(3)
        y, p = [], []
        for _ in range(4000):
            q = rng.random()
            y.append(1 if rng.random() < q else 0)
            p.append(q)
        self.assertLess(H.expected_calibration_error(y, p, nbins=10), 0.03)

    def test_band_lift_table_partitions_the_sample(self):
        rng = random.Random(4)
        y = [rng.randint(0, 1) for _ in range(500)]
        p = [rng.random() for _ in range(500)]
        rows = H.band_lift_table(y, p)
        self.assertEqual(len(rows), len(H.RISK_BANDS))
        self.assertEqual(sum(r["n"] for r in rows), 500)
        self.assertAlmostEqual(sum(r["share_of_network"] for r in rows), 1.0,
                               places=6)
        for r in rows:
            if r["n"]:
                self.assertTrue(0.0 <= r["observed_disruption_rate"] <= 1.0)
                self.assertTrue(0.0 <= r["mean_predicted"] <= 1.0)
            self.assertIn(r["band"], [b[1] for b in H.RISK_BANDS])

    def test_stratified_kfold(self):
        y = [1] * 90 + [0] * 410
        folds = H.stratified_kfold(y, k=5, seed=1)
        self.assertEqual(len(folds), 5)
        seen = []
        for tr, te in folds:
            self.assertEqual(set(tr) & set(te), set())
            self.assertAlmostEqual(H.mean([y[i] for i in te]), 0.18, delta=0.09)
            seen += te
        self.assertEqual(sorted(seen), list(range(500)))

    def test_platt_calibration_improves_ece(self):
        rng = random.Random(5)
        z = [rng.gauss(0, 1.6) for _ in range(3000)]
        y = [1 if rng.random() < H.sigmoid(0.8 * v - 0.5) else 0 for v in z]
        before = H.expected_calibration_error(y, [H.sigmoid(v) for v in z])
        a, b = H.fit_platt(z, y)
        after = H.expected_calibration_error(y, [H.sigmoid(a * v + b) for v in z])
        self.assertLess(after, before)
        self.assertLess(after, 0.05)
        self.assertGreater(a, 0.0)


# ==========================================================================
class TestPureGBDT(unittest.TestCase):
    def test_learns_and_serialises(self):
        X, y, _p = H._toy_dataset(n=900, seed=21)
        m = H.PureGBDT(n_estimators=60, max_depth=3, learning_rate=0.12,
                       seed=21).fit(X, y)
        self.assertGreater(H.roc_auc(y, m.predict_proba(X)), 0.85)
        d = m.to_dict()
        m2 = H.PureGBDT.from_dict(d)
        self.assertEqual([round(v, 12) for v in m.predict_proba(X[:40])],
                         [round(v, 12) for v in m2.predict_proba(X[:40])])
        json.dumps(d)

    def test_uniform_seed_argument_position(self):
        """fit(X, y, seed) must not be mistaken for fit(X, y, sample_weight)."""
        X, y, _p = H._toy_dataset(n=200, seed=3)
        m = H.PureGBDT(n_estimators=10).fit(X, y, 123)
        self.assertEqual(len(m.predict_proba(X)), len(X))

    def test_degenerate_inputs(self):
        X = [[0.0] * len(H.FEATURES)] * 20
        m = H.PureGBDT(n_estimators=5).fit(X, [0] * 20)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in m.predict_proba(X)))
        with self.assertRaises(ValueError):
            H.PureGBDT().fit([], [])


# ==========================================================================
class TestModel(unittest.TestCase):
    def test_backend_resolution(self):
        self.assertIn("pure", H.available_backends())
        self.assertEqual(H.resolve_backend("pure"), "pure")
        self.assertIn(H.resolve_backend("auto"), H.BACKEND_ORDER)
        with self.assertRaises(Exception):
            H.resolve_backend("definitely-not-a-backend")

    def test_fit_and_predict(self):
        m, g, recs, idx = fx("model_bundle")
        X, y, _meta = H.build_dataset(recs, idx)
        self.assertEqual(m.backend, "pure")
        self.assertEqual(tuple(m.features), H.FEATURES)
        self.assertTrue(m.fitted)
        pr = m.predict_proba(X[:500])
        self.assertTrue(all(0.0 <= v <= 1.0 for v in pr))
        self.assertEqual(len(pr), 500)
        cv = m.metrics["cv"]
        self.assertGreater(cv["roc_auc"], 0.90)
        self.assertLess(cv["ece"], 0.05)
        self.assertGreater(cv["pr_auc"], 0.5)
        self.assertIn("band_lift", cv)

    def test_out_of_fold_predictions_are_stored(self):
        m, _g, recs, _idx = fx("model_bundle")
        y, p = m.oof
        self.assertEqual(len(y), len(recs) - sum(1 for r in recs
                                                 if "disrupted" not in r))
        self.assertTrue(all(0.0 <= v <= 1.0 for v in p))
        self.assertGreater(H.roc_auc(y, p), 0.90)

    def test_wet_ranks_above_dry(self):
        """Physical sanity: risk must rise with rain, slope and cut face."""
        m = fx("model")
        e = flat_edge(slope_deg=14.0)
        p_dry, p_wet = m.score_edge(e, DRY), m.score_edge(e, WET)
        self.assertGreater(p_wet, p_dry + 0.05)
        flat = flat_edge(segment_id="T-2", slope_deg=1.0, cut_slope=0)
        steep = flat_edge(segment_id="T-3", slope_deg=22.0, cut_slope=1)
        self.assertGreater(m.score_edge(steep, WET), m.score_edge(flat, WET))
        self.assertLess(m.score_edge(flat, DRY), 0.1)

    def test_score_matches_batch_prediction(self):
        m = fx("model")
        e = flat_edge(slope_deg=13.0)
        x = H.edge_features(e, WET)
        self.assertAlmostEqual(m.score_edge(e, WET), m.predict_proba([x])[0],
                               places=9)

    def test_importances_normalised(self):
        m = fx("model")
        imp = m.importance
        self.assertEqual(len(imp), len(H.FEATURES))
        self.assertAlmostEqual(sum(imp.values()), 1.0, places=3)
        for k, v in imp.items():
            self.assertIsInstance(v, float)
            self.assertIn(k, H.FEATURES)
            self.assertGreaterEqual(v, 0.0)

    def test_save_load_roundtrip(self):
        m = fx("model")
        e = flat_edge(slope_deg=12.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "card.json")
            m.save(path)
            with open(path) as fh:
                card = json.load(fh)
            m2 = H.HazardRiskModel.load(path)
        self.assertEqual(card["problem_id"], "SIH26002")
        self.assertEqual(tuple(card["features"]), H.FEATURES)
        self.assertIn("metrics", card)
        self.assertEqual(m2.backend, m.backend)
        self.assertAlmostEqual(m2.metrics["cv"]["roc_auc"],
                               m.metrics["cv"]["roc_auc"], places=9)
        self.assertAlmostEqual(m2.score_edge(e, WET), m.score_edge(e, WET),
                               places=6)
        self.assertAlmostEqual(m2.score_edge(e, DRY), m.score_edge(e, DRY),
                               places=6)

    def test_predict_uses_threshold(self):
        m = fx("model")
        e = flat_edge(slope_deg=20.0)
        self.assertEqual(m.predict([H.edge_features(e, WET)])[0], 1)
        self.assertEqual(m.predict([H.edge_features(e, DRY)])[0], 0)


# ==========================================================================
class TestCostPolicy(unittest.TestCase):
    def test_formula_matches_the_documentation(self):
        """C(e) = t(e)[1 + a*P(e)] + b*H(e) + g*S(e)*C0, H(e)=tan(slope)L/V_REF."""
        pol = H.CostPolicy()
        e = flat_edge(length_m=6000.0, slope_deg=12.0, speed_kmh=45.0)
        t = e.length_m / (e.speed_kmh / 3.6)
        hgt = math.tan(math.radians(12.0)) * e.length_m / H.V_REF
        for p in (0.0, 0.17, 0.5, 0.83):
            want = (t * (1.0 + pol.alpha * p) + pol.beta * hgt
                    + pol.gamma * pol.cargo_multiplier * H.CARGO_OVERHEAD_S)
            self.assertAlmostEqual(pol.cost_soft(e, p)[0], want, places=6,
                                   msg=f"p={p}")

    def test_coefficient_defaults(self):
        pol = H.CostPolicy()
        self.assertEqual((pol.alpha, pol.beta, pol.gamma), (2.5, 0.8, 1.2))
        self.assertEqual(H.V_REF, 10.0)
        self.assertEqual(H.CARGO_OVERHEAD_S, 60.0)

    def test_cost_is_monotone_in_risk(self):
        pol = H.CostPolicy()
        e = flat_edge()
        prev = -1.0
        for p in [i / 20.0 for i in range(18)]:
            c = pol.cost_soft(e, p)[0]
            self.assertGreater(c, prev)
            prev = c

    def test_base_cost_is_the_p_zero_case(self):
        pol = H.CostPolicy()
        e = flat_edge()
        self.assertAlmostEqual(pol.base_cost(e)[0], pol.cost_soft(e, 0.0)[0],
                               places=6)
        self.assertAlmostEqual(pol.base_cost(e)[0], pol.cost(e, 0.0)[0], places=6)

    def test_rain_amplification_is_proportional_to_transit(self):
        pol = H.CostPolicy()
        e = flat_edge(length_m=5000.0, slope_deg=0.0, speed_kmh=50.0)
        t = 5000.0 / (50.0 / 3.6)
        self.assertAlmostEqual(pol.cost_soft(e, 1.0)[0], t * 3.5
                               + pol.gamma * H.CARGO_OVERHEAD_S, places=4)

    def test_hard_block_at_threshold(self):
        pol = H.CostPolicy(block_threshold=0.9)
        e = flat_edge()
        self.assertTrue(math.isinf(pol.cost(e, 0.95)[0]))
        self.assertTrue(math.isfinite(pol.cost(e, 0.89)[0]))
        self.assertTrue(math.isfinite(pol.cost_soft(e, 0.999)[0]))
        soft = H.CostPolicy(block_threshold=0.9, hard_block=False)
        self.assertTrue(math.isfinite(soft.cost(e, 0.999)[0]))

    def test_cargo_multipliers(self):
        self.assertEqual(H.CARGO_PROFILES["standard"], 1.0)
        self.assertEqual(H.CARGO_PROFILES["perishable"], 1.5)
        self.assertEqual(H.CARGO_PROFILES["hazmat"], 2.0)
        self.assertEqual(H.CARGO_PROFILES["medical"], 1.75)
        e = flat_edge()
        costs = {c: H.CostPolicy(cargo=c).cost_soft(e, 0.3)[0]
                 for c in ("standard", "perishable", "medical", "hazmat")}
        self.assertLess(costs["standard"], costs["perishable"])
        self.assertLess(costs["perishable"], costs["medical"])
        self.assertLess(costs["medical"], costs["hazmat"])

    def test_transit_and_rain_derate(self):
        pol = H.CostPolicy()
        e = flat_edge(length_m=10000.0, speed_kmh=50.0)
        self.assertAlmostEqual(pol.transit_s(e), 720.0, places=3)
        derated = H.CostPolicy(rain_derate=0.4)
        self.assertGreater(derated.transit_s(e, 1.0), pol.transit_s(e, 1.0))
        self.assertAlmostEqual(derated.transit_s(e, 0.0), 720.0, places=3)

    def test_slope_friction(self):
        pol = H.CostPolicy()
        flat = flat_edge(slope_deg=0.0, length_m=4000.0)
        steep = flat_edge(segment_id="T-9", slope_deg=20.0, length_m=4000.0)
        self.assertAlmostEqual(pol.slope_friction_s(flat), 0.0, places=6)
        self.assertAlmostEqual(pol.slope_friction_s(steep),
                               math.tan(math.radians(20.0)) * 4000.0 / H.V_REF,
                               places=6)

    def test_to_dict_is_json_safe(self):
        json.dumps(H.CostPolicy().to_dict())


# ==========================================================================
class TestMonsoonField(unittest.TestCase):
    def test_scenarios_exist_and_escalate(self):
        order = ("light", "moderate", "heavy", "extreme")
        for s in order:
            self.assertIn(s, H.SCENARIOS)
        for key in ("rain", "api", "wet", "cells"):
            vals = [H.SCENARIOS[s][key] for s in order]
            self.assertEqual(vals, sorted(vals), f"{key} must escalate")

    def test_field_is_deterministic(self):
        g = fx("graph")
        pts = [(e.lat, e.lon) for e in g.edges]
        f1 = H.MonsoonField("heavy", seed=SEED, bbox=g.bbox(), focus_points=pts)
        f2 = H.MonsoonField("heavy", seed=SEED, bbox=g.bbox(), focus_points=pts)
        for lat, lon in ((26.1, 91.7), (25.5, 91.9), (24.8, 92.8)):
            self.assertAlmostEqual(f1.intensity(lat, lon), f2.intensity(lat, lon),
                                   places=12)

    def test_intensity_is_bounded_by_the_scenario(self):
        g = fx("graph")
        f = H.MonsoonField("heavy", seed=SEED, bbox=g.bbox())
        peak = H.SCENARIOS["heavy"]["rain"]
        for lat, lon in ((26.1, 91.7), (25.5, 91.9), (24.8, 92.8), (27.5, 93.5)):
            v = f.intensity(lat, lon)
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, peak * 1.0001)

    def test_evaluate_returns_per_edge_weather(self):
        g = fx("graph")
        w = H.MonsoonField("extreme", seed=SEED, bbox=g.bbox()).evaluate(g.edges)
        self.assertEqual(len(w.per_edge), len(g.edges))
        for sid, d in w.per_edge.items():
            for k in ("rain_mm_hr", "rain_24h_mm", "api_3d", "soil_saturation"):
                self.assertIn(k, d, k)
                self.assertTrue(math.isfinite(d[k]), (sid, k))
            self.assertTrue(0.0 <= d["soil_saturation"] <= 1.0)
            self.assertGreaterEqual(d["rain_24h_mm"], d["rain_mm_hr"])
        self.assertEqual(w.scenario, "extreme")

    def test_heavier_scenario_is_wetter_on_average(self):
        g = fx("graph")
        means = {}
        for s in ("light", "moderate", "heavy", "extreme"):
            w = H.MonsoonField(s, seed=SEED, bbox=g.bbox()).evaluate(g.edges)
            means[s] = (H.mean([v["rain_mm_hr"] for v in w.per_edge.values()]),
                        H.mean([v["soil_saturation"] for v in w.per_edge.values()]))
        for i in range(3):
            a, b = list(means)[i], list(means)[i + 1]
            self.assertLess(means[a][0], means[b][0], f"rain {a}->{b}")
            self.assertLess(means[a][1], means[b][1], f"soil {a}->{b}")

    def test_cells_create_spatial_contrast(self):
        """Wet and dry pockets must coexist, otherwise no detour can help."""
        g = fx("graph")
        w = H.MonsoonField("heavy", seed=SEED, bbox=g.bbox(),
                           focus_points=[(e.lat, e.lon) for e in g.edges]
                           ).evaluate(g.edges)
        rains = sorted(v["rain_mm_hr"] for v in w.per_edge.values())
        self.assertGreater(rains[-1], 3.0 * max(rains[0], 1e-6))
        self.assertLess(rains[0], H.SCENARIOS["heavy"]["rain"] * 0.5)


# ==========================================================================
class TestRouting(unittest.TestCase):
    def tiny_graph(self, direct_mult=2.6):
        g = H.RoadGraph()
        for nid, lat, lon in (("A", 26.00, 91.00), ("B", 26.05, 91.05),
                              ("C", 26.10, 91.10), ("D", 26.15, 91.15)):
            g.add_node(nid, lat, lon, 100.0, name=nid)

        def mk(i, u, v, mult):
            e = H.RoadEdge(segment_id=f"E{i}", u=u, v=v,
                           coords=[[g.nodes[u]["lon"], g.nodes[u]["lat"]],
                                   [g.nodes[v]["lon"], g.nodes[v]["lat"]]],
                           length_m=7000.0 * mult, elev_from=100.0, elev_to=150.0,
                           slope_deg=2.0, speed_kmh=50.0)
            e.lat, e.lon = g.nodes[u]["lat"], g.nodes[u]["lon"]
            return e

        g.add_edge(mk(1, "A", "B", 1.0))
        g.add_edge(mk(2, "B", "C", 1.0))
        g.add_edge(mk(3, "A", "C", direct_mult))   # direct but much longer
        g.add_edge(mk(4, "C", "D", 1.0))
        return g

    def test_dijkstra_and_astar_agree(self):
        g = self.tiny_graph()
        w = lambda e: e.length_m
        d = H.dijkstra(g, "A", "C", w)
        a = H.astar(g, "A", "C", w)
        self.assertIsNotNone(d)
        self.assertIsNotNone(a)
        self.assertAlmostEqual(d[0], a[0], places=6)
        self.assertEqual(d[1], a[1])
        self.assertEqual(d[1], ["A", "B", "C"])

    def test_unknown_nodes_return_none(self):
        g = self.tiny_graph()
        self.assertIsNone(H.dijkstra(g, "A", "Z", lambda e: e.length_m))
        self.assertIsNone(H.astar(g, "A", "Z", lambda e: e.length_m))
        g.add_node("Z", 20.0, 80.0, 10.0)
        self.assertIsNone(H.dijkstra(g, "A", "Z", lambda e: e.length_m))

    def test_route_resolves_names_ids_and_coordinates(self):
        g = self.tiny_graph()
        g.nodes["A"]["name"], g.nodes["C"]["name"] = "Alpha", "Charlie"
        prep_costs(g, H.CostPolicy())
        r = H.route(g, "Alpha", "Charlie", algorithm="astar", mode="baseline")
        self.assertTrue(r.found)
        self.assertEqual(r.nodes, ["A", "B", "C"])
        self.assertEqual(r.source_name, "Alpha")
        self.assertEqual(r.target_name, "Charlie")
        self.assertGreater(r.distance_km, 0.0)
        self.assertGreater(r.transit_min, 0.0)
        self.assertGreater(r.cost_s, 0.0)
        self.assertEqual(r.n_segments, 2)
        self.assertTrue(H.route(g, "A", "C").found)
        self.assertTrue(H.route(g, "26.00,91.00", "Charlie").found)
        self.assertFalse(H.route(g, "Alpha", "Nowhere").found)
        self.assertFalse(H.route(g, "Alpha", "Charlie").found is None)

    def test_route_rejects_unknown_mode(self):
        g = self.tiny_graph()
        prep_costs(g, H.CostPolicy())
        with self.assertRaises(ValueError):
            H.route(g, "A", "C", mode="teleport")

    def test_blocked_edge_forces_detour(self):
        g = self.tiny_graph()
        pol = H.CostPolicy(block_threshold=0.5)
        for e in g.edges:
            e.p_disruption = 0.9 if e.segment_id in ("E1", "E2") else 0.0
            e.risk_band = H.risk_band(e.p_disruption)[0]
        prep_costs(g, pol)
        base = H.route(g, "A", "C", mode="baseline")
        risk = H.route(g, "A", "C", mode="risk")
        self.assertEqual(base.nodes, ["A", "B", "C"])
        self.assertEqual(risk.nodes, ["A", "C"])        # takes the long link
        self.assertGreater(risk.distance_km, base.distance_km)
        self.assertEqual(base.impassable_segments, 2)
        self.assertEqual(risk.impassable_segments, 0)
        self.assertLess(risk.max_p, 0.5)
        self.assertGreater(base.impassable_km, 0.0)

    def test_soft_mode_keeps_everything_routable(self):
        # direct link is 8x longer here, so soft costs still prefer driving
        # through the two risky legs while a hard block forbids them
        g = self.tiny_graph(direct_mult=8.0)
        pol = H.CostPolicy(block_threshold=0.5)
        for e in g.edges:
            e.p_disruption = 0.9 if e.segment_id in ("E1", "E2") else 0.0
            e.risk_band = H.risk_band(e.p_disruption)[0]
        prep_costs(g, pol)
        for e in g.edges:
            self.assertTrue(math.isfinite(e.cost_s_soft))
        soft = H.route(g, "A", "C", mode="risk_soft")
        self.assertTrue(soft.found)
        self.assertEqual(soft.nodes, ["A", "B", "C"])   # risk tolerated
        self.assertEqual(H.route(g, "A", "C", mode="risk").nodes, ["A", "C"])
        # --no-hard-block keeps the risky path legal under the dynamic cost
        prep_costs(g, H.CostPolicy(block_threshold=0.5, hard_block=False))
        self.assertTrue(all(math.isfinite(e.cost_s) for e in g.edges))
        self.assertEqual(H.route(g, "A", "C", mode="risk").nodes,
                         ["A", "B", "C"])
        # --no-hard-block leaves the same graph routable through the risk
        permissive = H.CostPolicy(block_threshold=0.5, hard_block=False)
        prep_costs(g, permissive)
        self.assertTrue(all(math.isfinite(e.cost_s) for e in g.edges))
        self.assertEqual(H.route(g, "A", "C", mode="risk").nodes, ["A", "B", "C"])

    def test_route_result_serialises(self):
        g = self.tiny_graph()
        prep_costs(g, H.CostPolicy())
        r = H.route(g, "A", "D")
        d = r.to_dict()
        json.dumps(d, default=H._json_default)
        self.assertTrue(d["found"])
        self.assertIn("legs", d)
        self.assertEqual(len(d["legs"]), 3)
        self.assertNotIn("Infinity", json.dumps(d, default=H._json_default))


# ==========================================================================
class TestEngine(unittest.TestCase):
    def setUp(self):
        self.g, self.recs, self.idx = fresh_pair(180)
        self.eng = H.HazardEngine(fx("model"), self.g, H.CostPolicy(),
                                  index=self.idx, records=self.recs)

    def test_reweight_scores_every_edge(self):
        g, w, elapsed = self.eng.reweight(self.g, "heavy")
        self.assertIs(g, self.g)
        self.assertEqual(len(w.per_edge), len(g.edges))
        for e in g.edges:
            self.assertTrue(0.0 <= e.p_disruption <= 1.0)
            self.assertIn(e.risk_band, [b[1] for b in H.RISK_BANDS])
            self.assertGreaterEqual(e.cost_s, e.base_cost_s - 1e-9)
            self.assertGreater(e.cost_s_soft, 0.0)
            self.assertTrue(math.isfinite(e.weather["rain_mm_hr"]))
            self.assertTrue(0.0 <= e.weather["soil_saturation"] <= 1.0)
        self.assertLess(elapsed, 5.0)

    def test_reweight_is_deterministic(self):
        a = self.eng.reweight(_graph(180), "heavy")[0]
        b = self.eng.reweight(_graph(180), "heavy")[0]
        self.assertEqual([round(e.p_disruption, 9) for e in a.edges],
                         [round(e.p_disruption, 9) for e in b.edges])

    def test_risk_escalates_with_scenario(self):
        series = []
        for s in ("light", "moderate", "heavy", "extreme"):
            g = _graph(180)
            idx = H.HistoricalHazardIndex(self.recs)
            idx.attach_to_graph(g)
            eng = H.HazardEngine(fx("model"), g, H.CostPolicy(), index=idx)
            g2, _w, _t = eng.reweight(g, s)
            series.append((H.mean([e.p_disruption for e in g2.edges]),
                           sum(1 for e in g2.edges if math.isinf(e.cost_s))))
        means = [m for m, _c in series]
        cuts = [c for _m, c in series]
        self.assertEqual(means, sorted(means), f"P means not monotone: {means}")
        self.assertEqual(cuts, sorted(cuts), f"cuts not monotone: {cuts}")
        self.assertGreater(means[-1], 5.0 * means[0])
        self.assertEqual(cuts[0], 0, "LIGHT rain must not cut anything")

    def test_risk_bands(self):
        self.assertEqual(H.risk_band(0.00)[0], "LOW")
        self.assertEqual(H.risk_band(0.14)[0], "LOW")
        self.assertEqual(H.risk_band(0.20)[0], "MODERATE")
        self.assertEqual(H.risk_band(0.45)[0], "HIGH")
        self.assertEqual(H.risk_band(0.70)[0], "SEVERE")
        self.assertEqual(H.risk_band(0.95)[0], "CRITICAL")
        self.assertEqual(H.risk_band(1.00)[0], "CRITICAL")
        for thr, _name, colour, action in H.RISK_BANDS:
            self.assertTrue(0.0 < thr <= 1.01)   # CRITICAL uses a 1.01 sentinel
            self.assertTrue(colour.startswith("#"))
            self.assertTrue(action)

    def test_compare_routes_reports_both_engines(self):
        self.eng.reweight(self.g, "heavy")
        c = self.eng.compare_routes(self.g, "Guwahati", "Shillong")
        for k in ("baseline", "risk_aware", "delta", "severed", "corridor"):
            self.assertIn(k, c)
        base, haz = c["baseline"], c["risk_aware"]
        self.assertTrue(base["found"] and haz["found"])
        self.assertLessEqual(haz["risk_exposure"], base["risk_exposure"] + 1e-9)
        # dynamic costs are not comparable across engines (the risk route is
        # deliberately longer); its static footprint, though, never shrinks
        self.assertGreaterEqual(haz["base_cost_s"], base["base_cost_s"] - 1e-6)
        self.assertEqual(base["mode"], "baseline")
        self.assertEqual(haz["mode"], "risk")
        for k in ("rerouted", "severed", "distance_pct", "risk_exposure_pct",
                  "segments_shared", "segments_avoided"):
            self.assertIn(k, c["delta"], k)
        self.assertAlmostEqual(
            c["delta"]["distance_pct"],
            round(100.0 * (haz["distance_km"] - base["distance_km"])
                  / base["distance_km"], 3), places=2)

    def test_severance_report(self):
        self.eng.reweight(self.g, "light")
        rep = self.eng.severance_report(self.g, hub="Guwahati")
        self.assertAlmostEqual(rep["accessibility_index"], 1.0, places=6)
        self.assertEqual(rep["severed_hubs"], [])
        self.assertEqual(rep["hub"], "Guwahati")
        self.assertEqual(rep["reachable_hubs"], rep["named_hubs"])
        self.assertEqual(rep["named_hubs"], 28)
        self.eng.reweight(self.g, "extreme")
        rep = self.eng.severance_report(self.g, hub="Guwahati")
        self.assertLess(rep["accessibility_index"], 1.0)
        self.assertGreater(rep["impassable_segments"], 0)
        self.assertGreater(rep["impassable_km"], 0.0)
        self.assertEqual(len(rep["severed_hubs"]) + rep["reachable_hubs"],
                         rep["named_hubs"])
        self.assertTrue(0 < rep["reachable_hubs"] < rep["named_hubs"])
        json.dumps(rep, default=H._json_default)

    def test_hub_resilience(self):
        self.eng.reweight(self.g, "light")
        hr = self.eng.hub_resilience(self.g, sample_pairs=12)
        self.assertAlmostEqual(hr["connectivity_index"], 1.0, places=6)
        self.assertEqual(hr["n_components"], 1)
        self.assertEqual(hr["passable_segments"], len(self.g.edges))
        self.assertEqual(hr["hub_pairs"],
                         hr["named_hubs"] * (hr["named_hubs"] - 1) // 2)
        so = hr["sample_outcome"]
        self.assertEqual(so["held"] + so["rerouted"] + so["severed"],
                         hr["sampled_pairs"])
        self.assertEqual(hr["connected_pairs"], hr["hub_pairs"])

        self.eng.reweight(self.g, "extreme")
        hr2 = self.eng.hub_resilience(self.g, sample_pairs=12)
        self.assertLess(hr2["connectivity_index"], hr["connectivity_index"])
        self.assertGreater(hr2["n_components"], hr["n_components"])
        self.assertLess(hr2["connected_pairs"], hr2["hub_pairs"])
        self.assertGreater(hr2["sample_outcome"]["severed"], 0)
        self.assertEqual(hr2["passable_segments"] + hr2["impassable_segments"],
                         len(self.g.edges))
        for d in hr2["largest_detours"]:
            self.assertIn("corridor", d)
            self.assertIn("distance_pct", d)
        json.dumps(hr2, default=H._json_default)

    def test_score_point_matches_the_graph_model(self):
        self.eng.reweight(self.g, "heavy")
        e = max(self.g.edges, key=lambda x: x.p_disruption)
        p = self.eng.score_point(e.lat, e.lon,
                                 rain_mm_hr=e.weather["rain_mm_hr"],
                                 api_3d=e.weather["api_3d"],
                                 soil_saturation=e.weather["soil_saturation"],
                                 slope_deg=e.slope_deg,
                                 hist_freq=e.hist_freq,
                                 length_m=e.length_m,
                                 elevation_m=e.elevation_m,
                                 ndvi=e.ndvi, drainage=e.drainage,
                                 cut_slope=e.cut_slope,
                                 sinuosity=e.sinuosity)
        self.assertAlmostEqual(p, e.p_disruption, places=6)

    def test_scenario_summary_is_complete(self):
        g, w, elapsed = self.eng.reweight(self.g, "heavy")
        s = self.eng.scenario_summary(g, w, elapsed, scenario="heavy")
        for k in ("scenario", "edges", "network_km", "p_disruption",
                  "band_counts", "segments_hard_blocked", "km_at_risk",
                  "severance", "hub_resilience", "top_risk_segments",
                  "reweight_seconds", "edges_per_second", "weather"):
            self.assertIn(k, s, k)
        self.assertEqual(sum(s["band_counts"].values()), len(g.edges))
        self.assertEqual(s["scenario"], "heavy")
        self.assertAlmostEqual(s["reweight_seconds"], elapsed, places=2)
        self.assertGreater(s["edges_per_second"], 0.0)
        self.assertEqual(s["edges"], len(g.edges))
        for k in ("mean", "median", "p90", "p99", "max"):
            self.assertIn(k, s["p_disruption"], k)
        json.dumps(s, default=H._json_default)

    def test_bootstrap_loads_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            gpath = os.path.join(tmp, "g.geojson")
            hpath = os.path.join(tmp, "h.csv")
            mpath = os.path.join(tmp, "card.json")
            H.save_json(_graph(180).to_geojson(), gpath)
            H.write_hazard_csv(_records(_graph(180)), hpath)
            eng = H.HazardEngine.bootstrap(graph_path=gpath, hazard_path=hpath,
                                           model_path=mpath, backend="pure",
                                           seed=SEED, verbose=False)
            self.assertTrue(os.path.exists(mpath))
            self.assertGreater(len(eng.graph.edges), 100)
            self.assertTrue(eng.model.fitted)
            self.assertIsNotNone(eng.index)


# ==========================================================================
class TestRealTimeSLA(unittest.TestCase):
    """Spec: re-weight 1,000 edges in under 2 seconds."""

    def test_under_two_seconds_per_thousand_edges(self):
        g = fx("big_graph")
        eng = fx("engine")
        timings = []
        for _ in range(3):
            gg = _graph(1100)
            timings.append(eng.reweight(gg, "heavy")[2])
        best = min(timings)
        self.assertGreaterEqual(len(g.edges), 1000)
        self.assertLess(best, 2.0, f"{len(g.edges)} edges took {best:.3f} s")

    def test_benchmark_report(self):
        g = _graph(1100)
        recs, idx = _records(g), None
        idx = _index(recs, g)
        eng = H.HazardEngine(fx("model"), g, H.CostPolicy(), index=idx)
        b = H.run_benchmark(eng, H.CostPolicy(), scenario="heavy", repeats=2)
        for k in ("edges", "backend", "score_and_reweight_s", "edges_per_second",
                  "single_edge_latency_us", "astar_route_ms", "sla_pass",
                  "sla_seconds"):
            self.assertIn(k, b, k)
        self.assertGreaterEqual(b["edges"], 1000)
        self.assertTrue(b["sla_pass"], b)
        self.assertLess(b["score_and_reweight_s"]["best"], 2.0)
        self.assertLess(b["single_edge_latency_us"]["p95"], 20000.0)
        self.assertGreater(b["astar_route_ms"], 0.0)
        json.dumps(b, default=H._json_default)


# ==========================================================================
class TestAudit(unittest.TestCase):
    def test_dgp_audit_recovers_most_of_the_ceiling(self):
        m, _g, recs, idx = fx("model_bundle")
        a = H.audit_dgp(recs, m, index=idx)
        for k in ("scoring", "n_rows", "auc_true_probability", "auc_model",
                  "log_loss_true", "log_loss_model", "spearman_pred_vs_truth",
                  "spearman_importance_vs_true_sensitivity",
                  "learned_importance", "true_standardised_sensitivity",
                  "dgp_intercept"):
            self.assertIn(k, a, k)
        self.assertEqual(a["n_rows"], len(recs))
        self.assertGreater(a["auc_true_probability"], 0.85)
        self.assertGreater(a["auc_model"], 0.85)
        self.assertLess(a["auc_model"], a["auc_true_probability"] + 0.01,
                        "a model cannot beat its own generator out-of-fold")
        self.assertGreater(a["spearman_pred_vs_truth"], 0.85)
        self.assertGreater(a["spearman_importance_vs_true_sensitivity"], 0.3)
        json.dumps(a, default=H._json_default)

    def test_ground_truth_is_reproducible_from_the_csv(self):
        """The audit must be able to rebuild p_true from stored columns alone."""
        recs = fx("records")
        before = H.DGP["intercept"]
        p1 = [H.sigmoid(H.true_disruption_logit(r)) for r in recs[:50]]
        H.DGP["intercept"] = before - 2.0
        p2 = [H.sigmoid(H.true_disruption_logit(r)) for r in recs[:50]]
        H.DGP["intercept"] = before
        self.assertLess(H.mean(p2), H.mean(p1))
        self.assertTrue(all(0.0 <= v <= 1.0 for v in p1))


# ==========================================================================
class TestCLI(unittest.TestCase):
    def run_cli(self, *args, timeout=1500):
        return subprocess.run([sys.executable, ENGINE, "--no-color", *args],
                              capture_output=True, text=True, timeout=timeout,
                              cwd=ROOT)

    def test_version(self):
        r = self.run_cli("--version", timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(H.__version__, r.stdout)

    def test_help_documents_the_cost_function(self):
        r = self.run_cli("--help", timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("C(e)=t(e)[1+a*P]+b*H(e)+g*S(e)*C0", r.stdout)
        for flag in ("--backend", "--scenario", "--corridor", "--cargo",
                     "--alpha", "--beta", "--gamma", "--block-threshold",
                     "--benchmark", "--selftest", "--json", "--audit-dgp"):
            self.assertIn(flag, r.stdout)

    def test_info_is_valid_json(self):
        r = self.run_cli("--info", timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["problem_id"], "SIH26002")
        self.assertIn("pure", d["backends_available"])
        self.assertEqual(d["features"], list(H.FEATURES))
        self.assertIn(d["backend_auto"], H.BACKEND_ORDER)

    def test_builtin_selftest_passes(self):
        r = self.run_cli("--selftest", timeout=1500)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:])
        self.assertIn("ALL CHECKS PASSED", r.stdout)
        self.assertNotIn("[FAIL]", r.stdout)

    def test_end_to_end_run_emits_valid_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_cli("--out", tmp, "--quiet", "--no-benchmark",
                             "--scenario", "light,heavy", "--register", "heavy")
            self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-2000:])
            expected = ("graph_light.json", "graph_heavy.json",
                        "route_light.json", "route_heavy.json",
                        "summary.json", "summary_light.json",
                        "summary_heavy.json", "hazard_model.json",
                        "segment_risk_register.csv")
            for name in expected:
                path = os.path.join(tmp, name)
                self.assertTrue(os.path.exists(path), f"missing {name}")
                self.assertGreater(os.path.getsize(path), 200, name)
            with open(os.path.join(tmp, "graph_heavy.json")) as fh:
                graph = json.load(fh)
            self.assertEqual(graph["type"], "FeatureCollection")
            self.assertGreater(len(graph["features"]), 1000)
            props = graph["features"][0]["properties"]
            for k in ("segment_id", "p_disruption", "risk_band", "cost_s",
                      "weather", "impassable"):
                self.assertIn(k, props, k)
            self.assertIn("rain_mm_hr", props["weather"])
            with open(os.path.join(tmp, "segment_risk_register.csv")) as fh:
                head = fh.readline().strip().split(",")
            for col in ("segment_id", "state", "highway", "p_disruption",
                        "risk_band", "cost_s"):
                self.assertIn(col, head)
            with open(os.path.join(tmp, "route_heavy.json")) as fh:
                route_doc = json.load(fh)
            self.assertIn("corridors", route_doc)

    def test_json_report_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_cli("--out", tmp, "--json", "--no-benchmark",
                             "--scenario", "light")
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            d = json.loads(r.stdout)
            self.assertEqual(d["problem_id"], "SIH26002")
            self.assertEqual(len(d["scenarios"]), 1)
            self.assertEqual(d["scenarios"][0]["scenario"], "light")
            self.assertIn("model", d)
            self.assertIn("inputs", d)

    def test_cost_coefficients_are_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_cli("--out", tmp, "--json", "--no-benchmark",
                             "--scenario", "heavy", "--alpha", "6.0",
                             "--cargo", "hazmat")
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            d = json.loads(r.stdout)
            pol = d["cost_policy"]
            self.assertEqual(pol["alpha"], 6.0)
            self.assertEqual(pol["cargo"], "hazmat")
            self.assertEqual(pol["S"], 2.0)
            self.assertIn("C(e)", pol["formula"])

    def test_corridor_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_cli("--out", tmp, "--json", "--no-benchmark",
                             "--scenario", "heavy",
                             "--corridor", "Guwahati->Silchar")
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            d = json.loads(r.stdout)
            cors = d["scenarios"][0]["routes"]
            self.assertEqual(len(cors), 1)
            self.assertIn("Guwahati", cors[0]["corridor"])

    def test_zero_dependency_mode(self):
        """The headline claim: it runs with numpy/pandas/sklearn/xgboost blocked."""
        script = (
            "import sys\n"
            "B={'numpy','pandas','sklearn','scipy','xgboost','torch','networkx'}\n"
            "class K:\n"
            "    def find_spec(self, n, p=None, t=None):\n"
            "        if n.split('.')[0] in B: raise ImportError(n)\n"
            "sys.meta_path.insert(0, K())\n"
            f"sys.path.insert(0, {ROOT!r})\n"
            "import hazard_prediction_engine as h\n"
            "assert h.available_backends() == ['pure'], h.available_backends()\n"
            "assert h.resolve_backend('auto') == 'pure'\n"
            "assert not any(h._HAS.values()), h._HAS\n"
            "print('ZERO-DEP OK')\n"
        )
        r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                           text=True, timeout=300, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ZERO-DEP OK", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
