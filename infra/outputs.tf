output "instance_id" {
  value = aws_instance.box.id
}

output "region" {
  value = var.region
}

output "secret_parameters" {
  description = "Fill these in with `aws ssm put-parameter --overwrite --type SecureString`."
  value       = { for k, p in aws_ssm_parameter.secret : k => p.name }
}

output "data_volume_id" {
  value = aws_ebs_volume.data.id
}
