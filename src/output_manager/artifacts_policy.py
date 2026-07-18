"""Central default artifact policy for all callers (shell / CLI / future GUI)."""

from __future__ import annotations

from src.transcription.types import ArtifactKind

# Default meeting outputs. VTT is opt-in only.
DEFAULT_ARTIFACTS: frozenset[ArtifactKind] = frozenset(
    {
        ArtifactKind.TXT,
        ArtifactKind.SRT,
        ArtifactKind.JSON,
    }
)

OPT_IN_ARTIFACTS: frozenset[ArtifactKind] = frozenset({ArtifactKind.VTT})


def resolve_outputs(requested: frozenset[ArtifactKind] | None) -> frozenset[ArtifactKind]:
    """Return the caller's subset, or the shared defaults when omitted."""
    if requested is None:
        return DEFAULT_ARTIFACTS
    if not requested:
        raise ValueError("outputs must contain at least one artifact kind")
    return frozenset(requested)


def default_outputs_arg() -> str:
    """Comma-separated default list for CLI/shell ``--outputs``."""
    return ",".join(kind.value for kind in sorted(DEFAULT_ARTIFACTS, key=lambda k: k.value))
