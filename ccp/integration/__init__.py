"""CCP Integration — real input in, verified artifact out.

Sits between the application and the Product Engine. It accepts user input,
analyses it, drives the existing build path, verifies the result, and hands back
an artifact. It reconstructs nothing and reimplements nothing: the representation
is the Core's, and verification is the Product Engine's.
"""

from .errors import CancelledError, ExportError, InputError
from .pipeline import (
    BuildOutcome,
    BuildPipeline,
    CancellationToken,
    Progress,
    ProgressCallback,
    Stage,
)
from .sources import (
    EXCLUDED_DIRECTORIES,
    InputAnalysis,
    InputLimits,
    ProjectDirectorySource,
    ZipProjectSource,
    analyse_directory,
    analyse_input,
    analyse_zip,
    source_for,
)
from .workspace import (
    APP_NAME,
    RecentArtifacts,
    RecentEntry,
    app_data_dir,
    atomic_write,
    default_output_dir,
    ensure_dir,
    safe_export_path,
)

__all__ = [
    "APP_NAME",
    "BuildOutcome",
    "BuildPipeline",
    "CancellationToken",
    "CancelledError",
    "EXCLUDED_DIRECTORIES",
    "ExportError",
    "InputAnalysis",
    "InputError",
    "InputLimits",
    "Progress",
    "ProgressCallback",
    "ProjectDirectorySource",
    "RecentArtifacts",
    "RecentEntry",
    "Stage",
    "ZipProjectSource",
    "analyse_directory",
    "analyse_input",
    "analyse_zip",
    "app_data_dir",
    "atomic_write",
    "default_output_dir",
    "ensure_dir",
    "safe_export_path",
    "source_for",
]
