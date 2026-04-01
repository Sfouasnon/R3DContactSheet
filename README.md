# R3DContactSheet

macOS-first RED `.R3D` still rendering and batch planning toolchain, built around a verified REDline IPP2 render path and aimed at multicamera array workflows.

## Current Status

- Verified working REDline single-frame R3D transcode path
- Confirmed correct IPP2 display-referred output using:
  `colorSciVersion=3`, `outputToneMap=1`, `rollOff=2`, `outputGammaCurve=32`
- Metadata-driven rendering now explicitly uses `--useMeta` so REDline applies actual clip look metadata
- REDline output validation now accepts emitted names such as `output.jpg.000000.jpg` and normalizes them back to the expected output path
- The desktop app now previews synchronized renders, outputs matched JPEG stills, and assembles a contact sheet PDF

## Desktop App

The current app is a same-day macOS delivery build focused on:

- REDline auto-detection with manual override and persisted path
- Persistent per-user settings stored in `~/Library/Application Support/R3DContactSheet/settings.json`
- One `Choose Source...` action that can target a single `.R3D`, an `.RDC` package, or a reel folder
- Output folder selection
- Metadata-driven sync preview with detected clip FPS, resolved absolute frame, resolved timecode, sync basis, and sync status
- Sequential batch planning, still rendering, and contact sheet PDF assembly
- Automatic metadata-based sync with optional advanced target-timecode override when production provides a specific target
- Logged commands, stdout/stderr capture, output existence checks, and per-job status
- A REDCINE-X / REDline update prompt when the installed REDline looks missing or too old for the verified IPP2 flags

## REDline Dependency

R3DContactSheet does not bundle REDline.

To preview metadata, render stills, or build the contact sheet PDF on another Mac, the user must already have REDCINE-X PRO installed so that REDline is available locally.

The app now:

- auto-detects REDline in common macOS REDCINE-X install locations
- allows a manual REDline path override
- persists that override in the per-user settings file
- blocks preview/render with a clear message when REDline is missing or too old

Typical expected REDline location on macOS:

```text
/Applications/REDCINE-X Professional/REDCINE-X PRO.app/Contents/MacOS/REDline
```

If REDline is not found, install REDCINE-X PRO first, then set the REDline path in the app and retry.

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

### Sharing Note

This repository is ready to share as source, and the built `.app` can now be shared with another Mac user as long as:

- REDCINE-X PRO / REDline is already installed on that machine
- the recipient points the app to their local REDline executable if auto-detection does not find it

REDline itself is an external dependency and is not included in this project or app bundle.

## Notes For Today

This iteration is intentionally centered on the core operator workflow:

- point the app at REDline
- choose a media source
- choose an output folder
- preview the synchronized clip plan
- confirm the resolved frame/timecode match across cameras
- build the contact sheet PDF with the verified IPP2 output recipe

### Source Selection Notes

- Visible build marker for this app bundle: `VERIFIED UI BUILD 2026-04-01-RDCFIX`
- `Choose Source...` scans a selected media location for `.RDC` packages and standalone `.R3D` files
- `.RDC` packages are treated as camera clip containers without requiring “Show Package Contents”
- When an `.RDC` contains multiple `.R3D` segments, the app uses the primary segment, preferring `_001`
- After selection, the app shows the source type, resolved clip count, grouping mode, and primary resolved clip in the source panel
- The sync area now resolves the matching moment automatically from clip metadata and promotes that timecode as the primary review value
- The preview table shows clip, group, FPS, resolved absolute frame, resolved timecode, sync basis, sync status, and output JPEG path
- The run step writes JPEG intermediates and assembles `r3dcontactsheet_contact_sheet.pdf` as a readable 12-up, multi-page PDF
- The app header now uses the approved R3DContactSheet logo with a compact sync-health indicator and system status
- The replay shell script is written to the chosen output folder as `r3dcontactsheet_last_batch.sh`

## What Remains Next

- LTC input beyond the current placeholder scaffold
- frame sync verification against a wider range of real RED metadata outputs
- smarter auto-batching for multicamera mini-arrays inside larger folder trees
- richer contact sheet layout/output controls
