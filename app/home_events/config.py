import logging
import os

import boto3

_ssm = None


def param(name):
    """Read a SecureString from SSM under PARAM_PREFIX. Fails loudly if unset."""
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    value = _ssm.get_parameter(
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
