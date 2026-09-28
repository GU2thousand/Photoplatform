#!/bin/sh
set -eu
exec /app/start.sh java -jar /app/app.jar --migrate-only
