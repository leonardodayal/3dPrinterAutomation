#!/bin/sh
# Start (or restart) the local Obico ML detection API used by printloop.
#   build once:  docker build -t printloop/obico-ml-api:local tools/obico-server/ml_api
#   then:        scripts/obico-ml.sh
# Listens on 127.0.0.1:3333 only. Reads OBICO_ML_TOKEN from .env.
set -e
cd "$(dirname "$0")/.."
TOKEN=$(grep '^OBICO_ML_TOKEN=' .env | cut -d= -f2- | cut -d' ' -f1)
[ -n "$TOKEN" ] || { echo "Set OBICO_ML_TOKEN in .env"; exit 1; }
docker rm -f obico-ml >/dev/null 2>&1 || true
docker run -d --name obico-ml --restart unless-stopped \
  -p 127.0.0.1:3333:3333 \
  -e ML_API_TOKEN="$TOKEN" -e FLASK_APP=server.py \
  printloop/obico-ml-api:local \
  bash -c "gunicorn --bind 0.0.0.0:3333 --workers 1 --timeout 120 wsgi"
echo "Started obico-ml. Health: curl http://127.0.0.1:3333/hc/"
