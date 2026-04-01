# R3DContactSheet
A timecode triggered contact sheet created from ingested, then transcoded R3Ds. Redline processing to JPEG w/ IPP2 transforms. Output to PDF with auto-layout / scaling. 
- Verified working REDline single-frame R3D transcode path
- Confirmed correct IPP2 display-referred output using:
  colorSciVersion=3, outputToneMap=1, rollOff=2, outputGammaCurve=32
- Metadata-driven rendering remains default unless --no-meta is used
- Resolved flat/gray output issue by incorporating final display gamma
