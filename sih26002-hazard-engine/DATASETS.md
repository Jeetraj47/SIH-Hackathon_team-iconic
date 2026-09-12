# 📥 Real datasets for the North Eastern Region

Where to get each input `data_ingestion.py` consumes, in the format it consumes
it, and what to do when a portal will not give you a clean download.

Nothing here is required to run the engine — `python data_ingestion.py --demo`
fabricates every source below in its real container so the whole fusion path can
be exercised offline. This file is for replacing those stand-ins with evidence.

---

## 1. The map: source → feature → flag

| # | Source | Feeds | Container the loader reads | Flag |
|:--|:--|:--|:--|:--|
| 1 | **GSI Bhukosh / NGDR / NLSM** landslide inventory | `disrupted`, `hazard_type`, `closure_hours`, `debris_tonnes`, `hist_freq_per_km` | CSV, TSV, GeoJSON FeatureCollection (Point) — headers auto-detected | `--landslides` |
| 2 | **OSM** (Overpass / OSMnx) or **NESAC / Bhuvan** roads | `length_m`, `highway`, `surface`, `speed_kmh`, `cut_slope`, `sinuosity`, network topology | GeoJSON `LineString` **or** `MultiLineString`, EPSG:4326 | `--roads` |
| 3 | **Copernicus GLO-30 / SRTMGL1 / NASADEM / Cartosat-1 / ASTER GDEM v3** | `slope_deg`, `elevation_m`, `relief_m`, `cut_slope`, `grade_pct` | `.hgt` (big-endian int16), ESRI `.asc`, GeoTIFF (needs `rasterio`) — a single file or a whole directory | `--dem` |
| 4 | **IMD / NESAC / NEDFI** rainfall | `rain_mm_hr`, `rain_24h_mm`, `api_3d_mm` | CSV in long, wide, grid or monthly layout — auto-detected | `--rainfall` |
| 5 | **ISRIC SoilGrids 2.0** | `soil_type`, `drainage`, field capacity, wilting point, porosity | REST JSON (fetched for you) or a local `lat,lon,clay,…` CSV | `--soil online` / `--soil-csv` |
| 6 | **ERA5-Land** or **SMAP L4** soil moisture | `soil_saturation` (observed, overrides the bucket model) | CSV `date,lat,lon,<moisture>` | `--moisture` |
| 7 | **Sentinel-2 / Landsat NDVI** | `ndvi` | any raster the DEM reader handles (`--ndvi` is read as a raster and rescaled) | `--ndvi` |

`state`, `district` come from the road properties when present, else from a
bounding-box lookup over the eight NER states.

---

## 2. Terrain — the single biggest lever on model quality

`--dem` accepts a directory and mosaics every tile in it, so download whole
1°×1° tiles and drop them in one folder.

| Product | Resolution | Where | Notes |
|:--|:--|:--|:--|
| **Copernicus GLO-30** | 30 m | Copernicus Open Access Hub / AWS open data | Generally the best free global DEM; fewer voids than SRTM in the NER |
| **SRTMGL1_003 / NASADEM** | 30 m | USGS EarthExplorer (login free), NASA Earthdata | NASADEM is SRTM reprocessed with voids largely filled — prefer it |
| **Cartosat-1 DEM** | 30 m (1 arc-sec) | Bhuvan Open Data Archive (ISRO), free registration | Best *Indian* source; tiled sheets, covers all eight NER states |
| **ASTER GDEM V003** | 30 m | NASA LP DAAC / Earthdata Search, DOI `10.5067/ASTER/ASTGTM.003` | **V003 is current**; V002 is superseded. Noisier than the above over steep terrain |

**Tile naming is not cosmetic.** `.hgt` files carry no georeference — the
filename *is* the georeference, and it names the **south-west** corner:

```
N26E091.hgt   ->  covers 26-27 N, 91-92 E      (Guwahati)
N24E092.hgt   ->  covers 24-25 N, 92-93 E      (Silchar)
S05W071.hgt   ->  covers 6-5 S, 72-71 W        (southern/western hemispheres)
```

The NER spans roughly `N21E089` … `N29E097` — about 70 tiles at 1°×1°.
A file named `download.hgt` or `dem_tile.hgt` is rejected with an explanation
rather than silently placed at 0,0.

