"""
REDline wrapper utilities for deterministic single-frame JPEG renders.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from glob import glob
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence


class RedlineError(RuntimeError):
    """Base REDline wrapper error."""


class RedlineNotFoundError(RedlineError):
    """Raised when the REDline executable cannot be found."""


class RedlineExecutionError(RedlineError):
    """Raised when REDline returns a non-zero exit code."""


class RedlineOutputError(RedlineError):
    """Raised when REDline appears to succeed but output is missing/invalid."""


FORMAT_DPX = 0
FORMAT_TIFF = 1
FORMAT_OPENEXR = 2
FORMAT_JPEG = 3

RES_FULL = 1
RES_HALF_HIGH = 2
RES_HALF_NORMAL = 3
RES_QUARTER = 4
RES_EIGHTH = 8

GAMMA_BT1886 = 32

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

EXPECTED_HELP_FLAGS = ("--colorSciVersion", "--outputToneMap", "--rollOff", "--gammaCurve")


@dataclass(frozen=True)
class RedlinePaths:
    explicit_path: Optional[Path] = None
    env_var_name: str = "REDLINE_PATH"


@dataclass(frozen=True)
class RenderSettings:
    output_format: int = FORMAT_JPEG
    render_res: Optional[int] = RES_QUARTER
    resize_x: Optional[int] = None
    resize_y: Optional[int] = None
    fit: Optional[int] = None
    filter: Optional[int] = None
    use_meta: bool = True
    meta_ignore_frame_guide: bool = False
    gamma_curve: Optional[int] = None
    color_space: Optional[int] = None
    export_preset: Optional[str] = None
    preset_file: Optional[Path] = None
    no_rocket: bool = False
    single_rocket: bool = False
    color_sci_version: Optional[int] = COLOR_SCI_IPP2
    roll_off: Optional[int] = ROLLOFF_DEFAULT
    output_tone_map: Optional[int] = OUTPUT_TONEMAP_MEDIUM
    output_gamma_curve: Optional[int] = GAMMA_BT1886
    extra_args: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class RenderJob:
    input_file: Path
    frame_index: int
    output_file: Path
    settings: RenderSettings = field(default_factory=RenderSettings)
    overwrite: bool = True


@dataclass
class RenderResult:
    job: RenderJob
    command: List[str]
    returncode: int
    stdout: str
    stderr: str
    output_exists: bool
    output_size: int


@dataclass(frozen=True)
class RedlineProbe:
    executable: Optional[Path]
    available: bool
    compatible: bool
    message: str
    help_text: str = ""


def find_redline(paths: Optional[RedlinePaths] = None) -> Path:
    paths = paths or RedlinePaths()
    candidates: List[Path] = []

    if paths.explicit_path:
        candidates.append(Path(paths.explicit_path).expanduser())

    env_value = os.getenv(paths.env_var_name)
    if env_value:
        candidates.append(Path(env_value).expanduser())

    if sys.platform == "darwin":
        candidates.extend(_default_macos_redline_candidates())
    elif os.name == "nt":
        candidates.extend(
            [
                Path(r"C:\Program Files\REDCINE-X PRO 64-bit\REDline.exe"),
                Path(r"C:\Program Files\REDCINE-X PRO\REDline.exe"),
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
        "Could not find REDline. Install REDCINE-X PRO or choose the executable manually."
    )


def _default_macos_redline_candidates() -> List[Path]:
    static_candidates = [
        Path("/Applications/REDCINE-X Professional/REDCINE-X PRO.app/Contents/MacOS/REDline"),
        Path("/Applications/REDCINE-X Professional/REDCINE-X PRO.app/Contents/MacOS/REDLine"),
        Path("/Applications/REDCINE-X PRO.app/Contents/MacOS/REDline"),
        Path("/Applications/REDCINE-X PRO.app/Contents/MacOS/REDLine"),
        Path("/Applications/REDCINE-X PRO 64-bit.app/Contents/MacOS/REDline"),
        Path("/Applications/REDCINE-X PRO 64-bit.app/Contents/MacOS/REDLine"),
    ]
    discovered = []
    for pattern in (
        "/Applications/REDCINE-X*.app/Contents/MacOS/REDline",
        "/Applications/REDCINE-X*.app/Contents/MacOS/REDLine",
        str(Path.home() / "Applications" / "REDCINE-X*.app" / "Contents" / "MacOS" / "REDline"),
        str(Path.home() / "Applications" / "REDCINE-X*.app" / "Contents" / "MacOS" / "REDLine"),
    ):
        discovered.extend(Path(match) for match in glob(pattern))
    seen: set[Path] = set()
    ordered: List[Path] = []
    for candidate in [*static_candidates, *discovered]:
        resolved = candidate.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(resolved)
    return ordered


def probe_redline(paths: Optional[RedlinePaths] = None, timeout: float = 5.0) -> RedlineProbe:
    try:
        executable = find_redline(paths)
    except RedlineError as exc:
        return RedlineProbe(
            executable=None,
            available=False,
            compatible=False,
            message=(
                f"{exc} This machine likely needs a REDCINE-X / REDline install or update before rendering."
            ),
        )

    try:
        result = subprocess.run(
            [str(executable), "--help"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return RedlineProbe(
            executable=executable,
            available=True,
            compatible=False,
            message=(
                f"Found REDline at {executable}, but it could not be queried ({exc}). "
                "Please update REDCINE-X / REDline."
            ),
        )

    help_text = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    missing = [flag for flag in EXPECTED_HELP_FLAGS if flag not in help_text]
    if missing:
        return RedlineProbe(
            executable=executable,
            available=True,
            compatible=False,
            message=(
                f"Found REDline at {executable}, but it appears too old for the verified IPP2 render path. "
                "Please update REDCINE-X / REDline on this machine."
            ),
            help_text=help_text,
        )

    return RedlineProbe(
        executable=executable,
        available=True,
        compatible=True,
        message=f"REDline ready: {executable}",
        help_text=help_text,
    )


def build_redline_command(redline_exe: Path | str, job: RenderJob) -> List[str]:
    input_file = _abs_path(job.input_file)
    output_file = _abs_path(job.output_file)
    settings = job.settings

    _validate_frame_index(job.frame_index)
    _validate_output_parent(output_file)
    _validate_settings(settings)

    cmd: List[str] = [str(redline_exe)]
    cmd.extend(["--i", str(input_file)])
    cmd.extend(["--o", str(output_file)])
    cmd.extend(["--start", str(job.frame_index)])
    cmd.extend(["--frameCount", "1"])
    cmd.extend(["--format", str(settings.output_format)])

    if settings.render_res is not None:
        cmd.extend(["--res", str(settings.render_res)])
    if settings.resize_x is not None:
        cmd.extend(["--resizeX", str(settings.resize_x)])
    if settings.resize_y is not None:
        cmd.extend(["--resizeY", str(settings.resize_y)])
    if settings.fit is not None:
        cmd.extend(["--fit", str(settings.fit)])
    if settings.filter is not None:
        cmd.extend(["--filter", str(settings.filter)])

    if settings.use_meta:
        cmd.append("--useMeta")
    if settings.meta_ignore_frame_guide:
        cmd.append("--metaIgnoreFrameGuide")

    if not settings.use_meta:
        if settings.gamma_curve is not None:
            cmd.extend(["--gammaCurve", str(settings.gamma_curve)])
        if settings.color_space is not None:
            cmd.extend(["--colorSpace", str(settings.color_space)])

    if settings.color_sci_version is not None:
        cmd.extend(["--colorSciVersion", str(settings.color_sci_version)])
    if settings.roll_off is not None:
        cmd.extend(["--rollOff", str(settings.roll_off)])
    if settings.output_tone_map is not None:
        cmd.extend(["--outputToneMap", str(settings.output_tone_map)])
    if settings.output_gamma_curve is not None:
        cmd.extend(["--gammaCurve", str(settings.output_gamma_curve)])
    if settings.preset_file is not None:
        cmd.extend(["--presetFile", str(_abs_path(settings.preset_file))])
    if settings.export_preset:
        cmd.extend(["--exportPreset", settings.export_preset])
    if settings.no_rocket:
        cmd.append("--noRocket")
    if settings.single_rocket:
        cmd.append("--singleRocket")
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
    allow_nonzero_with_valid_output: bool = True,
) -> RenderResult:
    exe = Path(redline_exe) if redline_exe else find_redline(redline_paths)
    cmd = build_redline_command(exe, job)

    if job.output_file.exists() and not job.overwrite:
        raise RedlineOutputError(f"Output already exists: {job.output_file}")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )

    output_exists = job.output_file.exists()
    if not output_exists:
        emitted = _find_emitted_output(job.output_file)
        if emitted is not None:
            emitted.replace(job.output_file)
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
        valid_output = output_exists and output_size >= min_output_bytes
        if result.returncode != 0 and not (allow_nonzero_with_valid_output and valid_output):
            raise RedlineExecutionError(_format_failure(render_result))
        if not output_exists:
            raise RedlineOutputError(_format_missing_output(render_result))
        if output_size < min_output_bytes:
            raise RedlineOutputError(_format_tiny_output(render_result, min_output_bytes))

    return render_result


def write_batch_file(
    jobs: Sequence[RenderJob],
    destination: Path,
    redline_exe: Path | str | None = None,
    *,
    redline_paths: Optional[RedlinePaths] = None,
    windows: Optional[bool] = None,
) -> Path:
    exe = Path(redline_exe) if redline_exe else find_redline(redline_paths)
    destination = _abs_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if windows is None:
        windows = os.name == "nt"

    lines: List[str] = ["@echo off" if windows else "#!/usr/bin/env bash"]
    if windows:
        lines.append("setlocal")
    else:
        lines.append("set -euo pipefail")

    for job in jobs:
        lines.append(shell_join(build_redline_command(exe, job), windows=windows))

    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not windows:
        destination.chmod(destination.stat().st_mode | 0o111)
    return destination


def make_render_job(
    input_file: Path,
    frame_index: int,
    output_dir: Path,
    *,
    stem: Optional[str] = None,
    render_res: Optional[int] = RES_QUARTER,
    resize_x: Optional[int] = None,
    resize_y: Optional[int] = None,
    use_meta: bool = True,
    gamma_curve: Optional[int] = None,
    color_space: Optional[int] = None,
    color_sci_version: Optional[int] = COLOR_SCI_IPP2,
    roll_off: Optional[int] = ROLLOFF_DEFAULT,
    output_tone_map: Optional[int] = OUTPUT_TONEMAP_MEDIUM,
    output_gamma_curve: Optional[int] = GAMMA_BT1886,
    extra_args: Sequence[str] = (),
) -> RenderJob:
    input_file = _abs_path(input_file)
    output_dir = _abs_path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{stem or input_file.stem}.jpg"

    settings = RenderSettings(
        output_format=FORMAT_JPEG,
        render_res=render_res,
        resize_x=resize_x,
        resize_y=resize_y,
        use_meta=use_meta,
        gamma_curve=gamma_curve,
        color_space=color_space,
        color_sci_version=color_sci_version,
        roll_off=roll_off,
        output_tone_map=output_tone_map,
        output_gamma_curve=output_gamma_curve,
        extra_args=tuple(extra_args),
    )
    return RenderJob(
        input_file=input_file,
        frame_index=frame_index,
        output_file=output_file,
        settings=settings,
    )


def shell_join(parts: Sequence[str], *, windows: bool = False) -> str:
    if windows:
        return subprocess.list2cmdline(list(parts))
    return shlex.join(list(parts))


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
        raise ValueError(f"Unsupported output_format={settings.output_format}.")

    allowed_res = {None, RES_FULL, RES_HALF_HIGH, RES_HALF_NORMAL, RES_QUARTER, RES_EIGHTH}
    if settings.render_res not in allowed_res:
        raise ValueError(f"Unsupported render_res={settings.render_res}.")

    allowed_color_sci = {None, COLOR_SCI_CURRENT, COLOR_SCI_V1, COLOR_SCI_FLUT, COLOR_SCI_IPP2}
    if settings.color_sci_version not in allowed_color_sci:
        raise ValueError(f"Unsupported color_sci_version={settings.color_sci_version}.")

    allowed_rolloff = {None, ROLLOFF_NONE, ROLLOFF_HARD, ROLLOFF_DEFAULT, ROLLOFF_MEDIUM, ROLLOFF_SOFT}
    if settings.roll_off not in allowed_rolloff:
        raise ValueError(f"Unsupported roll_off={settings.roll_off}.")

    allowed_tonemap = {None, OUTPUT_TONEMAP_LOW, OUTPUT_TONEMAP_MEDIUM, OUTPUT_TONEMAP_HIGH, OUTPUT_TONEMAP_NONE}
    if settings.output_tone_map not in allowed_tonemap:
        raise ValueError(f"Unsupported output_tone_map={settings.output_tone_map}.")

    if settings.resize_x is not None and settings.resize_x <= 0:
        raise ValueError("resize_x must be > 0")
    if settings.resize_y is not None and settings.resize_y <= 0:
        raise ValueError("resize_y must be > 0")
    if settings.fit is not None and settings.fit <= 0:
        raise ValueError("fit must be > 0")
    if settings.filter is not None and settings.filter < 0:
        raise ValueError("filter must be >= 0")
    if not settings.use_meta and settings.meta_ignore_frame_guide:
        raise ValueError("meta_ignore_frame_guide requires use_meta=True")


def _format_failure(result: RenderResult) -> str:
    return (
        "REDline failed.\n"
        f"Command: {shell_join(result.command, windows=(os.name == 'nt'))}\n"
        f"Return code: {result.returncode}\n"
        f"STDOUT:\n{result.stdout.strip()}\n"
        f"STDERR:\n{result.stderr.strip()}\n"
    )


def _format_missing_output(result: RenderResult) -> str:
    return (
        "REDline completed without a usable output file.\n"
        f"Expected: {result.job.output_file}\n"
        f"Command: {shell_join(result.command, windows=(os.name == 'nt'))}\n"
        f"STDOUT:\n{result.stdout.strip()}\n"
        f"STDERR:\n{result.stderr.strip()}\n"
    )


def _format_tiny_output(result: RenderResult, min_output_bytes: int) -> str:
    return (
        "REDline output exists but is suspiciously small.\n"
        f"Output: {result.job.output_file}\n"
        f"Actual size: {result.output_size} bytes\n"
        f"Minimum expected size: {min_output_bytes} bytes\n"
        f"Command: {shell_join(result.command, windows=(os.name == 'nt'))}\n"
        f"STDOUT:\n{result.stdout.strip()}\n"
        f"STDERR:\n{result.stderr.strip()}\n"
    )


def _find_emitted_output(expected_output: Path) -> Optional[Path]:
    pattern = str(expected_output) + ".*"
    matches = [Path(path) for path in glob(pattern) if Path(path).is_file()]
    jpeg_matches = [path for path in matches if path.suffix.lower() in {".jpg", ".jpeg"}]
    candidates = jpeg_matches or matches
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Single-frame REDline smoke test")
    parser.add_argument("--input", required=True, help="Path to input .R3D")
    parser.add_argument("--output", required=True, help="Path to output still")
    parser.add_argument("--frame", required=True, type=int, help="Resolved frame index")
    parser.add_argument("--redline", default=None, help="Path to REDline executable")
    parser.add_argument("--resize-x", type=int, default=None)
    parser.add_argument("--resize-y", type=int, default=None)
    parser.add_argument("--res", type=int, default=RES_QUARTER)
    parser.add_argument("--no-meta", action="store_true")
    parser.add_argument("--gamma-curve", type=int, default=None)
    parser.add_argument("--color-space", type=int, default=None)
    parser.add_argument("--no-rocket", action="store_true")
    parser.add_argument("--single-rocket", action="store_true")
    parser.add_argument("--write-batch", default=None)
    parser.add_argument("--color-sci-version", type=int, default=COLOR_SCI_IPP2)
    parser.add_argument("--roll-off", type=int, default=ROLLOFF_DEFAULT)
    parser.add_argument("--output-tone-map", type=int, default=OUTPUT_TONEMAP_MEDIUM)
    parser.add_argument("--output-gamma-curve", type=int, default=GAMMA_BT1886)

    args = parser.parse_args(argv)
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
        output_gamma_curve=args.output_gamma_curve,
    )
    job = RenderJob(
        input_file=Path(args.input),
        frame_index=args.frame,
        output_file=Path(args.output),
        settings=settings,
    )

    if args.write_batch:
        batch_path = write_batch_file([job], Path(args.write_batch), redline_exe=args.redline)
        print(f"Wrote batch script: {batch_path}")

    result = render_frame(job, redline_exe=args.redline)
    print("Render succeeded.")
    print(f"Output: {result.job.output_file}")
    print(f"Size: {result.output_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
