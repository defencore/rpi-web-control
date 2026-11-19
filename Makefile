PYTHON ?= python3

.PHONY: deb verify-deb check test
deb:
	$(PYTHON) packaging/build.py

verify-deb:
	$(PYTHON) packaging/build.py --verify

check test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
