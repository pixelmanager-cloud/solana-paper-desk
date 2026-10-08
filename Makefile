PYTHON ?= python3

.PHONY: test demo
test:
	$(PYTHON) -m unittest discover -s tests -v

demo:
	$(PYTHON) -m desk replay --config config/paper.json --input fixtures/demo.jsonl --db var/demo.sqlite --report var/demo-report.json
