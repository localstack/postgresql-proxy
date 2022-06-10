VENV_DIR ?= .venv
VENV_RUN = . $(VENV_DIR)/bin/activate
PIP_CMD ?= pip

usage:             ## Show this help
	@fgrep -h "##" $(MAKEFILE_LIST) | fgrep -v fgrep | sed -e 's/\\$$//' | sed -e 's/##//'

install:           ## Install dependencies in local virtualenv folder
	(test `which virtualenv` || $(PIP_CMD) install --user virtualenv) && \
		(test -e $(VENV_DIR) || virtualenv $(VENV_OPTS) $(VENV_DIR)) && \
		($(VENV_RUN) && $(PIP_CMD) install --upgrade pip) && \
		(test ! -e requirements.txt || ($(VENV_RUN); $(PIP_CMD) install -r requirements.txt))

publish:           ## Publish the library to the central PyPi repository
	($(VENV_RUN); pip install twine; python ./setup.py sdist && twine upload dist/*)

.PHONY: usage install clean publish test lint