**GeoTIFF → `.asc`** when you would rather not install rasterio (GDAL ships with
QGIS):

```bash
gdal_translate -of AAIGrid N26E091.tif N26E091.asc
gdal_merge.py -of AAIGrid -o ner_dem.asc N2*.hgt          # or mosaic first
```

Check any DEM before you trust it:

```bash
python data_ingestion.py --inspect data/raw/srtm
```

which prints per-tile extent, pixel size in metres, min/median/max elevation and
the **void fraction**. SRTM3 has systematic voids over steep, cloud-covered NER
terrain; a tile reporting 40 % voids is not usable as-is. Void cells degrade to
the mean of their valid neighbours rather than to −32768 m.

---

## 3. Landslide inventory — the label source

| Source | What it gives you | Access |
|:--|:--|:--|
| **GSI Bhukosh** (`bhukosh.gsi.gov.in/Bhukosh/Public`) | Landslide locations, dates, type, district; the National Landslide Susceptibility Map (NLSM) layers | Free, web portal. Bulk download is not offered as a clean CSV — expect to digitise/export map selections |
| **NGDR** (National Geological Data Repository, GSI) | The inventory behind NLSM: ~4.3 lakh km² mapped, ~91,000 landslides of which ~33,904 field-validated | Free registration; data on request |
| **State SDMA / DMAs, BRO, NHIDCL** | Closure logs and incident reports — the *road-disruption* label rather than the slope-failure label | Varies by state; often PDF |

**Any of these column spellings is recognised** (case, spaces, underscores and
unit suffixes are normalised before matching):

```
lat      <- Latitude | lat | LAT | y | latitude_deg | centroid_lat
lon      <- Longitude | lon | LONG | x | longitude_deg | centroid_lon
date     <- Event_Date | date | landslide_date | occurred_on | timestamp
district <- District | district_name | admin1
state    <- State | state_name | admin0
hazard   <- Landslide_Type | hazard_type | type | disaster_type
severity <- Severity | magnitude | impact
rainfall <- Rainfall_mm | rainfall | rain_mm | daily_rainfall | precipitation
id       <- Landslide_ID | Sl_No | id | event_id
```

GeoJSON Point features work too — properties are matched by the same table.

**Verify before you fuse:**

```bash
python data_ingestion.py --inspect data/raw/gsi_landslide_inventory.csv
```

```
  detected : lat<-Latitude, lon<-Longitude, date<-Event_Date, district<-District,
             hazard_type<-Landslide_Type, severity<-Severity, rainfall<-Rainfall_mm
  reads as : hazard inventory (GSI Bhukosh / NGDR / NLSM)
```

If `detected` is missing `lat`/`lon`/`date`, rename the columns — the loader
never guesses a coordinate column, because a wrong guess silently produces a
training set with no positives.

**Dates.** `%Y-%m-%d`, `%d/%m/%Y`, `%d-%m-%Y`, `%d.%m.%Y`, `%d %b %Y`,
`%Y%m%d` and ISO timestamps are all accepted. **Order matters:** `%d/%m/%Y` is
tried before `%m/%d/%Y`, so an ambiguous `07/05/2023` reads as 7 May — the
Indian convention. A US-style export must be normalised first or every ambiguous
date will be wrong.

**Known bias, stated plainly.** GSI inventories are dense where roads and
reporters are and sparse in unpopulated hills. The label therefore correlates
with accessibility, not only with hazard. The engine reports
`hist_freq_per_km` provenance so this can be audited; it does not pretend to
correct it.

---

## 4. Roads — OSM, NESAC or Bhuvan

### Overpass (no Python dependencies)

```
[out:json][timeout:180];
area["ISO3166-1"="IN"]->.ind;
(
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary)$"](area.ind);
);
out geom tags;
```

Narrow it to a state with `area["name"="Assam"]`, or to a bounding box with
`(24.5,91.5,27.0,94.5)`. `out geom;` produces the coordinate arrays the loader
wants; save the result as `data/raw/ner_roads.geojson`.

### OSMnx (if you have it)

```python
import osmnx as ox
G = ox.graph_from_bbox(bbox=(91.5, 24.5, 94.5, 27.0))   # order is W, S, E, N
edges = ox.convert.graph_to_gdfs(G, nodes=False)
edges.to_file("data/raw/ner_roads.geojson", driver="GeoJSON")
```

