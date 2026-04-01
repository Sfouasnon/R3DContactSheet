"""
redline.py

A production-oriented REDline wrapper for extracting single stills from R3D clips.

Design goals:
- deterministic command generation
- explicit executable discovery
- single-frame render via: --start <frame> --frameCount 1
- safe subprocess execution
- output validation
- optional batch file emission for debugging
- optional parallel rendering

This module assumes one output still per invocation.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence


# ----------------------------
# Exceptions
# ----------------------------

class RedlineError(RuntimeError):
    """Base REDline wrapper error."""


class RedlineNotFoundError(RedlineError):
    """Raised when the REDline executable cannot be found."""


class RedlineExecutionError(RedlineError):
    """Raised when REDline returns a non-zero exit code."""


class RedlineOutputError(RedlineError):
    """Raised when REDline appears to succeed but output is missing/invalid."""


# ----------------------------
# Enums / constants
# ----------------------------

# RED docs:
# --format 0=DPX, 1=TIFF, 2=OpenEXR, 3=JPEG, ...
FORMAT_DPX = 0
FORMAT_TIFF = 1
FORMAT_OPENEXR = 2
FORMAT_JPEG = 3

# RED docs:
# --res 1=Full, 2=Half high, 3=Half normal, 4=1/4, 8=1/8
RES_FULL = 1
RES_HALF_HIGH = 2
RES_HALF_NORMAL = 3
RES_QUARTER = 4
RES_EIGHTH = 8

# Common v42 color/gamma enum values
# gammaCurve
GAMMA_REC709 = 1
GAMMA_REDGAMMA4 = 30
GAMMA_LOG3G12 = 33

# colorSpace
COLORSPACE_REC709 = 1
COLORSPACE_REC709_ALT = 13
COLORSPACE_REDCOLOR4 = 22
COLORSPACE_REC2020 = 24

# Image Pipeline Settings
COLOR_SCI_CURRENT = 0
COLOR_SCI_V1 = 1
COLOR_SCI_FLUT = 2
COLOR_SCI_IPP2 = 3

ROLLOFF_NONE = 0
ROLLOFF_HARD = 1
ROLLOFF_DEFAULT = 2
ROLLOFF_MEDIUM = 3
ROLLOFF_SOFT = 4

OUTPUT_TONEMAP_LOW = 0
OUTPUT_TONEMAP_MEDIUM = 1
OUTPUT_TONEMAP_HIGH = 2
OUTPUT_TONEMAP_NONE = 3


# ----------------------------
# Dataclasses
# ----------------------------

@dataclass(frozen=True)
class RedlinePaths:
    """
    Executable discovery hints.

    Resolution order:
    1. explicit_path
    2. REDLINE_PATH env var
    3. common platform defaults
    4. PATH lookup
    """
    explicit_path: Optional[Path] = None
    env_var_name: str = "REDLINE_PATH"


@dataclass(frozen=True)
class RenderSettings:
    """
    Single-frame render settings.

    For deterministic contact-sheet work:
    - default to JPEG
    - default to metadata-driven look via use_meta=True
    - default to no overlays/burn-ins
    """
    output_format: int = FORMAT_JPEG
    render_res: Optional[int] = RES_QUARTER
    resize_x: Optional[int] = None
    resize_y: Optional[int] = None
    fit: Optional[int] = None
    filter: Optional[int] = None

    use_meta: bool = True
    meta_ignore_frame_guide: bool = False

    # Manual color overrides for REDline versions that use gammaCurve/colorSpace.
    # These must not be combined with use_meta=True.
    gamma_curve: Optional[int] = None
    color_space: Optional[int] = None

    # Optional preset loading if you need REDCINE preset parity.
    export_preset: Optional[str] = None
    preset_file: Optional[Path] = None

    # Rocket controls from RED examples.
    no_rocket: bool = False
    single_rocket: bool = False

    # Image pipeline controls. These are optional and work well with use_meta.
    color_sci_version: Optional[int] = None
    roll_off: Optional[int] = None
    output_tone_map: Optional[int] = OUTPUT_TONEMAP_MEDIUM

    # Extra args for future expansion or version quirks.
    extra_args: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class RenderJob:
    """
    One single-frame extraction job.
    """
    input_file: Path
    frame_index: int
    output_file: Path
    settings: RenderSettings = field(default_factory=RenderSettings)
    overwrite: bool = True


@dataclass
class RenderResult:
    """
    Execution result for one render job.
    """
    job: RenderJob
    command: List[str]
    returncode: int
    stdout: str
    stderr: str
    output_exists: bool
    output_size: int


# ----------------------------
# Public API
# ----------------------------

def find_redline(paths: Optional[RedlinePaths] = None) -> Path:
    """
    Locate the REDline executable.

    Raises:
        RedlineNotFoundError
    """
    paths = paths or RedlinePaths()

    candidates: List[Path] = []

    if paths.explicit_path:
        candidates.append(Path(paths.explicit_path))

    env_value = os.getenv(paths.env_var_name)
    if env_value:
        candidates.append(Path(env_value))

    if sys.platform == "darwin":
        candidates.extend(
            [
                Path("/Applications/REDCINE-X PRO.app/Contents/MacOS/REDline"),
                Path("/Applications/REDCINE-X PRO 64-bit.app/Contents/MacOS/REDline"),
                Path("/Applications/REDCINE-X Professional/REDCINE-X PRO.app/Contents/MacOS/REDline"),
            ]
        )
    elif os.name == "nt":
        candidates.extend(
            [
                Path(r"C:\Program Files\REDCINE-X PRO 64-bit\REDline.exe"),
                Path(r"C:\Program Files\REDCINE-X PRO\REDline.exe"),
                Path(r"C:\Program Files\RED\REDCINE-X PRO\REDline.exe"),
            ]
        )
    else:
        candidates.extend(
            [
                Path("~/REDline/REDline").expanduser(),
                Path("/usr/local/bin/REDline"),
                Path("/usr/bin/REDline"),
            ]
        )

    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate.resolve()

    which = shutil.which("REDline")
    if which:
        return Path(which).resolve()

    raise RedlineNotFoundError(
        "Could not find REDline. Supply an explicit path, set REDLINE_PATH, "
        "or install REDCINE-X PRO / standalone REDline."
    )


def build_redline_command(
    redline_exe: Path | str,
    job: RenderJob,
) -> List[str]:
    """
    Build a deterministic REDline command for a single-frame render.
    """
    input_file = _abs_path(job.input_file)
    output_file = _abs_path(job.output_file)
    settings = job.settings

    _validate_frame_index(job.frame_index)
    _validate_output_parent(output_file)
    _validate_settings(settings)

    cmd: List[str] = [str(redline_exe)]

    # File settings
    cmd.extend(["--i", str(input_file)])
    cmd.extend(["--o", str(output_file)])

    # Single-frame selection
    cmd.extend(["--start", str(job.frame_index)])
    cmd.extend(["--frameCount", "1"])

    # Format settings
    cmd.extend(["--format", str(settings.output_format)])

    if settings.render_res is not None:
        cmd.extend(["--res", str(settings.render_res)])

    # Crop/scale settings
    if settings.resize_x is not None:
        cmd.extend(["--resizeX", str(settings.resize_x)])
    if settings.resize_y is not None:
        cmd.extend(["--resizeY", str(settings.resize_y)])
    if settings.fit is not None:
        cmd.extend(["--fit", str(settings.fit)])
    if settings.filter is not None:
        cmd.extend(["--filter", str(settings.filter)])

    # Color pipeline
    if settings.use_meta:
        cmd.append("--useMeta")
        if settings.meta_ignore_frame_guide:
            cmd.append("--metaIgnoreFrameGuide")
    else:
        if settings.gamma_curve is not None:
            cmd.extend(["--gammaCurve", str(settings.gamma_curve)])
        if settings.color_space is not None:
            cmd.extend(["--colorSpace", str(settings.color_space)])

    # Image pipeline settings
    if settings.color_sci_version is not None:
        cmd.extend(["--colorSciVersion", str(settings.color_sci_version)])
    if settings.roll_off is not None:
        cmd.extend(["--rollOff", str(settings.roll_off)])
    if settings.output_tone_map is not None:
        cmd.extend(["--outputToneMap", str(settings.output_tone_map)])

    # Preset support
    if settings.preset_file is not None:
        cmd.extend(["--presetFile", str(_abs_path(settings.preset_file))])
    if settings.export_preset:
        cmd.extend(["--exportPreset", settings.export_preset])

    # Hardware toggles
    if settings.no_rocket:
        cmd.append("--noRocket")
    if settings.single_rocket:
        cmd.append("--singleRocket")

    # Extension point
    cmd.extend(settings.extra_args)

    return cmd


def render_frame(
    job: RenderJob,
    redline_exe: Path | str | None = None,
    *,
    redline_paths: Optional[RedlinePaths] = None,
    check: bool = True,
    timeout: Optional[float] = None,
    min_output_bytes: int = 1024,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
) -> RenderResult:
    """
    Execute one REDline single-frame render.

    Validates:
    - process exit code
    - output existence
    - output size threshold
    """
    exe = Path(redline_exe) if redline_exe else find_redline(redline_paths)
    cmd = build_redline_command(exe, job)

    if job.output_file.exists() and not job.overwrite:
        raise RedlineOutputError(f"Output already exists: {job.output_file}")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=env,
        check=False,
    )

    output_exists = job.output_file.exists()
    output_size = job.output_file.stat().st_size if output_exists else 0

    render_result = RenderResult(
        job=job,
        command=cmd,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        output_exists=output_exists,
        output_size=output_size,
    )

    if check:
        if result.returncode != 0:
            raise RedlineExecutionError(_format_failure(render_result))

        if not output_exists:
            raise RedlineOutputError(_format_missing_output(render_result))

        if output_size < min_output_bytes:
            raise RedlineOutputError(_format_tiny_output(render_result, min_output_bytes))

    return render_result


def render_many(
    jobs: Iterable[RenderJob],
    redline_exe: Path | str | None = None,
    *,
    redline_paths: Optional[RedlinePaths] = None,
    max_workers: int = 4,
    fail_fast: bool = True,
    timeout: Optional[float] = None,
    min_output_bytes: int = 1024,
) -> List[RenderResult]:
    """
    Render many jobs in parallel.
    """
    exe = Path(redline_exe) if redline_exe else find_redline(redline_paths)
    job_list = list(jobs)
    results: List[RenderResult] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(
                render_frame,
                job,
                exe,
                check=True,
                timeout=timeout,
                min_output_bytes=min_output_bytes,
            ): job
            for job in job_list
        }

        for future in as_completed(future_map):
            job = future_map[future]
            try:
                results.append(future.result())
            except Exception:
                if fail_fast:
                    raise
                results.append(
                    RenderResult(
                        job=job,
                        command=build_redline_command(exe, job),
                        returncode=-1,
                        stdout="",
                        stderr="Render failed; inspect exception/logs upstream.",
                        output_exists=job.output_file.exists(),
                        output_size=job.output_file.stat().st_size if job.output_file.exists() else 0,
                    )
                )

    order = {id(job): idx for idx, job in enumerate(job_list)}
    results.sort(key=lambda r: order[id(r.job)])
    return results


def write_batch_file(
    jobs: Iterable[RenderJob],
    destination: Path,
    redline_exe: Path | str | None = None,
    *,
    redline_paths: Optional[RedlinePaths] = None,
    windows: Optional[bool] = None,
) -> Path:
    """
    Emit a debug batch/shell script containing one REDline command per job.
    """
    exe = Path(redline_exe) if redline_exe else find_redline(redline_paths)
    destination = _abs_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if windows is None:
        windows = os.name == "nt"

    lines: List[str] = []

    if windows:
        lines.append("@echo off")
        lines.append("setlocal")
    else:
        lines.append("#!/usr/bin/env bash")
        lines.append("set -euo pipefail")

    for job in jobs:
        cmd = build_redline_command(exe, job)
        lines.append(shell_join(cmd, windows=windows))

    text = "\n".join(lines) + "\n"
    destination.write_text(text, encoding="utf-8")

    if not windows:
        destination.chmod(destination.stat().st_mode | 0o111)

    return destination


def shell_join(parts: Sequence[str], *, windows: bool = False) -> str:
    """
    Return a shell-safe joined command line.
    """
    if windows:
        return subprocess.list2cmdline(list(parts))
    return shlex.join(list(parts))


# ----------------------------
# Helpers
# ----------------------------

def make_contact_sheet_job(
    input_file: Path,
    frame_index: int,
    output_dir: Path,
    *,
    stem: Optional[str] = None,
    resize_x: Optional[int] = None,
    resize_y: Optional[int] = None,
    render_res: Optional[int] = RES_QUARTER,
    use_meta: bool = True,
    gamma_curve: Optional[int] = None,
    color_space: Optional[int] = None,
    extra_args: Sequence[str] = (),
    color_sci_version: Optional[int] = None,
    roll_off: Optional[int] = None,
    output_tone_map: Optional[int] = OUTPUT_TONEMAP_MEDIUM,
) -> RenderJob:
    """
    Convenience builder for typical contact-sheet stills.
    """
    input_file = _abs_path(input_file)
    output_dir = _abs_path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    clip_stem = stem or input_file.stem
    output_file = output_dir / f"{clip_stem}.jpg"

    settings = RenderSettings(
        output_format=FORMAT_JPEG,
        render_res=render_res,
        resize_x=resize_x,
        resize_y=resize_y,
        use_meta=use_meta,
        gamma_curve=gamma_curve,
        color_space=color_space,
        extra_args=tuple(extra_args),
        color_sci_version=color_sci_version,
        roll_off=roll_off,
        output_tone_map=output_tone_map,
    )
    return RenderJob(
        input_file=input_file,
        frame_index=frame_index,
        output_file=output_file,
        settings=settings,
    )


def _abs_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _validate_frame_index(frame_index: int) -> None:
    if not isinstance(frame_index, int):
        raise TypeError(f"frame_index must be int, got {type(frame_index).__name__}")
    if frame_index < 0:
        raise ValueError(f"frame_index must be >= 0, got {frame_index}")


def _validate_output_parent(output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)


def _validate_settings(settings: RenderSettings) -> None:
    allowed_formats = {FORMAT_DPX, FORMAT_TIFF, FORMAT_OPENEXR, FORMAT_JPEG}
    if settings.output_format not in allowed_formats:
        raise ValueError(
            f"Unsupported output_format={settings.output_format}. "
            f"Expected one of {sorted(allowed_formats)}."
        )

    allowed_res = {None, RES_FULL, RES_HALF_HIGH, RES_HALF_NORMAL, RES_QUARTER, RES_EIGHTH}
    if settings.render_res not in allowed_res:
        raise ValueError(
            f"Unsupported render_res={settings.render_res}. "
            f"Expected one of {sorted(x for x in allowed_res if x is not None)} or None."
        )

    allowed_color_sci = {None, COLOR_SCI_CURRENT, COLOR_SCI_V1, COLOR_SCI_FLUT, COLOR_SCI_IPP2}
    if settings.color_sci_version not in allowed_color_sci:
        raise ValueError(
            f"Unsupported color_sci_version={settings.color_sci_version}. "
            f"Expected one of {[x for x in allowed_color_sci if x is not None]} or None."
        )

    allowed_rolloff = {None, ROLLOFF_NONE, ROLLOFF_HARD, ROLLOFF_DEFAULT, ROLLOFF_MEDIUM, ROLLOFF_SOFT}
    if settings.roll_off not in allowed_rolloff:
        raise ValueError(
            f"Unsupported roll_off={settings.roll_off}. "
            f"Expected one of {[x for x in allowed_rolloff if x is not None]} or None."
        )

    allowed_tonemap = {None, OUTPUT_TONEMAP_LOW, OUTPUT_TONEMAP_MEDIUM, OUTPUT_TONEMAP_HIGH, OUTPUT_TONEMAP_NONE}
    if settings.output_tone_map not in allowed_tonemap:
        raise ValueError(
            f"Unsupported output_tone_map={settings.output_tone_map}. "
            f"Expected one of {[x for x in allowed_tonemap if x is not None]} or None."
        )

    if settings.resize_x is not None and settings.resize_x <= 0:
        raise ValueError("resize_x must be > 0")
    if settings.resize_y is not None and settings.resize_y <= 0:
        raise ValueError("resize_y must be > 0")
    if settings.fit is not None and settings.fit <= 0:
        raise ValueError("fit must be > 0")
    if settings.filter is not None and settings.filter < 0:
        raise ValueError("filter must be >= 0")

   if settings.use_meta:
    cmd.append("--useMeta")
    else:
    if settings.gamma_curve is not None:
        cmd.extend(["--gammaCurve", str(settings.gamma_curve)])
    if settings.color_space is not None:
        cmd.extend(["--colorSpace", str(settings.color_space)])

    if not settings.use_meta and settings.meta_ignore_frame_guide:
        raise ValueError("meta_ignore_frame_guide requires use_meta=True")


def _format_failure(result: RenderResult) -> str:
    cmd_text = shell_join(result.command, windows=(os.name == "nt"))
    return (
        "REDline failed.\n"
        f"Command: {cmd_text}\n"
        f"Return code: {result.returncode}\n"
        f"STDOUT:\n{result.stdout.strip()}\n"
        f"STDERR:\n{result.stderr.strip()}\n"
    )


def _format_missing_output(result: RenderResult) -> str:
    cmd_text = shell_join(result.command, windows=(os.name == "nt"))
    return (
        "REDline completed without a usable output file.\n"
        f"Expected: {result.job.output_file}\n"
        f"Command: {cmd_text}\n"
        f"STDOUT:\n{result.stdout.strip()}\n"
        f"STDERR:\n{result.stderr.strip()}\n"
    )


def _format_tiny_output(result: RenderResult, min_output_bytes: int) -> str:
    cmd_text = shell_join(result.command, windows=(os.name == "nt"))
    return (
        "REDline output exists but is suspiciously small.\n"
        f"Output: {result.job.output_file}\n"
        f"Actual size: {result.output_size} bytes\n"
        f"Minimum expected size: {min_output_bytes} bytes\n"
        f"Command: {cmd_text}\n"
        f"STDOUT:\n{result.stdout.strip()}\n"
        f"STDERR:\n{result.stderr.strip()}\n"
    )


# ----------------------------
# Optional smoke-test CLI
# ----------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Single-frame REDline smoke test")
    parser.add_argument("--input", required=True, help="Path to input .R3D")
    parser.add_argument("--output", required=True, help="Path to output still")
    parser.add_argument("--frame", required=True, type=int, help="Resolved frame index")
    parser.add_argument("--redline", default=None, help="Path to REDline executable")
    parser.add_argument("--resize-x", type=int, default=None)
    parser.add_argument("--resize-y", type=int, default=None)
    parser.add_argument("--res", type=int, default=RES_QUARTER)
    parser.add_argument("--no-meta", action="store_true")
    parser.add_argument("--gamma-curve", type=int, default=None, help="Manual gammaCurve override; requires --no-meta")
    parser.add_argument("--color-space", type=int, default=None, help="Manual colorSpace override; requires --no-meta")
    parser.add_argument("--no-rocket", action="store_true")
    parser.add_argument("--single-rocket", action="store_true")
    parser.add_argument("--write-batch", default=None, help="Optional path to write a debug batch/shell script")
    parser.add_argument("--color-sci-version", type=int, default=None, help="REDline color science version override")
    parser.add_argument("--roll-off", type=int, default=None, help="REDline highlight roll-off override")
    parser.add_argument("--output-tone-map", type=int, default=OUTPUT_TONEMAP_MEDIUM, help="REDline output tone map override")

    args = parser.parse_args()

    settings = RenderSettings(
        output_format=FORMAT_JPEG,
        render_res=args.res,
        resize_x=args.resize_x,
        resize_y=args.resize_y,
        use_meta=not args.no_meta,
        gamma_curve=args.gamma_curve,
        color_space=args.color_space,
        no_rocket=args.no_rocket,
        single_rocket=args.single_rocket,
        color_sci_version=args.color_sci_version,
        roll_off=args.roll_off,
        output_tone_map=args.output_tone_map,
    )

    job = RenderJob(
        input_file=Path(args.input),
        frame_index=args.frame,
        output_file=Path(args.output),
        settings=settings,
    )

    exe = Path(args.redline) if args.redline else None

    if args.write_batch:
        batch_path = write_batch_file([job], Path(args.write_batch), redline_exe=exe)
        print(f"Wrote batch script: {batch_path}")

    result = render_frame(job, redline_exe=exe)
    print("Render succeeded.")
    print(f"Output: {result.job.output_file}")
    print(f"Size: {result.output_size} bytes")