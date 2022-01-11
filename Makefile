.PHONY: all

all:
	mkdir -p dist
	poetry install
	poetry build
	shiv -c webserver -o dist/webserver --compile-pyc -p '/usr/bin/env python3' dist/twitch_fapi_backend-0.1.0.tar.gz