Two things trip people up here, both corrected above:

- `graph_from_bbox` takes `(west, south, east, north)`. Passing
  `(north, south, east, west)` returns an empty graph without error.
- The function is `ox.convert.graph_to_gdfs`. **`ox.graph_to_geojson` does not
  exist** in any version — write the GeoDataFrame with `to_file(driver="GeoJSON")`.

### Tags that are actually used

| Tag | Becomes | Default when absent |
|:--|:--|:--|
| `highway` | `road_class`, and `speed_kmh` via the class table | `unclassified`, 30 km/h |
| `maxspeed` | `speed_kmh` (clamped 12–100) | class default |
| `surface` | `surface` | `asphalt` |
| `lanes` | `lanes` | 2 |
| `cutting=yes` | `cut_slope=1` | inferred: `slope_deg ≥ 12` **and** `relief_m ≥ 25` |
| `embankment=yes` | `soil_type="alluvial"` | from SoilGrids texture |
| `ref` / `name` | `highway` when it starts `NH`/`SH` | `road_class` |
| `state`, `district` | join keys for rainfall | state from a bounding box, district empty |

`cutting` and `embankment` are rarely tagged in the NER — that is why the
slope-and-relief inference exists, and why the provenance report says which of
the two produced each `cut_slope`.

### The connectivity trap

`--inspect` on a road file reports connected components:

```
  loaded   : 129 segments / 130 nodes / 1185.5 km (99 length splits, 20 junction splits)
  topology : 2 connected component(s), largest 91 node(s)
  ! the network is FRAGMENTED: corridors crossing a component boundary will
    report NO PATH.
```

In real OSM, connectivity lives in shared **node IDs**. Rebuilt from coordinates,
any vertex appearing in two ways is a junction and must become a graph node —
otherwise a feeder road that meets a trunk mid-way looks connected in QGIS and
is a separate component after length-based splitting. The loader runs a junction
pre-pass and forces splits onto those vertices (`junction splits` above), so
T-junctions survive. Ways that merely *touch* geometrically without sharing a
vertex are still disconnected, and only snapping in QGIS (`Vector → Snapping
toolbar`) or an OSM fix will join them.

### City names

OSM ways carry no place names, so `resolve_endpoint("Guwahati")` finds nothing on
an ingested network and every named corridor reports `NO PATH`. The loader snaps
the engine's 28 NER city anchors onto the nearest junction within
`--city-radius` (default 5 km) and names it, reporting each snap distance and
listing cities the network never reaches. Nodes are never moved. Pass
`--no-city-names` to keep junctions anonymous and address corridors as
`"lat,lon"` instead.

---

## 5. Rainfall — IMD, NESAC, NEDFI

| Source | Granularity | Access |
|:--|:--|:--|
| **IMD Pune** (`imd.gov.in`) | District daily rainfall, gauge and satellite-merged | Published tables; bulk historical data on request |
| **NEDFI Databank** (`nedfi.in`) | District rainfall for the eight NER states | Free registration |
| **NESAC** (`nesac.gov.in`) | Gridded rainfall products for the NER | On request |
| **IMD AAWS / nowcast** | Hourly, 1 km | Live-ops feed, not historical |

Four layouts are auto-detected:

```csv
# long — district daily (the common IMD/NEDFI export)
date,district,state,rainfall_mm
2023-07-20,Kamrup Metropolitan,Assam,64.2
2023-07-20,East Khasi Hills,Meghalaya,118.0

# grid — lat/lon cells (NESAC, gridded IMD)
date,lat,lon,rainfall_mm
2023-07-20,26.125,91.750,64.2

# wide — one column per date
district,2023-07-20,2023-07-21
Kamrup Metropolitan,64.2,12.0

# monthly — district, year, month (NEDFI summaries)
district,year,month,rainfall_mm
Cachar,2023,7,842.5
```

`rain_mm_hr` is derived as `rain_24h / 24 × 3.2`, i.e. the daily total spread
over the day with a peak-intensity factor — the same convention the engine's own
generator uses. Sub-daily IMD data can be supplied directly via a
`rainfall_mm_hr` column.

