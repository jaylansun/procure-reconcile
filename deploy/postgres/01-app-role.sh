#!/bin/sh
set -eu
: "${PROCURE_APP_PASSWORD:?PROCURE_APP_PASSWORD is required}"
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 --set=app_password="$PROCURE_APP_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE procure_app LOGIN PASSWORD %L', :'app_password') \gexec
GRANT CONNECT ON DATABASE :"DBNAME" TO procure_app;
ALTER SCHEMA public OWNER TO procure_app;
SQL
