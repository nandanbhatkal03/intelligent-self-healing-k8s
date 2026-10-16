variable "cluster_name" {
  description = "Kind cluster name (kubectl context becomes kind-<name>)"
  type        = string
  default     = "selfheal"
}

variable "worker_count" {
  description = "Number of Kind worker nodes"
  type        = number
  default     = 2
}

variable "node_image" {
  description = "kindest/node image; null = the default for your kind provider version"
  type        = string
  default     = null
}

variable "kubeconfig_path" {
  description = "Kubeconfig the cluster credentials are merged into"
  type        = string
  default     = "~/.kube/config"
}

variable "install_chaos_mesh" {
  description = "Install Chaos Mesh (Objective 3 testbed)"
  type        = bool
  default     = true
}

# Chart version pins — null installs the latest. Pin after the first good
# apply (`helm list -A`) so evaluation runs are reproducible (NFR-11).
variable "chart_versions" {
  type = object({
    kube_prometheus_stack = optional(string)
    blackbox_exporter     = optional(string)
    loki                  = optional(string)
    otel_collector        = optional(string)
    chaos_mesh            = optional(string)
  })
  default = {}
}
