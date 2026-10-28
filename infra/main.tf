data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  param_prefix = "/${var.name}"

  # Secrets are created with a placeholder and filled in out-of-band with
  # `aws ssm put-parameter --overwrite` so real values never touch state.
  secret_params = {
    yolink_uaid    = "yolink/uaid"
    yolink_secret  = "yolink/secret"
    wireguard_conf = "wireguard/conf"
    evl_host       = "evl/host"
    evl_password   = "evl/password"
  }
}

# ---------------------------------------------------------------- network ---

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# No ingress rules at all. Shell and dashboard access both go through SSM
# Session Manager, whose agent dials out.
resource "aws_security_group" "box" {
  name        = "${var.name}-box"
  description = "Egress only"
  vpc_id      = data.aws_vpc.default.id

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------------------------------------------------------- secrets ---

resource "aws_ssm_parameter" "secret" {
  for_each = local.secret_params

  name  = "${local.param_prefix}/${each.value}"
  type  = "SecureString"
  value = "CHANGE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

# ------------------------------------------------------------ instance IAM ---

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "box" {
  name               = "${var.name}-box"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.box.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "box" {
  statement {
    sid       = "ReadOwnParams"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.param_prefix}/*"]
  }
}

resource "aws_iam_role_policy" "box" {
  name   = "${var.name}-box"
  role   = aws_iam_role.box.id
  policy = data.aws_iam_policy_document.box.json
}

resource "aws_iam_instance_profile" "box" {
  name = "${var.name}-box"
  role = aws_iam_role.box.name
}

# --------------------------------------------------------------- instance ---

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_instance" "box" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
  instance_type          = var.instance_type
  subnet_id              = sort(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids = [aws_security_group.box.id]
  iam_instance_profile   = aws_iam_instance_profile.box.name

  # No NAT gateway (~$33/mo), so the box needs a public IPv4 for egress
  # (~$3.60/mo). The security group still allows no inbound traffic.
  associate_public_ip_address = true

  # "unlimited" (the T4g default) bills surplus CPU credits; "standard" just
  # throttles. This workload idles, so throttling is the safer failure.
  credit_specification {
    cpu_credits = "standard"
  }

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region         = var.region
    data_volume_id = aws_ebs_volume.data.id
    github_repo    = var.github_repo
    param_prefix   = local.param_prefix
    poll_minutes   = var.yolink_poll_minutes
    hostname       = var.name
  })

  tags = {
    Name = var.name
  }

  # The root volume is disposable; new AMIs or bootstrap edits should not
  # silently replace the box. Code updates go through scripts/deploy.sh.
  lifecycle {
    ignore_changes = [ami, user_data]
  }
}

# The database lives on its own volume so the instance can be replaced
# without touching data.
resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.box.availability_zone
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true

  tags = {
    Name     = "${var.name}-data"
    Snapshot = "${var.name}-data"
  }

  lifecycle {
    prevent_destroy = true
  }
}

data "aws_subnet" "box" {
  id = sort(data.aws_subnets.default.ids)[0]
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.box.id
}

# ------------------------------------------------------------- snapshots ---

data "aws_iam_policy_document" "dlm_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["dlm.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dlm" {
  name               = "${var.name}-dlm"
  assume_role_policy = data.aws_iam_policy_document.dlm_assume.json
}

resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

# SQLite in WAL mode survives power loss, and an EBS snapshot is the same
# state as a power cut, so these snapshots restore cleanly.
resource "aws_dlm_lifecycle_policy" "data" {
  description        = "${var.name} data volume snapshots"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { Snapshot = "${var.name}-data" }

    schedule {
      name = "hourly"
      create_rule {
        interval      = 1
        interval_unit = "HOURS"
      }
      retain_rule {
        count = var.hourly_snapshots_kept
      }
      copy_tags = true
    }

    schedule {
      name = "daily"
      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["10:00"] # UTC
      }
      retain_rule {
        count = var.daily_snapshots_kept
      }
      copy_tags = true
    }
  }
}
