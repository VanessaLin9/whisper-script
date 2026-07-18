"""Meeting Workspace / Output Manager.

Plans and creates per-meeting workspaces, applies source ownership policy, and
centralizes default transcript artifacts. Does not download, run Whisper, or
own UI prompts.
"""

from .artifacts_policy import (
    DEFAULT_ARTIFACTS,
    OPT_IN_ARTIFACTS,
    default_outputs_arg,
    resolve_outputs,
)
from .types import (
    MeetingWorkspace,
    SourceDescriptor,
    SourceKind,
    WorkspaceError,
    WorkspacePlan,
    WorkspaceStage,
)
from .workspace import (
    assert_no_plan_conflicts,
    assert_source_readable,
    create_workspace,
    plan_workspace,
    prepare_local_workspace,
)

__all__ = [
    "DEFAULT_ARTIFACTS",
    "OPT_IN_ARTIFACTS",
    "MeetingWorkspace",
    "SourceDescriptor",
    "SourceKind",
    "WorkspaceError",
    "WorkspacePlan",
    "WorkspaceStage",
    "assert_no_plan_conflicts",
    "assert_source_readable",
    "create_workspace",
    "default_outputs_arg",
    "plan_workspace",
    "prepare_local_workspace",
    "resolve_outputs",
]
