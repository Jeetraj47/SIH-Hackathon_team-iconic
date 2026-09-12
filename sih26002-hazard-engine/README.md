# 🛰️ SIH26002 — Hazard Prediction Engine
### Predictive Route Optimization for Disaster-Prone Terrain
> **Segment-level disruption risk `P(e) ∈ [0,1]` for every road in the North Eastern Region, and a monsoon-aware re-weighting of the OSM graph that turns those probabilities into routes a convoy should actually take.**

![python](https://img.shields.io/badge/python-3.9%2B-blue) ![deps](https://img.shields.io/badge/required%20dependencies-ZERO-success) ![tests](https://img.shields.io/badge/tests-109%20passed-brightgreen) ![auc](https://img.shields.io/badge/ROC%20AUC-0.980%20(CV)-orange) ![sla](https://img.shields.io/badge/re--weight%201%2C105%20edges-0.008%20s-blueviolet) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

**Smart India Hackathon 2026 · Problem Statement SIH 26002 · Team ICONIC (119301) · Usha Martin University**
*A backend module of [NE-AURA](../NER/README.md) — the North Eastern Autonomous Resilient Architecture.*

---

## 📌 Executive summary

| | |
|:--|:--|
| **Input** | An OSM-style road graph + a historical hazard log + a live or forecast weather field |
| **Output** | `P_disruption(e)` per segment, a re-weighted GeoJSON graph per monsoon scenario, and risk-aware routes with a static-vs-hazard comparison |
| **Core model** | Gradient-boosted trees (XGBoost) with a pure-Python Newton-boosting fallback — **no mandatory dependencies** |
| **Cost function** | `C(e) = t(e)·[1 + α·P(e)] + β·H(e) + γ·S(e)` with α=2.5, β=0.8, γ=1.2 |
| **Real-time SLA** | 1,105 edges re-weighted in **0.008 s** (spec asks for < 2 s per 1,000 edges) → **250× headroom** |
| **Run it** | `python hazard_prediction_engine.py` — trains, scores 4 scenarios, routes, and writes every artefact in **~4 s** |

The engine is a **single file** (`hazard_prediction_engine.py`, ~4,800 lines, standard library only at its core) with a CLI, a JSON mode, and a Python API. It generates its own demo data deterministically if none is present, so a fresh clone needs no setup at all.

---

## 🚀 Quickstart

```bash
cd sih26002-hazard-engine
python hazard_prediction_engine.py          # that's it — no pip install required
```

First run generates `data/mock_osm_graph.geojson` (1,105 edges) and `data/historical_hazards_2023.csv` (4,952 observations), trains the model, scores four monsoon scenarios, routes the flagship corridors, and writes everything to `outputs/`.

```bash
# optional: unlock the faster XGBoost backend
pip install -r requirements.txt

# a single scenario, hazardous cargo, stricter risk aversion
python hazard_prediction_engine.py --scenario heavy --cargo hazmat --alpha 4

# machine-readable output for another service to consume
python hazard_prediction_engine.py --json --scenario extreme > risk.json

# prove the zero-dependency claim
python hazard_prediction_engine.py --backend pure --benchmark

# prove correctness
python hazard_prediction_engine.py --selftest      # 16 built-in checks
python -m unittest discover -s tests               # 202 unit tests
```

### Training on real data instead of the demo generator

```bash
# fabricate GSI/OSM/SRTM/IMD/ERA5/SoilGrids-shaped inputs, fuse them, retrain
python data_ingestion.py --demo --train

# or let it FETCH the roads for a state (OSMnx if installed, else Overpass)
python data_ingestion.py --state Mizoram --bbox 21.9,91.5,24.5,93.5 \
    --landslides data/raw/gsi_inventory.csv --dem data/raw/srtm --train
python data_ingestion.py --state Mizoram --offline --landslides data/raw/gsi_inventory.csv

# then point it at YOUR downloads (see DATASETS.md for where each one comes from)
python data_ingestion.py --inspect data/raw/gsi_landslide_inventory.csv
python data_ingestion.py --roads data/raw/ner_roads.geojson \
    --landslides data/raw/gsi_landslide_inventory.csv --dem data/raw/srtm \
    --slope-tif data/raw/aster_slope.tif \
    --rainfall data/raw/imd_district_daily.csv --soil online --year 2023
python hazard_prediction_engine.py --hazards data/historical_hazards_2023.csv \
    --graph data/ner_roads.geojson --retrain
```

`data_ingestion.py` is a separate, optional module: the engine still runs
standalone with no inputs at all. See **[DATASETS.md](DATASETS.md)** for the
source-by-source download guide and **[§ Using real data](#-using-real-data)**.

---

## 🧮 Mathematical formulation

### 1. Segment Disruption Risk

```
P_disruption(e) = f( rain(e), API₃(e), slope(e), soil_sat(e), hist_freq(e) )  ∈ [0,1]
```

`f` is a gradient-boosted tree ensemble over **12 features** — the 5 canonical spec features plus 7 engineered ones:

| canonical | engineered | why |
|:--|:--|:--|
| `rain_mm_hr` | `slope_x_rain` = tan(slope)·rain/10 | the dominant landslide trigger: intensity × driving force on a slope |
| `api_3d` (antecedent precipitation index, mm) | `cut_slope` × `soil_sat` | an engineered hill-cut face fails when the soil behind it saturates |
| `slope_deg` (effective terrain gradient) | `elev_band` | elevation-stratified failure regimes (valley flooding vs ridge slips) |
| `soil_saturation` ∈ [0,1] | `length_m`, `sinuosity` | exposure: a longer, more winding segment spans more failure cells |
| `hist_freq` (events / km / season) | `ndvi`, `drainage` | vegetation armouring and how fast water leaves the carriageway |

Raw tree output is passed through **Platt scaling** (`a = 0.811, b = −0.053`) so that the numbers are *probabilities*, not scores. This matters: `P(e)` is consumed by the cost function as a probability and reported to operators as "chance of disruption". Calibration error after scaling: **ECE 0.009**.

### 2. Dynamic cost function

```
C(e) = t(e) · [1 + α · P(e)]  +  β · H(e)  +  γ · S(e) · C₀

  t(e)  = L(e) / v(e, P)            free-flow transit time, seconds
  H(e)  = tan(slope(e)) · L(e) / V_REF     terrain friction, normalised to seconds
  S(e)  = cargo sensitivity multiplier
  α = 2.5   β = 0.8   γ = 1.2   V_REF = 10 m/s   C₀ = 60 s
```

Every term is in **seconds**, so `C(e)` is a time-like quantity a router can minimise directly. `H(e)` is divided by `V_REF` and `S(e)` is multiplied by a fixed handling overhead `C₀` precisely to keep the units coherent — the spec's `β·H(e)` and `γ·S(e)` are dimensionally heterogeneous without them, and a router cannot add metres to a unitless multiplier.

| cargo | S(e) | | cargo | S(e) |
|:--|--:|:--|:--|--:|
| `standard` | 1.0 | | `medical` | 1.75 |
| `perishable` | 1.5 | | `hazmat` / `fuel` | 2.0 |

**Hard blocking.** Segments with `P(e) ≥ 0.90` get `C(e) = ∞` — the CRITICAL band is a no-go, and a convoy planner must not be offered a road that is statistically closed. Pass `--no-hard-block` (or `hard_block=False`) to keep every segment routable with a finite penalty, or `--block-threshold 0.99` to tune it. Blocked edges are serialised as `"cost_s": null, "impassable": true` so the output stays strict JSON (`Infinity` is not valid JSON).

### 3. Risk bands → operational action

| band | P range | colour | recommended action |
|:--|:--|:--|:--|
| **LOW** | 0.00 – 0.15 | 🟢 | PROCEED — normal convoy discipline |
| **MODERATE** | 0.15 – 0.35 | 🟡 | MONITOR — advisory broadcast to fleet |
| **HIGH** | 0.35 – 0.60 | 🟠 | CAUTION — escort vehicle + reduced speed |
| **SEVERE** | 0.60 – 0.80 | 🔴 | AVOID — reroute unless mission-critical |
| **CRITICAL** | 0.80 – 1.00 | ⛔ | NO-GO — segment treated as blocked |

### 4. Empirical base risk (the "historical learning" requirement)

Raw per-segment event counts are noisy: a 200 m segment with one landslide looks 25× worse than a 20 km segment with the same count. `HistoricalHazardIndex` therefore computes **events per km per season** and applies empirical-Bayes shrinkage toward an exposure-weighted `(state, highway)` group prior:

```
freq(e) = ( km(e) · raw_freq(e) + k · prior(state, highway) ) / ( km(e) + k )      k = 1.6
```

Thin evidence is pulled toward its peer group; thick evidence is trusted. Unseen segments fall back segment → group → global prior (**1.46 events/km/season**).

**Train/serve parity.** `build_dataset(records, index)` substitutes the *index's* shrunken frequency into the training matrix. This is not cosmetic — an earlier revision trained on generator latencies and served on raw counts, which put serving features an order of magnitude below the training range. The index is now the single source of truth for `hist_freq` on both sides.

### 5. Weather field

`MonsoonField` lays `n_cells` Gaussian rain cells over the graph bbox, anchored on real road geometry (focus points), each with radius `radius_km × U(0.22, 0.58)`. Antecedent rainfall and soil saturation are derived from the cell field plus drainage and elevation, so the four scenarios are *spatially heterogeneous* — wet and dry pockets coexist, which is what makes a detour meaningful at all. Cells are seeded from `sha1(scenario)`, never Python's `hash()`, so runs are reproducible across processes and `PYTHONHASHSEED`.

| scenario | peak rain mm/h | peak API₃ | peak soil sat | cells | radius km |
|:--|--:|--:|--:|--:|--:|
| `light` | 2.5 | 25 | 0.32 | 3 | 58 |
| `moderate` | 12 | 85 | 0.52 | 5 | 50 |
| `heavy` | 35 | 190 | 0.74 | 6 | 44 |
| `extreme` | 78 | 340 | 0.91 | 8 | 40 |

---

## 🏗️ Architecture

```
        ┌──────────────────────────────────────────────────────────────────┐
        │  INPUTS  (auto-generated deterministically if absent)            │
        │   data/mock_osm_graph.geojson     1,105 edges · 988 nodes        │
        │   data/historical_hazards_2023.csv 4,952 obs · 718 disruptions   │
        └───────────────┬──────────────────────────────┬───────────────────┘
                        │                              │
          ┌─────────────▼──────────────┐   ┌───────────▼─────────────────┐
          │ HistoricalHazardIndex      │   │ TerrainModel                │
          │  events/km/season          │   │  anisotropic relief field   │
          │  + EB shrinkage (k=1.6)    │   │  400 m profile → p85 slope  │
          └─────────────┬──────────────┘   │  soil / NDVI / drainage     │
                        │                  └───────────┬─────────────────┘
                        │      ┌──────────────────┐    │
                        │      │ MonsoonField     │    │
                        │      │  rain cells      │    │
                        │      │  API₃, soil sat  │    │
                        │      └────────┬─────────┘    │
                        └───────────────┼──────────────┘
                                        │
                            ┌───────────▼────────────┐
                            │ edge_features(e, w)    │  12-D vector
                            │ record_features(row)   │  ← same schema, parity-tested
                            └───────────┬────────────┘
                                        │
                     ┌──────────────────▼───────────────────┐
                     │ HazardRiskModel                      │
                     │  xgboost ▸ sklearn ▸ torch ▸ pure    │
                     │  5-fold stratified CV + Platt scaling│
                     └──────────────────┬───────────────────┘
                                        │  P_disruption(e)
                            ┌───────────▼────────────┐
                            │ CostPolicy             │  C(e)=t(1+αP)+βH+γSC₀
                            │  block at P ≥ 0.90     │
                            └───────────┬────────────┘
                          ┌─────────────┴──────────────┐
              ┌───────────▼──────────┐      ┌──────────▼──────────────┐
              │ A* / Dijkstra router │      │ GeoJSON + JSON + CSV    │
              │  static vs hazard    │      │  artefact emitters      │
              └──────────────────────┘      └─────────────────────────┘
```

### Backend ladder — the zero-dependency guarantee

`BACKEND_ORDER = ("xgboost", "sklearn", "torch", "pure")`. `resolve_backend("auto")` picks the best that imports. If **none** are installed, `pure` takes over: a from-scratch Newton-boosted histogram tree ensemble (histogram binning, second-order leaf weights, depth-wise growth, JSON-serialisable) written in nothing but the standard library.

| backend | train (4,952 rows) | ROC AUC (CV) | ECE | re-weight 1,105 edges |
|:--|--:|--:|--:|--:|
| **xgboost** | 0.94 s | **0.9804** | 0.0090 | **0.008 s** (134k edges/s) |
| **pure** (stdlib only) | 15.3 s | 0.9801 | 0.0079 | 0.112 s (9.9k edges/s) |

The fallback is not a stub: it reaches the same discrimination and calibration, and it still clears the SLA by 18×. That is what "runs anywhere" means — a district control room on a locked-down Python install gets the same engine as a GPU box.

---

## 📁 Project structure

```
sih26002-hazard-engine/
├── hazard_prediction_engine.py     # the entire engine: CLI + library (~4,800 lines)
├── data_ingestion.py               # OPTIONAL: real-source readers + fusion CLI
├── README.md                       # this file
├── DATASETS.md                     # where to download each NER source, in what format
├── requirements.txt                # all optional; documents the backend ladder
├── Makefile                        # convenience targets (demo, pure, test, audit…)
├── LICENSE                         # MIT
├── .gitignore                      # generated data/ and outputs/ are not tracked
├── data/                           # auto-generated on first run
│   ├── mock_osm_graph.geojson      #   1,105 edges · 988 nodes · 5,442 km · EPSG:4326
│   ├── historical_hazards_2023.csv #   4,952 observations · 29 columns
│   ├── historical_hazards_2023.dgp.json  # ground-truth DGP coefficients (for --audit-dgp)
│   ├── raw/                        #   YOUR downloads (or --demo fabrications), untracked
│   └── cache/soilgrids.json        #   SoilGrids REST responses, never re-fetched
├── outputs/                        # emitted on every run
│   ├── graph_{light,moderate,heavy,extreme}.json   # re-weighted OSM graphs
│   ├── route_{light,moderate,heavy,extreme}.json   # static-vs-hazard route comparisons
│   ├── summary_{light,moderate,heavy,extreme}.json # per-scenario analytics
│   ├── summary.json                # combined run manifest
│   ├── hazard_model.json           # portable model card (+ .xgb.ubj native weights)
│   ├── segment_risk_register.csv   # flat per-segment register (--register <scenario>)
│   └── ingestion_report.json       # column-by-column real-vs-imputed provenance
└── tests/
    ├── test_engine.py              # 109 unit tests, no third-party deps needed
    └── test_ingestion.py           # 147 unit tests for the ingestion/fusion path
```

`data/` and `outputs/` are git-ignored: both are rebuilt bit-identically from `seed=26002` by the first run.

---

## 🎛️ CLI reference

```
python hazard_prediction_engine.py [options]
```

| group | flag | meaning |
|:--|:--|:--|
| **I/O** | `--graph PATH` | OSM-style GeoJSON graph (generated if absent) |
| | `--hazards PATH` | historical hazard log CSV/JSON (generated if absent) |
| | `--model PATH` | model card (default `<out>/hazard_model.json`) |
| | `--out DIR` | output directory (default `outputs/`) |
| | `--regenerate-data` | rebuild `data/` even if present |
| | `--register SCENARIO` | also write the flat per-segment risk CSV |
| **data** | `--edges N` | target edge count for the generated graph (default 1000) |
| | `--seed N` | global PRNG seed (default 26002) |
| **model** | `--backend {auto,xgboost,sklearn,torch,pure}` | force a backend |
| | `--retrain` | ignore the cached model card |
| | `--no-calibrate` | disable Platt scaling |
| | `--folds N` / `--threshold P` | CV folds / decision threshold |
| | `--audit-dgp` | score the model against the synthetic ground truth |
| | `--estimators N` / `--max-depth N` | override tree hyper-parameters |
| **cost** | `--alpha` `--beta` `--gamma` | the three cost coefficients (2.5 / 0.8 / 1.2) |
| | `--cargo {standard,perishable,medical,hazmat,fuel}` | S(e) profile |
| | `--cargo-mode {additive,multiplied}` | documented additive form, or S also scaling t(e) |
| | `--cargo-overhead S` / `--vref MS` | C₀ seconds and V_REF m/s normalisers |
| | `--rain-derate F` | fraction of free-flow speed lost at P=1 (default 0 = off) |
| | `--block-threshold P` / `--no-hard-block` | hard-closure policy |
| **routing** | `--scenario LIST` | comma-separated subset of the 4 scenarios |
| | `--corridor 'A->B'` | repeatable; accepts city names, node ids or `lat,lon` |
| | `--algorithm {astar,dijkstra,both}` | shortest-path engine |
| **report** | `--benchmark` / `--no-benchmark` | latency & throughput SLA test |
| | `--json` / `--quiet` / `--no-color` / `--info` | machine-readable, silent, plain, capability report |
| | `--selftest` / `--version` | built-in checks / version |

---

## 📤 Output artefacts

### Re-weighted graph — `outputs/graph_heavy.json`

A drop-in OSM `FeatureCollection`. Each feature keeps its original geometry and gains risk and cost properties:

```json
{
  "type": "Feature",
  "geometry": { "type": "LineString", "coordinates": [[91.746607, 26.064448], ...] },
  "properties": {
    "segment_id": "SEG-00001", "highway": "NH-6", "state": "Assam",
    "length_m": 18971.43, "slope_deg": 2.86, "cut_slope": 0,
    "hist_freq": 0.8066,
    "p_disruption": 0.87599, "risk_band": "CRITICAL",
    "transit_s": 1521.09, "slope_friction_s": 94.78,
    "base_cost_s": 1668.92, "cost_s": 5000.09, "cost_s_soft": 5000.09,
    "impassable": false,
    "weather": { "rain_mm_hr": 120.45, "rain_24h_mm": 433.9,
                 "api_3d": 615.1, "soil_saturation": 0.9045 }
  }
}
```

`base_cost_s` is the risk-blind cost `C(e)|P=0` — what OSRM or Google Maps would route on — so any consumer can compute both routes from one file. Segments above the block threshold carry `"cost_s": null` and `"impassable": true`.

### Route comparison — `outputs/route_heavy.json`

For every corridor: `baseline` (static engine) and `risk_aware` (hazard engine), each with `distance_km`, `transit_min`, `cost_s`, `risk_exposure` (length-weighted mean P), `max_p`, `expected_closure_hours`, `impassable_segments`, `band_counts`, per-leg detail, and a `delta` block (`rerouted`, `severed`, `distance_pct`, `risk_exposure_pct`, `segments_avoided`, `segments_added`).

### Model card — `outputs/hazard_model.json`

Self-describing and portable: `features` + `feature_units`, `risk_bands`, `hyperparams`, the full `metrics` block (including the band-lift table), `platt_calibration`, `feature_importance`, `backend_stack`, `seed`, `sha1`. Loading it with `HazardRiskModel.load()` restores identical predictions without retraining.

### Risk register — `outputs/segment_risk_register.csv`

Flat, spreadsheet-ready: `scenario, segment_id, state, district, highway, name, lat, lon, length_m, slope_deg, surface, soil_type, hist_freq, rain_mm_hr, api_3d, soil_saturation, p_disruption, risk_band, transit_s, base_cost_s, cost_s, cost_ratio, recommended_action`.

---

## 🐍 Python API

```python
from hazard_prediction_engine import (
    HazardEngine, CostPolicy, MonsoonField, route, SCENARIOS,
)

# one call: generate/load inputs, train or load the model, build the index
engine = HazardEngine.bootstrap()          # backend="auto", seed=26002

# score a scenario and re-weight the graph in place
graph, weather, elapsed = engine.reweight(engine.graph, "heavy")
print(f"{len(graph.edges)} edges in {elapsed:.3f} s")

# route with both engines and compare
cmp = engine.compare_routes(graph, "Imphal", "Kohima")
print(cmp["delta"]["distance_pct"], cmp["delta"]["risk_exposure_pct"])
print(cmp["delta"]["segments_avoided"])

# ad-hoc routing on the re-weighted graph
r = route(graph, "Guwahati", "Silchar", algorithm="astar", mode="risk")
print(r.distance_km, r.transit_min, r.risk_exposure, r.impassable_segments)

# a single point, no graph needed (for a live ops console)
p = engine.score_point(lat=25.5788, lon=91.8933, rain_mm_hr=42.0,
                       api_3d=210.0, soil_saturation=0.81, slope_deg=17.5)

# network-wide resilience
print(engine.hub_resilience(graph)["connectivity_index"])
print(engine.severance_report(graph, hub="Guwahati")["accessibility_index"])

# tune the policy
pol = CostPolicy(alpha=4.0, beta=1.2, gamma=2.0, cargo="hazmat",
                 block_threshold=0.85)
```

`RoadGraph.to_networkx()` bridges to NetworkX when it is installed (it raises a clear `RuntimeError` otherwise) (undirected, with `p_disruption`, `risk_band`, `cost_s` and `length_m` as edge attributes) for betweenness or flow analysis; everything else stays dependency-free.

---

## 🔌 Integrating with a real router

The engine is deliberately a **weight producer**, not a routing daemon. Point any router at the emitted weights:

| router | how |
|:--|:--|
| **OSRM / Valhalla** | regenerate the profile with per-way `cost_s` (or use the `speed_kmh` derate via `--rain-derate`), or consume `graph_*.json` and feed `cost_s` as a custom weight table |
| **pgRouting** | `UPDATE edge_table SET cost = <cost_s>, reverse_cost = <cost_s> WHERE id = <segment_id>` then `pgr_dijkstra` / `pgr_aStar` |
| **NetworkX** | `G = RoadGraph.from_geojson(doc).to_networkx()`, then `nx.astar_path(G, u, v, weight="cost_s")` |
| **OSMnx / raw OSM** | `load_geojson_graph()` accepts any EPSG:4326 `FeatureCollection` of LineStrings with `length_m`; missing attributes are inferred from geometry |
| **your own A\*** | read `p_disruption` + `cost_s` per feature; blocked edges are flagged `impassable` |

To score live forecasts instead of the four canned scenarios, build a `WeatherState` from your IMD/nowcast feed and call `engine.score_edges(edges, weather)` directly.

---

## 📊 Results

All numbers below are from `python hazard_prediction_engine.py` on this machine (2 cores, Python 3.11, XGBoost 3.2), `seed=26002`, reproducible to the digit.

### Model quality — 5-fold stratified CV on 4,952 observations (718 positive, 14.5%)

| metric | value | | metric | value |
|:--|--:|:--|:--|--:|
| ROC AUC | **0.9804 ± 0.0062** | | Log loss | 0.1191 |
| PR AUC | 0.9184 | | Brier score | 0.0351 |
| KS statistic | 0.8629 | | Brier skill vs base rate | +0.7169 |
| Precision @0.5 | 0.8548 | | ECE (calibration) | **0.0090** |
| Recall @0.5 | 0.8036 | | F1 @0.5 | 0.8284 |

Feature importance: `slope_x_rain` 0.38 · `api_3d` 0.16 · `soil_saturation` 0.14 · `hist_freq` 0.11 · `rain_mm_hr` 0.04 · `slope_deg` 0.03. The rain×slope interaction dominating is physically right: intensity alone rarely moves a slope, and a slope alone rarely fails in a dry spell.

### Operational validation — what actually happened in each band

Out-of-fold predictions, binned into the five operational bands. *Observed* is the empirical disruption rate; *predicted* is the mean model probability; *lift* is observed ÷ network base rate.

| risk band | n | share | observed | predicted | lift |
|:--|--:|--:|--:|--:|--:|
| LOW | 3,980 | 80.4% | **1.3%** | 1.3% | 0.09× |
| MODERATE | 209 | 4.2% | **27.8%** | 22.9% | 1.91× |
| HIGH | 149 | 3.0% | **45.0%** | 47.7% | 3.10× |
| SEVERE | 123 | 2.5% | **63.4%** | 70.8% | 4.37× |
| CRITICAL | 491 | 9.9% | **94.7%** | 94.1% | 6.53× |

Observed tracks predicted in every band, and the ordering is monotone by a factor of 73 from LOW to CRITICAL. A "LOW" segment really is a 1-in-77 proposition and a "CRITICAL" one really is a near-certainty — which is the only property that makes the bands safe to hand to a convoy commander.

### Honesty check — `--audit-dgp`

Because the demo data is synthetic, the true generative probability `p_true(e)` is known. The audit compares the model against that ceiling **on out-of-fold predictions** (never in-sample, which would flatter the model):

```
DGP audit  [out-of-fold] truth-ceiling AUC 0.9849 vs model 0.9804
           logloss 0.1043 vs 0.1191
           ρ(prediction, ground truth) = 0.955
           ρ(importance, true sensitivity) = 0.718
```

The model recovers **99.5%** of the achievable AUC and cannot — and does not — beat its own generator. ρ(importance, true sensitivity) = 0.72 says the learned feature ranking broadly matches the coefficients that actually produced the labels.

> **Read this number correctly.** 0.98 AUC is a property of *synthetic* data with a known, smooth generating process. Real BRO/NHIDCL/IMD logs are noisier, sparser, biased toward reported incidents, and only partially geolocated; expect materially lower discrimination there. What transfers is the pipeline, the calibration discipline and the cost model — not the headline AUC.

### Scenario sweep — 1,105 edges / 5,442 km

| scenario | rain mean/max mm/h | API₃ | soil sat | P̄ | P p99 | LOW/MOD/HIGH/SEV/CRT | km ≥ 0.35 | cut | access | re-weight |
|:--|:--|--:|--:|--:|--:|:--|--:|--:|--:|--:|
| LIGHT | 1.1 / 5 | 14 | 0.07 | 0.005 | 0.052 | 1100/5/0/0/0 | 0 | 0 | **1.00** | 0.01 s |
| MODERATE | 8.1 / 62 | 70 | 0.27 | 0.051 | 0.814 | 1010/47/14/19/15 | 236 | 7 | 0.96 | 0.01 s |
| HEAVY | 15.8 / 116 | 117 | 0.48 | 0.135 | 0.987 | 878/69/41/35/82 | 817 | 55 | 0.75 | 0.02 s |
| EXTREME | 44.5 / 222 | 271 | 0.76 | 0.307 | 0.996 | 655/83/63/77/227 | 1,824 | 174 | **0.25** | 0.02 s |

*access* = the NER Accessibility Index: fraction of the 28 named hubs still reachable from Guwahati. Light rain cuts nothing; a cloudburst isolates three quarters of the region. That single number is the operational argument for this problem statement.

### Hub-to-hub resilience — 28 hubs, 378 pairs

Exact connectivity is computed by union-find over the passable segments; a deterministic 60-pair sample is then routed under both engines.

| scenario | connectivity | components | held | rerouted | severed | detour cost | exposure delta |
|:--|--:|--:|--:|--:|--:|:--|--:|
| LIGHT | 100.0% | 1 | 53 | 7 | 0 | +13.9% km / +3.4% t | **−70.7%** |
| MODERATE | 92.9% | 5 | 33 | 27 | 7 | +20.5% km / +14.4% t | −34.2% |
| HEAVY | 55.6% | 41 | 42 | 18 | 20 | +27.8% km / +20.4% t | −55.8% |
| EXTREME | 8.2% | 135 | 27 | 33 | 55 | +10.7% km / +5.7% t | −24.8% |

The headline result: **under light rain the risk-aware router buys a 71% cut in expected disruption exposure for 14% more kilometres.** That is the trade a logistics operator wants — a few extra minutes of driving to avoid the segments most likely to close. As the monsoon intensifies the network fragments faster than any router can compensate, and by EXTREME most pairs are simply severed; at that point the correct output is "do not dispatch", not "here is a clever detour".

### Flagship corridors — HEAVY monsoon

| corridor | engine | distance | transit | P exposure | max P | closure | verdict |
|:--|:--|--:|--:|--:|--:|--:|:--|
| Guwahati → Shillong | static | 70.3 km | 1 h 39 m | 0.262 | 0.99 | 42 h | 2 CUT |
| | hazard | 70.3 km | 1 h 39 m | 0.262 | 0.99 | 42 h | SEVERED — shortest path is also least-risk |
| Silchar → Aizawl | static | 123.9 km | 3 h 20 m | 0.171 | 0.91 | 66 h | 1 CUT |
| | hazard | 150.4 km | 5 h 11 m | 0.263 | 0.87 | 67 h | AVOIDED 1 CUT segment (+21% km) |
| Imphal → Kohima | static | 98.4 km | 3 h 23 m | 0.433 | 0.98 | 122 h | 3 CUT |
| | hazard | 297.8 km | 8 h 44 m | **0.054** | 0.77 | **55 h** | AVOIDED 3 CUT segments (+203% km) |

Imphal → Kohima is the flagship result: tripling the distance drops expected exposure **88%** (0.433 → 0.054) and halves expected closure hours (122 → 55 h). Guwahati → Shillong severs, and the engine says so plainly instead of inventing a detour — the weather cells cover every approach to the Shillong plateau, so the least-risk path *is* the shortest one and no router can help. Reporting that honestly is part of the deliverable.

A detour can raise *mean* exposure when it trades one hard-blocked segment for several moderately risky ones; the cost function minimises `C(e)`, not `P`, which is why the CUT column is the operative signal.

### Performance — real-time SLA

| measure | xgboost | pure (stdlib) | spec |
|:--|--:|--:|:--|
| re-weight 1,105 edges | **0.008 s** | 0.112 s | < 2.0 s per 1,000 edges |
| throughput | 133,884 edges/s | 9,868 edges/s | — |
| single edge p50 / p95 | 271 µs / 399 µs | 49 µs / 71 µs | — |
| A* corridor search | 1.2 ms | 1.6 ms | — |
| SLA verdict | **PASS (250×)** | **PASS (18×)** | — |

Note the per-edge inversion: XGBoost is 13× faster on a *batch* (it vectorises the whole graph in one call) but slower on a *single* edge, where array conversion dominates. Score the graph in bulk and cache; use `score_point()` for one-off operator queries, where either backend is comfortably interactive.

Re-weighting a *state-sized* network (50,000 edges) would still take well under a second, so the engine can be re-run on every IMD forecast refresh.

---

## 🧪 Testing

```bash
python hazard_prediction_engine.py --selftest     # 16 built-in checks, ~8 s
python -m unittest discover -s tests              # 256 unit tests, ~105 s
make test && make selftest
```

The built-in suite (`--selftest`) travels with the single file so it can be run in the field. The `tests/` suite needs no third-party packages either.

Coverage includes: haversine/bearing/offset geometry · graph determinism, connectivity and terrain plausibility · GeoJSON round-trip · hazard-log schema, seasonality and base-rate calibration · empirical-Bayes shrinkage on thin vs thick evidence · **train/serve feature parity** (both extractors must emit a byte-identical schema) · ROC/PR/log-loss/Brier/KS/ECE against hand-computed cases · stratified k-fold partitioning · Platt calibration improving ECE · the pure GBDT learning and serialising losslessly · **the cost formula asserted term-by-term against the documented expression** · monotonicity of C in P · hard-block and `--no-hard-block` behaviour · Dijkstra ≡ A* · blocked-edge detours · scenario monotonicity (mean P and cut count must rise light→extreme, and LIGHT must cut nothing) · hub resilience · the <2 s SLA · emitted-artefact validity (strict JSON, no `Infinity`) · the CLI end-to-end · and the **zero-dependency guarantee**, verified by re-importing the module inside a subprocess whose `sys.meta_path` raises `ImportError` for numpy, pandas, scikit-learn, scipy, xgboost, torch and networkx.

`tests/test_ingestion.py` (147 tests) covers the real-source path with the same
rule: no third-party packages, no network. Every "download" is a file written
into a temp dir in the exact container the real portal serves — big-endian
`.hgt`, ESRI `.asc`, OSM-tagged GeoJSON, GSI/IMD/ERA5-Land CSV, integer-encoded
ISRIC JSON. It asserts: `.hgt` byte order and tile georeferencing from the
filename · `.asc` north-first row order (the classic bug) · void cells degrading
to neighbours rather than to −32768 m · Horn slope recovering a known gradient ·
column sniffing across portal dialects and date formats · the rainfall index's
API decay and its **counted** district fallback · ISRIC integer decoding ·
porosity from bulk density · the bucket model's mass balance and its agreement
with the satellite-moisture scale · **T-junctions surviving length-based way
splitting** (the trap that silently fragments an ingested network into
components and makes every corridor report `NO PATH`) · MultiLineString
support · city-name snapping that never moves a node · the event→road join
matching a hit **mid-segment** rather than only near the centroid · the
case-control label design · **expanding-window `hist_freq` never leaking its own
label** · provenance separating real from imputed columns · CLI determinism ·
and the zero-dependency guarantee re-checked for this module too.

The fetch path is covered without a network: Overpass and OSMnx replies are
converted from stub objects, so neither `geopandas` nor `shapely` nor `osmnx`
needs to be installed. It asserts both `--bbox` orderings resolve to one
answer · an over-large or transposed box is refused rather than silently
cropped · the inventory centroid is a **median**, so one mistyped coordinate
cannot move the fetch window · and — the one that encodes the most judgement —
that capping a fetch by **shrinking the window** leaves >90 % of the network in
one component while capping it by **sampling rows**, the obvious approach, does
not. The offline contract is tested too: a cache hit must not touch the
network, `--offline` with no cache must stop with instructions rather than dial
out, and a cache fetched for a different window must be announced loudly.

Two tests are worth calling out because they encode judgement rather than arithmetic:

- **`test_wet_ranks_above_dry`** — the same segment must score strictly higher in a downpour than when dry, and a 22° cut face higher than a 1° plain. A model that fails this is not usable for routing however good its AUC looks.
- **`test_fit_and_predict` / Bayes-ceiling checks** — synthetic toy data is scored against `roc_auc(y, p_true)`, the ceiling of its own generator, not against an arbitrary constant. The pure backend recovers 96% of it.

### Bugs the suite caught during development

- The hazard-log base-rate calibration used a Newton step on the *mean of sigmoids*, which oscillated on a wide logit distribution; replaced with a bracketed bisection, which is monotone by construction.
- That bisection then made the generator **non-reproducible across calls**: the bracket was derived from the previously-tuned global intercept, so the row sitting exactly on the decision boundary flipped depending on call order. The bracket is now fixed and the winner rounded before the final labelling pass — three consecutive calls are bit-identical.
- `PureGBDT.fit(X, y, seed)` was being called through a backend interface whose third positional argument was `sample_weight`.
- Train/serve skew on `hist_freq` (see §4 above).

---

## 🌱 Using real data

The engine ships with a synthetic generator so it runs with no inputs at all.
`data_ingestion.py` replaces those inputs with evidence from the actual NER
sources, and needs no code changes to the engine:

```bash
python data_ingestion.py --demo --train     # works offline, no pip install
```

That one command fabricates every source below **in its real container**, fuses
them, and retrains the engine on the result — so the whole path is exercised
before you have downloaded anything.

| Source | Feeds | Format the loader reads |
|:--|:--|:--|
| GSI Bhukosh / NGDR / NLSM landslide inventory | `disrupted`, `hazard_type`, `closure_hours`, `debris_tonnes`, `hist_freq_per_km` | CSV / TSV / GeoJSON Points, headers auto-detected |
| OSM (Overpass, OSMnx) or NESAC / Bhuvan roads | `length_m`, `highway`, `surface`, `speed_kmh`, `cut_slope`, `sinuosity`, topology | GeoJSON `LineString` **or** `MultiLineString`, EPSG:4326 — **or fetched live** with `--state` / `--bbox` |
| Copernicus GLO-30, SRTMGL1/NASADEM, Cartosat-1, ASTER GDEM **v3** | `slope_deg`, `elevation_m`, `relief_m`, `cut_slope` | `.hgt` (big-endian int16), ESRI `.asc`, GeoTIFF — a file or a whole directory |
| IMD / NESAC / NEDFI rainfall | `rain_mm_hr`, `rain_24h_mm`, `api_3d_mm` | CSV in long, wide, grid or monthly layout, auto-detected |
| ISRIC SoilGrids 2.0 | `soil_type`, `drainage`, field capacity, wilting point, porosity | REST JSON (fetched and cached for you) or a local CSV |
| ERA5-Land or SMAP L4 | `soil_saturation` (observed, overrides the bucket model) | CSV `date,lat,lon,<moisture>` |
| Sentinel-2 / Landsat NDVI | `ndvi` | any raster the DEM reader handles |
| Pre-computed slope (GDAL, QGIS, ASTER derivative) | `slope_deg`, ahead of DEM-derived slope | ESRI `.asc` or GeoTIFF, degrees **or** percent — the unit is detected |

**[DATASETS.md](DATASETS.md)** is the full guide: download locations, exact REST
endpoints and query parameters, ISRIC's integer unit encoding, the four accepted
rainfall layouts, the OSM tag table, and a troubleshooting matrix. It also
corrects several claims that circulate in dataset guides for this region — most
importantly that **SoilGrids does not provide soil moisture** (`wv0033`/`wv1500`
are water-*retention* points), and that `ox.graph_to_geojson` does not exist.

### Getting the roads without downloading them

Roads are the one source the pipeline can fetch for itself. Omit `--roads` and
name a window:

```bash
python data_ingestion.py --state Mizoram --landslides data/raw/gsi_inventory.csv
python data_ingestion.py --state Assam --bbox 24.0,89.5,28.0,96.0 --gsi-csv inv.csv
```

It tries the on-disk cache, then OSMnx if installed, then the Overpass API
across three mirrors — the last of those needs nothing but the standard
library, and the OSMnx path is converted straight off the graph object so it
needs neither `geopandas` nor `pyogrio`. `osmnx` is imported on first use, not
at module load: it drags in half the geospatial stack and would cost several
seconds on every run even when the roads are already a file.

Every fetch is cached, so only the first run needs network access, and
`--offline` turns that into a hard promise — the run uses caches or stops with
an explanation, and never dials out. Two details matter more than they look:

- `--bbox` is read as `S,W,N,E` **or** `W,S,E,N`. Overpass and OSMnx use
  opposite conventions, so the ordering is detected from the values instead of
  trusting you to remember which tool you copied it from. A transposed or
  over-large box is refused with the reason, not silently cropped.
- `--max-edges` **shrinks the window; it never samples rows.** Random sampling
  leaves every surviving way without its neighbours, so the "network" becomes
  thousands of two-node islands and routing reports `NO PATH` everywhere while
  nothing raises an error. The window centres on the *median* inventory
  coordinate, so a 500 km-wide state box still fetches where the landslides are.

Inventories and rainfall it still cannot fetch: Bhukosh, NGDR, NLSM and the
NEDFI databank sit behind interactive sessions and registration, so there is no
honest way to script them.

### The three things that decide whether a real run works

**1. Inspect before you fuse.** Every reader has a dry run that reports what it
detected rather than what you hoped it would detect:

```bash
python data_ingestion.py --inspect data/raw/ner_roads.geojson
```
```
  type     : GeoJSON FeatureCollection, 30 feature(s)
  used     : ['highway', 'surface', 'lanes', 'maxspeed', 'cutting', 'name', 'ref', 'district']
  loaded   : 129 segments / 130 nodes / 1185.5 km (99 length splits, 20 junction splits)
  topology : 1 connected component(s), largest 130 node(s)
```

On a road file the number that matters is **components**. In real OSM,
connectivity lives in shared node IDs; rebuilt from coordinates, a feeder road
that meets a trunk mid-way looks connected in QGIS and becomes a separate
component after length-based splitting, so every corridor through it reports
`NO PATH`. The loader runs a junction pre-pass and forces splits onto shared
vertices, but ways that merely *touch* without sharing a vertex still need
snapping. `--inspect` on a CSV prints the resolved column mapping and the
layout it will be read as; on a DEM directory it prints per-tile extent, pixel
size in metres and the **void fraction**.

**2. Read the provenance report.** `outputs/ingestion_report.json` states,
column by column, whether a feature came from a real source or from a documented
fallback — and prints the summary at the end of every run:

```
  real-data cols  : api_3d_mm, cut_slope, disrupted, drainage, elevation_m,
                    hist_freq_per_km, length_m, rain_24h_mm, rain_mm_hr,
                    relief_m, sinuosity, slope_deg, soil_saturation, soil_type, state
  IMPUTED cols    : ndvi
```

A model trained on imputed columns is not the same product as one trained on
real ones, and the report exists so nobody has to guess which they have.

**3. Labels are case-control, and history cannot leak.** A positive row is
created *at* each matched event date, so its weather is the weather that
actually accompanied the failure; negatives are sampled from the season,
excluding any date within `--window` days of a known event on that segment, and
preferentially from wet days because dry days teach nothing.
`hist_freq_per_km` counts **only events strictly before** the row's own date,
normalised by segment length and by seasons of inventory available at that
point. The usual `hist_freq = len(events_on_segment)` leaks the label directly
and produces an AUC that will not survive contact with a new monsoon.

### Outputs

| File | Contents |
|:--|:--|
| `data/historical_hazards_<year>.csv` | fused training table in the engine's 29-column schema |
| `data/ner_roads.geojson` | road graph with DEM, soil and OSM attributes baked in — pass to `--graph` |
| `outputs/ingestion_report.json` | column provenance + every source's parse statistics |

Then retrain, and everything downstream is unchanged:

```bash
python hazard_prediction_engine.py --hazards data/historical_hazards_2023.csv \
    --graph data/ner_roads.geojson --retrain
```

Verified end to end on the fabricated real-format inputs: 672 observations
(156 positive), 129 segments / 1,185 km in one connected component, 9 named
city hubs, **ROC AUC 0.9488 ± 0.0228**, PR-AUC 0.8403, ECE 0.038, and observed
disruption rates rising monotonically across the risk bands (0.08× → 3.69×
lift). The same run reproduces bit-identically with numpy and rasterio blocked
from importing.

---

## ⚠️ Limitations

Stated plainly, because a hazard product that oversells itself is worse than no product:

1. **The engine's built-in demo data is synthetic.** Slopes, soils, rainfall cells and hazard counts come from a documented generator (`DGP` coefficients shipped in `data/*.dgp.json`), georeferenced to real NER cities, highways and elevations. It exercises the pipeline; it is not evidence about any actual road. `--audit-dgp` exists so the model can be checked against known truth rather than trusted on its AUC. `data_ingestion.py --demo` likewise fabricates its inputs — in the real source *formats*, but not with real values — and says so on every run.
2. **No live weather feed.** `data_ingestion.py` reads *historical* IMD/NESAC/NEDFI rainfall; the four scoring scenarios are still canned fields. Production needs IMD nowcast ingestion into a `WeatherState` — the scoring path already accepts one, and the historical reader is the shape it would take.
3. **Closure *hours* are heuristic.** `expected_closure_hours` is a susceptibility-weighted estimate for ranking routes, not a clearance-time forecast. BRO task-force deployment, debris volume and single-carriageway vs double-lane matters are not modelled.
4. **No temporal dynamics.** Rain cells are static snapshots; there is no recession curve, no forecast horizon, no convoy-in-motion re-planning.
5. **Correlation is not causation.** `slope_x_rain` dominating importance reflects the generator's physics, and would need re-validation on real logs — reporting bias (incidents are reported where people are) is a genuine hazard for this feature set.
6. **EXTREME severs most corridors.** At 78 mm/h peak with 174 impassable segments, hub-to-hub connectivity drops to 8.2%. The engine reports this rather than routing through statistically closed roads — the correct answer is "do not dispatch", and a planner that pretended otherwise would be dangerous.

**Done since the first release:** real DEM loading (`.hgt` / `.asc` / GeoTIFF, mosaiced, void-tolerant) for `slope_deg`, `elevation_m` and `relief_m` · ISRIC SoilGrids client with disk cache for `soil_type`, `drainage` and water retention · ERA5-Land / SMAP soil-moisture ingestion · IMD/NESAC/NEDFI rainfall readers in four layouts · GSI/NGDR inventory readers with header sniffing · OSM road ingestion with junction-preserving splits and city-name snapping · column-level provenance reporting · **live OSM road fetching** by `--state` / `--bbox` (OSMnx or Overpass, cached, with `--offline` as a hard promise) · **pre-computed slope rasters** via `--slope-tif` with automatic degree/percent detection · `--inspect` dry runs · optional `logging` output via `--log-level` / `--log-file`.

**Roadmap:** IMD *live* nowcast ingestion · temporal recession + forecast horizon · multi-modal swap hooks (road → rail → NW-2 waterway → air-lift, per NE-AURA) · clearance-time model from BRO task-force data · per-vehicle-type speed profiles · streaming re-weight for networks >10⁶ edges · Sentinel-1 SAR change detection for post-event verification.

---

## 👥 Team ICONIC

**Team ID** `119301` · **Institution** Usha Martin University · **Problem Statement** SIH 26002

| | |
|:--|:--|
| **Jeet Raj** | Team Leader & Lead Systems Architect |
| **Roshan Kumar Sahu** | Lead GNN & Full-Stack AI Engineer |
| **Satyam Kumar** | Geospatial & ULIP Gateway Lead |
| **Ankit Kujur** | Edge Compute & Delta-CRDT Engineer |
| **Samir Sarar** | Multilingual Bhashini Voice AI Specialist |
| **Aastha Jaiswal** | UI/UX & Public Audit Governance Specialist |

This module is the hazard-prediction and route-optimisation backend of **NE-AURA**, the team's multi-modal logistics, climate-resilience and public-fund-governance platform for the eight North Eastern states. See [`../NER/README.md`](../NER/README.md) for the platform overview and [`../NER/frontend/`](../NER/frontend/) for the 12 consumer-facing applications. **This module is backend-only by design** — it exposes a CLI, a JSON mode and a Python API for those applications to consume.

---

## 📄 License

MIT — see [`LICENSE`](LICENSE).

*Built for Smart India Hackathon 2026 (SIH 26002) by Team ICONIC.*