`api_3d_mm` is the 3-day Antecedent Precipitation Index, an exponentially decayed
trailing sum with `k=0.85`, **including the observation date** (day 0). This
matches the engine's serve-time convention; excluding today would desynchronise
the ingested table from anything scored live.

**District names must match the roads.** A segment whose `district` is not in the
rainfall table falls back to that day's mean over all reported districts, and
every fallback is counted and reported:

```
  ! 412 rainfall lookups used the daily all-district mean because the segment's
    district name was not in the rainfall table - check spelling or pass
    district on the roads
```

A silent `0.0` there would look exactly like a genuine dry day and quietly poison
the training set. Normalise spelling (`Kamrup Metropolitan` vs `KAMRUP(M)`)
before fusing, or add the alias to the road properties.

---

## 6. Soil — ISRIC SoilGrids 2.0

**SoilGrids has no live soil-moisture product.** `wv0033` and `wv1500` are
water-*retention* curve points — field capacity (−33 kPa) and wilting point
(−1500 kPa). They are exactly what a bucket model needs to turn rainfall into a
saturation fraction, which is how `soil_saturation` is derived when no satellite
moisture is supplied. For observed moisture use §7.

### REST point query (what `--soil online` does)

```
https://rest.isric.org/soilgrids/v2.0/properties/query
    ?lon=91.736200&lat=26.144500
    &property=clay&property=sand&property=silt&property=soc
    &property=bdod&property=wv0033&property=wv1500&property=phh2o&property=cec
    &depth=0-5cm&value=mean
```

No API key. Responses are cached to `data/cache/soilgrids.json` and never
re-fetched, so a run is repeatable and offline-capable afterwards.

**Values are integer-encoded** — the loader decodes them:

| Property | Raw units | Divide by | Physical |
|:--|:--|:--|:--|
| `clay`, `sand`, `silt` | g/kg | 10 | % |
| `soc` | dg/kg | 10 | % |
| `bdod` | cg/cm³ | 100 | g/cm³ |
| `phh2o` | pH × 10 | 10 | pH |
| `cec` | mmol(c)/kg × 10 | 10 | mmol(c)/kg |
| `wv0033`, `wv1500`, `wv0010` | cm³/cm³ × 100 | 10 | vol % |

Depths run `0-5cm` … `100-200cm`; statistics are `Q0.05`, `Q0.5`, `Q0.95`,
`mean`. Resolution is 250 m.

### Bulk rasters (better for a whole region)

```
https://files.isric.org/soilgrids/latest/data/
    clay/clay_0-5cm_mean.tif
    bdod/bdod_0-5cm_mean.tif
    wv0033/wv0033_0-5cm_mean.tif
```

Clip to the NER in QGIS and either (a) convert to `.asc` and supply as
`--soil-csv` after sampling points, or (b) sample points along your road network
into a CSV:

```csv
lat,lon,clay,sand,silt,bdod,wv0033,wv1500,soc,phh2o
26.1445,91.7362,34.0,30.0,36.0,1.38,31.0,14.0,1.4,5.2
```

Pass it with `--soil-csv`. Values in that table must already be in physical
units, not SoilGrids integers.

### What is derived from it

```
soil_type      USDA textural triangle -> the engine's 7-class vocabulary
drainage       0 = free-draining pores ... 1 = impermeable/bedrock
porosity       n = 1 - bdod/2.65      (2.65 = quartz particle density, g/cm3)
field_capacity wv0033
wilting_point  wv1500
awc            field_capacity - wilting_point
```

Lookups are nearest-neighbour within `--soil-nearest-km` (default 1.0 km) when
the exact key is absent or the API fails. At 250 m resolution an exact
3-decimal key match is an arbitrary test: two points 40 m apart belong to the
same cell, while a 1 km segment centroid can fall outside the cell of either
endpoint. Set it to `0` to disable.

---

## 7. Live soil moisture — ERA5-Land or SMAP L4

This is what people usually mean by "soil moisture from SoilGrids", and it is
not there. Two real options:

### ERA5-Land (Copernicus CDS)

- Dataset `reanalysis-era5-land`, variable **`volumetric_soil_water_layer_1`**
- 0.1° (~11 km), hourly, four layers: 0–7, 7–28, 28–100, 100–289 cm
- Free CDS account; `cdsapi` Python client
- Known **wet bias** in the surface layer — acceptable for a relative feature,
  worth knowing before quoting absolute values

