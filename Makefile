.PHONY: install install-plugin test

PYTHON ?= python
CLAUDE ?= claude

install:
	$(PYTHON) -m pip install .

# Point the ctransfer marketplace at this checkout and install the plugin from it.
# Safe to rerun; the plugin loads in place, so later edits need only /reload-plugins.
install-plugin:
	$(CLAUDE) plugin marketplace add $(CURDIR)
	$(CLAUDE) plugin install ctransfer@ctransfer

test:
	$(PYTHON) -m pytest
