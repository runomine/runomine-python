#!/bin/sh
# Run the test suite inside a clean Python container without writing to the mounted source.
#   docker run --rm -v "$PWD":/src:ro python:3.12 sh /src/scripts/docker-test.sh
set -e
cp -r /src /app
cd /app
pip install -q --root-user-action=ignore --disable-pip-version-check -e '.[test]'
python -c "import sys, django, starlette, httpx; print(sys.version.split()[0], 'django', django.__version__, 'starlette', starlette.__version__, 'httpx', httpx.__version__)"
pytest -q "$@"
