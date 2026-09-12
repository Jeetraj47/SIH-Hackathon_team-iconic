# ---------------------------------------------------------------------------
#  SIH26002 Hazard Prediction Engine — convenience targets
#  Nothing here is required; every target is a plain python invocation.
# ---------------------------------------------------------------------------
PY    ?= python3
ARGS  ?=
F     ?= data/raw/ner_roads.geojson
STATE ?= Mizoram
INV   ?= data/raw/gsi_landslide_inventory.csv
ENGINE := hazard_prediction_engine.py
INGEST := data_ingestion.py

.DEFAULT_GOAL := help
.PHONY: help demo pure train scenarios benchmark test selftest info audit \
        ingest ingest-demo ingest-train ingest-fused ingest-live ingest-offline \
        inspect clean distclean

help:            ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

demo:            ## full pipeline: train -> score -> route -> emit artefacts
	$(PY) $(ENGINE) $(ARGS)

pure:            ## full pipeline using ONLY the Python standard library
	HPE_BACKEND=pure $(PY) $(ENGINE) --backend pure $(ARGS)

train:           ## retrain the model card (light scenario only, fastest)
	$(PY) $(ENGINE) --retrain --scenario light --no-benchmark $(ARGS)

scenarios:       ## score the 4 monsoon scenarios, emit re-weighted graphs
	$(PY) $(ENGINE) --scenario light,moderate,heavy,extreme --no-benchmark $(ARGS)

benchmark:       ## real-time SLA benchmark (<2 s per 1,000 edges)
	$(PY) $(ENGINE) --benchmark $(ARGS)

test:            ## unittest suite (engine + ingestion, 202 tests)
	$(PY) -m unittest discover -s tests -v

selftest:        ## built-in dependency-free self-test suite
	$(PY) $(ENGINE) --selftest $(ARGS)

info:            ## environment / backend capability report
	$(PY) $(ENGINE) --info $(ARGS)

audit:           ## model-vs-generator honesty audit (--audit-dgp)
	$(PY) $(ENGINE) --audit-dgp --regenerate-data $(ARGS)

ingest-demo:     ## fabricate real-format sources offline, fuse, write the table
	$(PY) $(INGEST) --demo $(ARGS)

ingest-train:    ## demo fusion + retrain the engine on the fused table
	$(PY) $(INGEST) --demo --train $(ARGS)

ingest-fused:    ## fuse YOUR data/raw downloads and retrain (see DATASETS.md)
	$(PY) $(INGEST) --roads data/raw/ner_roads.geojson \
	  --landslides data/raw/gsi_landslide_inventory.csv \
	  --dem data/raw/srtm --rainfall data/raw/imd_district_daily.csv \
	  --moisture data/raw/era5land_soil_moisture.csv --soil online --train $(ARGS)

ingest-live:     ## FETCH roads for STATE=$(STATE), fuse with INV, retrain
	$(PY) $(INGEST) --state $(STATE) --landslides $(INV) --train $(ARGS)

ingest-offline:  ## same, but never dial out - cached roads and soils only
	$(PY) $(INGEST) --state $(STATE) --landslides $(INV) --offline --train $(ARGS)

inspect:         ## dry-run one source file: format, mapping, gaps, connectivity
	$(PY) $(INGEST) --inspect $(F)

clean:           ## remove emitted artefacts (keeps the generated inputs)
	rm -rf outputs/* && mkdir -p outputs && touch outputs/.gitkeep

distclean: clean ## remove artefacts, generated inputs AND downloaded raw data
	rm -rf data/* && mkdir -p data && touch data/.gitkeep
