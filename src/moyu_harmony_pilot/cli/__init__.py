"""L5: Agent-ready machine-first CLI with JSON envelopes, semantic exit codes, and schema."""

from moyu_harmony_pilot.cli.codes import EXIT_CODE_DESCRIPTIONS, ExitCode, classify_exception
from moyu_harmony_pilot.cli.envelope import (
    SCHEMA_VERSION,
    CliEnvelope,
    CliErrorDetail,
    error_envelope,
    ok_envelope,
)
from moyu_harmony_pilot.cli.main import build_parser, main
from moyu_harmony_pilot.cli.schema import build_cli_schema

__all__ = [
    "CliEnvelope",
    "CliErrorDetail",
    "EXIT_CODE_DESCRIPTIONS",
    "ExitCode",
    "SCHEMA_VERSION",
    "build_cli_schema",
    "build_parser",
    "classify_exception",
    "error_envelope",
    "main",
    "ok_envelope",
]
