import logging
import os

_ssm = None


def ssm():
    """The SSM client, on its IPv6 endpoint so it works with the tunnel down."""
    global _ssm
    if _ssm is None:
        # Imported here so wireguard.py can run at first boot, before boto3 is.
        import boto3
        from botocore.config import Config
        _ssm = boto3.client("ssm", config=Config(use_dualstack_endpoint=True))
    return _ssm


def param(name):
    """Read a SecureString from SSM under PARAM_PREFIX. Fails loudly if unset."""
    value = ssm().get_parameter(
        Name=f"{os.environ['PARAM_PREFIX']}/{name}", WithDecryption=True
    )["Parameter"]["Value"]
    if value == "CHANGE_ME":
        # systemd restarts the service, so this retries until the value is set.
        raise SystemExit(f"SSM parameter {name} has not been set yet")
    return value


def setup_logging():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s: %(message)s",  # journald adds timestamps
    )
