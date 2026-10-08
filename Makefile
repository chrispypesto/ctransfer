.PHONY: install test

PYTHON ?= python

install:
	$(PYTHON) -m pip install .

test:
	$(PYTHON) -m pytest
