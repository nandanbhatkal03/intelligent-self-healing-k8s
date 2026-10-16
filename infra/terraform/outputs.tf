output "kube_context" {
  value       = "kind-${kind_cluster.this.name}"
  description = "kubectl context for the cluster"
}

output "next_steps" {
  value = <<-EOT
    Cluster and platform services are up. Next:
      make app            # build + deploy ShopLite, load generator, alerts, RBAC
      make port-forward   # Grafana http://localhost:3000 (admin / selfheal)
      make smoke
  EOT
}
