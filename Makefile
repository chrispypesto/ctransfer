.PHONY: test

PYTHON ?= python

test:
	$(PYTHON) -m pytest