```python
import cdsapi
c = cdsapi.Client()
c.retrieve("reanalysis-era5-land", {
    "variable": "volumetric_soil_water_layer_1",
    "year": "2023", "month": ["06","07","08","09"],
    "day": [f"{d:02d}" for d in range(1, 31)],
    "time": ["12:00"],
    "area": [27.0, 89.0, 21.5, 97.5],      # N, W, S, E
    "format": "grib",
}, "era5land_sm.grib")
```

### SMAP L4 (NASA)

- 9 km EASE-grid, 3-hourly, surface (0–5 cm) **and root-zone (0–100 cm)**
- Continuous from 2015-03-31 — which is *after* much of the GSI inventory, so
  for older events you must fall back to the bucket model
- Lowest RMSE of the products commonly compared for the region
- NASA Earthdata login; AppEEARS for point/area extraction

### The layout the loader wants

```csv
date,lat,lon,volumetric_soil_water_layer_1
2023-07-20,26.125,91.750,0.3412
2023-07-20,26.125,91.875,0.3187
```

Any column whose name contains `soil_moisture`, `volumetric_soil_water`, `vswl`,
`soil_water`, `ssm` or `theta` is picked up, so a CDS or AppEEARS export needs no
renaming. Values must be **m³/m³** (0–1). Dates without a satellite overpass
fall back to the bucket model for that day.

`soil_saturation` is then the degree of saturation

```
(theta - theta_wilting) / (theta_porosity - theta_wilting)
```

with porosity from SoilGrids bulk density. Using **porosity** rather than field
capacity as the upper bound matters: against field capacity a monsoon soil sits
at or above 1.0 almost always and the feature collapses to a constant, carrying
no information. The bucket model reports saturation on the *same* scale, so a
segment whose moisture comes from the satellite on some days and from the bucket
on others is not feeding the model two different definitions of one feature.

---

## 8. NDVI (optional)

Sentinel-2 L2A or Landsat 8/9 surface reflectance → NDVI, via Copernicus
Browser, Google Earth Engine (`COPERNICUS/S2_SR_HARMONIZED`) or Bhuvan. Export a
monsoon-composite mean as `.asc` or GeoTIFF and pass `--ndvi`. Rasters scaled
×100 or ×1000 are detected and rescaled. Without it, `ndvi` stays at its
documented default and the provenance report lists it as imputed — it is the one
canonical feature with no fallback source in this repo.

---

## 9. The workflow

```bash
cd sih26002-hazard-engine

# 0. see the pipeline work with fabricated real-format inputs, no network
python data_ingestion.py --demo --train

# 1. put your downloads in data/raw/ and check each one before fusing
python data_ingestion.py --inspect data/raw/ner_roads.geojson
python data_ingestion.py --inspect data/raw/gsi_landslide_inventory.csv
python data_ingestion.py --inspect data/raw/imd_district_daily.csv
python data_ingestion.py --inspect data/raw/srtm

# 2. fuse them into one training table + one enriched graph
python data_ingestion.py \
    --roads      data/raw/ner_roads.geojson \
    --landslides data/raw/gsi_landslide_inventory.csv \
    --dem        data/raw/srtm \
    --rainfall   data/raw/imd_district_daily.csv \
    --moisture   data/raw/era5land_soil_moisture.csv \
    --soil       online \
    --year       2023 \
    --out        data/historical_hazards_2023.csv \
    --graph-out  data/ner_roads_enriched.geojson

# 3. read the provenance report before believing the model
python -c "import json;r=json.load(open('outputs/ingestion_report.json'));\
print('real   :',r['columns_from_real_data']);\
print('imputed:',r['columns_imputed'])"

# 4. retrain and route
python hazard_prediction_engine.py \
    --hazards data/historical_hazards_2023.csv \
    --graph   data/ner_roads_enriched.geojson \
    --retrain
```

`--train` on step 2 chains straight into step 4.

Outputs:

| File | Contents |
|:--|:--|
| `data/historical_hazards_<year>.csv` | the fused training table, in the engine's 29-column schema |
| `data/ner_roads_enriched.geojson` | the road graph with DEM, soil and OSM-derived attributes baked in, ready for `--graph` |
| `outputs/ingestion_report.json` | column-by-column provenance plus every source's parse statistics |

