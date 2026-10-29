variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-west-2"
}

variable "name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "log-cabin"
}

variable "instance_type" {
  description = "t4g.small is in the free trial (750 h/month) through 2026-12-31; t4g.nano is the cheap fallback afterwards."
  type        = string
  default     = "t4g.small"
}

variable "data_volume_gb" {
  description = "Size of the gp3 volume holding the SQLite database."
  type        = number
  default     = 2
}

variable "hourly_snapshots_kept" {
  description = "How many hourly snapshots of the data volume to keep."
  type        = number
  default     = 48
}

variable "daily_snapshots_kept" {
  description = "How many daily snapshots of the data volume to keep."
  type        = number
  default     = 14
}

variable "github_repo" {
  description = "Public GitHub repo (owner/name) the box downloads its code from."
  type        = string
  default     = "robinld/log-cabin"
}

variable "yolink_poll_minutes" {
  description = "How often to poll every YoLink device's state to catch anything MQTT missed."
  type        = number
  default     = 30
}
