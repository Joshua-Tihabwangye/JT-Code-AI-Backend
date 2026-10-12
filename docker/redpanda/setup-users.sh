#!/bin/sh
# Create the Kafka SCRAM users and their ACLs. Idempotent: runs on every
# `docker compose -f docker-compose.prod.yml up` once the broker is healthy.
#   jt-admin     superuser (bootstrap.yaml): creates and configures topics
#   jt-producer  API/workers/beat: write + describe on <prefix>.*, idempotent writes
#   jt-consumer  run_kafka_consumer: read + describe on <prefix>.*, read on its groups
set -eu
: "${KAFKA_ADMIN_PASSWORD:?}" "${KAFKA_PRODUCER_PASSWORD:?}" "${KAFKA_CONSUMER_PASSWORD:?}"
: "${KAFKA_TOPIC_PREFIX:?}" "${KAFKA_CONSUMER_GROUP_PREFIX:?}"

upsert_user() {
  rpk security user create "$1" -p "$2" --mechanism SCRAM-SHA-256 -X admin.hosts=kafka:9644 >/dev/null 2>&1 ||
    rpk security user update "$1" --new-password "$2" --mechanism SCRAM-SHA-256 -X admin.hosts=kafka:9644 >/dev/null
  echo "user $1 ready"
}

acl() {
  rpk security acl create "$@" -X brokers=kafka:9092 -X user=jt-admin -X pass="$KAFKA_ADMIN_PASSWORD" \
    -X sasl.mechanism=SCRAM-SHA-256 >/dev/null
}

upsert_user jt-admin "$KAFKA_ADMIN_PASSWORD"
upsert_user jt-producer "$KAFKA_PRODUCER_PASSWORD"
upsert_user jt-consumer "$KAFKA_CONSUMER_PASSWORD"

acl --allow-principal User:jt-producer --operation write,describe \
  --topic "$KAFKA_TOPIC_PREFIX." --resource-pattern-type prefixed
acl --allow-principal User:jt-producer --operation idempotent_write --cluster
acl --allow-principal User:jt-consumer --operation read,describe \
  --topic "$KAFKA_TOPIC_PREFIX." --resource-pattern-type prefixed
acl --allow-principal User:jt-consumer --operation read \
  --group "$KAFKA_CONSUMER_GROUP_PREFIX." --resource-pattern-type prefixed
echo "Kafka users and ACLs are in place."
