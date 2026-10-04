# Cloudflare edge policy

Terraform for the API hostname's TLS settings, WAF (Cloudflare Managed and OWASP
Core rulesets plus custom path, method and size rules), edge rate limits, the
no-cache policy for `/api/`, and the origin lock.

## Origin lock

The `origin_auth` ruleset adds `X-JT-Origin-Auth: <secret>` to every proxied
request. Set the same value as `CLOUDFLARE_ORIGIN_SECRET` and
`CLOUDFLARE_ENFORCE_ORIGIN=true` on the API. Django then:

* rejects requests that do not carry the header with 403 (health probes are
  exempt, so the load balancer can still check the pods);
* trusts `CF-Connecting-IP` as the client address only on verified requests.

Behind a further load balancer, also set `TRUSTED_PROXY_HOPS`. Keep the origin
firewall limited to Cloudflare's IP ranges as defence in depth.

## Apply

```bash
cd infra/cloudflare
terraform init
terraform plan -var-file=production.tfvars   # never commit *.tfvars
terraform apply -var-file=production.tfvars
```

`bot_management_enabled` needs Cloudflare Bot Management. The managed ruleset
IDs are Cloudflare's published constants for the Managed and OWASP rulesets.
