#!/bin/sh
# Writes the broker's password file before mosquitto starts (compose service mqtt-init, mosquitto image).
#   warroom-app  password: $MQTT_APP_PASSWORD if set, else kept from ${AUTH}/app.pass, else 32 random chars.
#                app.pass is readable only by the app container user (uid 10001), mounted read-only into it.
#   gateway      created only when $MQTT_GATEWAY_PASSWORD is set (deploy/spark/.env); without it no
#                external client can publish at all.
# Idempotent: runs at every `docker compose up`.
set -eu
AUTH="${AUTH:-/mosquitto/auth}"
mkdir -p "$AUTH"
chmod 0711 "$AUTH"
umask 077
if [ -n "${MQTT_APP_PASSWORD:-}" ]; then
  printf '%s' "$MQTT_APP_PASSWORD" > "$AUTH/app.pass"
elif [ ! -s "$AUTH/app.pass" ]; then
  tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 > "$AUTH/app.pass"
fi
rm -f "$AUTH/passwd.new"
: > "$AUTH/passwd.new"
mosquitto_passwd -b "$AUTH/passwd.new" warroom-app "$(cat "$AUTH/app.pass")"
if [ -n "${MQTT_GATEWAY_PASSWORD:-}" ]; then
  if [ "${#MQTT_GATEWAY_PASSWORD}" -lt 12 ]; then
    echo "mqtt-init: MQTT_GATEWAY_PASSWORD must be 12+ characters" >&2
    exit 1
  fi
  mosquitto_passwd -b "$AUTH/passwd.new" gateway "$MQTT_GATEWAY_PASSWORD"
  echo "mqtt-init: users warroom-app, gateway"
else
  echo "mqtt-init: user warroom-app only (set MQTT_GATEWAY_PASSWORD in deploy/spark/.env to let the plant gateway publish)"
fi
cp /mosquitto/acl.src "$AUTH/acl"
chown 1883:1883 "$AUTH/passwd.new" "$AUTH/acl"
chmod 0600 "$AUTH/passwd.new" "$AUTH/acl"
mv "$AUTH/passwd.new" "$AUTH/passwd"
chown 10001 "$AUTH/app.pass"
chmod 0400 "$AUTH/app.pass"
