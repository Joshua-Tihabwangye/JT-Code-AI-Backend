# The upstream collector image is FROM scratch (no shell, no HTTP client), so a Docker health
# check could not reach its health_check extension (:13133). Add one static busybox binary.
FROM busybox:1.37.0-musl AS probe

FROM otel/opentelemetry-collector-contrib:0.112.0
COPY --from=probe /bin/busybox /busybox
