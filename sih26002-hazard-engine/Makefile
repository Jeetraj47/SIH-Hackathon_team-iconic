# ---------------------------------------------------------------------------
#  SIH26002 Hazard Prediction Engine — convenience targets
#  Nothing here is required; every target is a plain python invocation.
# ---------------------------------------------------------------------------
PY   ?= python3
ARGS ?=
ENGINE := hazard_prediction_engine.py

.DEFAULT_GOAL := help
.PHONY: help demo pure train scenarios benchmark test selftest info audit clean distclean

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

test:            ## unittest suite (tests/test_engine.py)
	$(PY) -m unittest discover -s tests -v

selftest:        ## built-in dependency-free self-test suite
	$(PY) $(ENGINE) --selftest $(ARGS)

info:            ## environment / backend capability report
	$(PY) $(ENGINE) --info $(ARGS)

audit:           ## model-vs-generator honesty audit (--audit-dgp)
	$(PY) $(ENGINE) --audit-dgp --regenerate-data $(ARGS)

clean:           ## remove emitted artefacts (keeps the generated inputs)
	rm -rf outputs/* && mkdir -p outputs && touch outputs/.gitkeep

distclean: clean ## remove artefacts AND generated inputs
	rm -rf data/* && mkdir -p data && touch data/.gitkeep
