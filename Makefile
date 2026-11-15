PYTHON ?= python

.PHONY: demo test collect report lint-size clean

demo:
	$(PYTHON) -m jobtrace demo

test:
	$(PYTHON) -m pytest -q

collect:
	$(PYTHON) -m jobtrace collect --with-mock --scenario baseline

report:
	$(PYTHON) -m jobtrace report

lint-size:
	$(PYTHON) -m jobtrace lint-size

clean:
	$(PYTHON) -c "import shutil, pathlib; shutil.rmtree('var', ignore_errors=True)"
