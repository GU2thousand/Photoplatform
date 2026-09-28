#!/bin/sh
set -eu
# CSI files are explicitly mapped; never eval secret contents or arbitrary names.
for name in APP_JWT_SECRET SPRING_DATASOURCE_URL SPRING_DATASOURCE_USERNAME SPRING_DATASOURCE_PASSWORD RABBITMQ_HOST RABBITMQ_USER RABBITMQ_PASSWORD ENCODER_TOKEN CDN_PRIVATE_KEY_PEM MIGRATOR_DATABASE_URL MIGRATOR_DATABASE_USERNAME MIGRATOR_DATABASE_PASSWORD STORAGE_ACCESS_KEY STORAGE_SECRET_KEY; do
  eval "path=\${${name}_FILE:-}"
  if [ -n "$path" ]; then
    [ -r "$path" ] && [ -s "$path" ] || { echo "Missing or empty secret file for $name" >&2; exit 1; }
    value=$(cat "$path")
    eval "direct=\${${name}:-}"
    [ -z "$direct" ] || [ "$direct" = "$value" ] || { echo "Conflicting direct/file secret for $name" >&2; exit 1; }
    export "$name=$value"
  fi
done
if [ "$#" -eq 0 ]; then set -- java -jar /app/app.jar; fi
exec "$@"
