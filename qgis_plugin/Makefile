PY ?= python3

.PHONY: package resources test compile clean
package:        ## copy schema.sql + rulesets into the plugin and build dist/wwnet-<ver>.zip
	$(PY) package.py
resources:
	$(PY) package.py --no-zip
test:
	$(PY) -m pytest tests -q
compile:
	$(PY) -m compileall -q wwnet
clean:
	rm -rf dist wwnet/resources/schema.sql wwnet/resources/*.toml
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
