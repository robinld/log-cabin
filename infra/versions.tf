terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State is local by default. Keep terraform.tfstate out of git (see
  # .gitignore), or move it to an S3 backend.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = var.name
    }
  }
}
