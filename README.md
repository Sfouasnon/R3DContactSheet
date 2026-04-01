# R3DContactSheet

macOS-first RED `.R3D` still rendering and batch planning toolchain, built around a verified REDline IPP2 render path and aimed at multicamera array workflows.

## Current Status

- Verified working REDline single-frame R3D transcode path
- Confirmed correct IPP2 display-referred output using:
  `colorSciVersion=3`, `outputToneMap=1`, `rollOff=2`, `outputGammaCurve=32`
- Metadata-driven rendering is the default path in the wrapper
- The wrapper intentionally does not pass `--useMeta` for the verified default render path
- The new desktop app batches one still render per clip and prepares the project for LTC and contact-sheet follow-on work

## Desktop App

The current app is a same-day macOS delivery build focused on:

- REDline auto-detection with manual override and persisted path
- Single clip or recursive reel-folder selection
- Output folder selection
- Sequential batch planning and rendering
- Frame index targeting now, with timecode scaffolding ready for the next LTC step
- Logged commands, stdout/stderr capture, output existence checks, and per-job status
- A REDCINE-X / REDline update prompt when the installed REDline looks missing or too old for the verified IPP2 flags

Run it locally with:

```bash
python3 app.py
```

Or run the backend smoke-test CLI with:

```bash
python3 redline.py \
  --input "/path/to/clip.R3D" \
  --output "/path/to/output.jpg" \
  --frame 0 \
  --redline "/Applications/REDCINE-X Professional/REDCINE-X PRO.app/Contents/MacOS/REDline"
```

## Project Layout

```text
r3dcontactsheet/
  app.py
  batch.py
  frame_index.py
  ltc.py
  redline.py
  settings.py
  timecode.py
packaging/
tests/
app.py
redline.py
setup.py
```

## Packaging For macOS

Tested build path:

```bash
python3 -m pip install -r requirements.txt
python3 -m PyInstaller -y --windowed --name "R3D Contact Sheet" app.py
```

Recommended one-step build command:

```bash
./packaging/build_mac_app.sh
```

Expected deliverable:

```text
dist/R3D Contact Sheet.app
```

An alternate `py2app` setup file is also included in `setup.py`, but the verified same-day build path in this repository is currently PyInstaller.

## Notes For Today

This iteration is intentionally centered on the core operator workflow:

- point the app at REDline
- choose a clip or reel
- choose an output folder
- choose a frame index or target timecode
- preview the planned renders
- render JPEG stills with the verified IPP2 output recipe

## What Remains Next

- LTC input beyond the current placeholder scaffold
- frame index analyzer refinement using actual clip metadata
- smarter auto-batching for multicamera mini-arrays inside larger folder trees
- contact sheet builder and PDF layout/output controls
