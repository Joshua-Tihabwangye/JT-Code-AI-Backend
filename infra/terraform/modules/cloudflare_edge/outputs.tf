output "hostnames" {
  value = [for record in cloudflare_record.hosts : record.hostname]
}