---

## 10. How labels are built — and why

Sampling dates and then hoping an event falls inside the window produces almost
no positives. The fusion is a **case-control** design instead:

- a **positive** row is created *at* each matched event date, so the weather on
  that row is the weather that actually accompanied the failure;
- **negative** rows are sampled from the season, excluding any date within
  `--window` days (default 3) of a known event on that segment;
- negative dates preferentially come from the wettest available days, which are
  the informative ones — a dry day with no landslide teaches the model little;
- `--max-positives` (default 6) caps positives per segment so one hotspot cannot
  dominate the fit.

`hist_freq_per_km` counts **only events strictly before** the row's own date,
normalised by segment length and by the number of seasons of inventory available
at that point. The usual `hist_freq = len(events_on_segment)` sketch leaks the
label directly and produces an AUC that will not survive contact with a new
monsoon. Undated events cannot be placed in time, so they are attributed to the
start of the inventory period — conservative, and counted separately in the
report.

The event→road join is a buffered spatial match (`--buffer`, default 100 m) over
a coarse bucket grid, indexed on **every polyline vertex** rather than segment
centroids: a 12 km segment whose centroid is far from an event can still pass
within metres of it. Inventories with poor positional accuracy usually need
`--buffer 250` to `--buffer 500`; the join rate is reported so you can see the
effect.

---

## 11. Corrections to widely-repeated claims

These appear in dataset guides for this region and are wrong:

| Claim | Reality |
|:--|:--|
| "SoilGrids gives soil moisture" | It gives **water-retention points** (`wv0033` = field capacity, `wv1500` = wilting point) and texture/chemistry. Static soil properties, not state. Moisture comes from ERA5-Land or SMAP |
| "SoilGrids has a saturated-hydraulic-conductivity layer" | SoilGrids 2.0 does not publish `ksat` through the REST API. Drainage here is a pedotransfer proxy from texture and bulk density |
| "`ox.graph_to_geojson(G)`" | Does not exist. Use `ox.convert.graph_to_gdfs` then `gdf.to_file(..., driver="GeoJSON")` |
| "`graph_from_bbox(north, south, east, west)`" | The order is `(west, south, east, north)` |
| "ASTER GDEM v2 is the current version" | **V003** is current (DOI `10.5067/ASTER/ASTGTM.003`); V002 is superseded. For India, Cartosat-1 DEM, NASADEM or Copernicus GLO-30 are all better choices |
| "Cartosat-1 DEM is 10 m" | The publicly distributed Cartosat-1 DEM is **30 m** (1 arc-sec). The 10 m / 2.5 m products are Cartosat-2S/3 stereo pairs and are not in the open archive |
| "SRTM tile `N26E091` covers 25–26 N" | It covers **26–27 N**: the name is the south-west corner |

---

## 12. When something goes wrong

| Symptom | Cause | Fix |
|:--|:--|:--|
| `0 positive` in the fusion log | Inventory and roads cover different areas, or dates fall outside `--year`'s season | Widen `--buffer`; check `--inspect` on both; verify the inventory's date range against `--year` |
| Most events unmatched | Buffer smaller than the inventory's positional accuracy | `--buffer 250` or `500` |
| `NO PATH` on every corridor | Fragmented network, or junctions unnamed | Run `--inspect` on the roads: it reports components. Snap geometries in QGIS. Check `city anchors` in the log |
| `rainfall lookups used the daily all-district mean` | District spelling differs between roads and rainfall table | Normalise spellings, or add `district` to the road properties |
| `cannot infer the tile corner from the file name` | `.hgt` not named after its south-west corner | Rename to `N26E091.hgt`, or export `.asc` |
| `reading GeoTIFF needs rasterio` | No rasterio installed | `pip install rasterio`, or `gdal_translate -of AAIGrid in.tif out.asc` |
| Soil all `laterite`, drainage 0.5 | No soil source reached | `--soil online`, or `--soil-csv`. The provenance report lists `soil_type`/`drainage` as imputed |
| `no DEM tiles found` | Directory holds no `.hgt`/`.asc`/`.tif` | Check the path; the error is deliberate — an empty mosaic would silently zero slope and elevation |

---

*Part of the SIH26002 hazard-prediction engine by Team ICONIC (119301).*
