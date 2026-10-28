# CRAFTS TOD Manual Mask

中文完整操作说明见 [软件操作指南](docs/USER_GUIDE_CN.md)。
定标批次的一键入口见 [启动说明](rfi_calibration_launcher/README.md)。

This tool groups calibrated CRAFTS `*_T.fits` files by scan name and four-digit
file serial, requires M01--M19, and shows two polarization waterfalls. Each
beam's science-RFI masks (OneChannel-v2 OR science-eligible SAT1380) are applied
before the 19-beam mean. The FITS files are never modified.
The third panel shows the two polarization spectra after masking, calculated by
averaging the remaining valid records at every frequency.

The expected filename pattern is:

```text
<scan>_arcdrift-M<beam>_<window>_<serial>_T.fits
```

For example, the M01, M05, and M09 files below are one list entry:

```text
Dec+2654_22_09_arcdrift-M01_W_0007_T.fits
...
Dec+2654_22_09_arcdrift-M19_W_0007_T.fits
```

## Run

For server-side data with a local macOS window, start XQuartz on the Mac and
open a trusted X11 SSH connection:

```bash
open -a XQuartz
ssh -Y crafts
```

Or launch the validated configuration directly from the Mac clone:

```bash
bash launch_xquartz.sh Dec+2654_22_09:0002
```

For the validated `Dec+2654_22_09__20230716` server directory, use the dedicated
launcher. It prints the available groups before opening the requested nfile:

```bash
bash run_dec2654_server_ui.sh 2
```

For fast interactive work, use the native browser launcher. It keeps all FITS
reads and mask writes on the server and forwards only the local web port over
SSH; XQuartz is not used:

```bash
./run_dec2654_native_ui.sh 2 8997
```

The page opens at `http://127.0.0.1:8997/`. If that local/remote port is busy,
select another deterministic port, for example
`./run_dec2654_native_ui.sh 2 8998`. Keep the launcher terminal open and press
Ctrl-C there when finished so its SSH session and remote native-UI process
close.

Run the program on the server. Discovery is recursive, and mask directories are
inferred beside each stripe's FITS files, so the five-stripe campaign root can
be passed directly:

```bash
cd /data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/crafts_rfi_mask_manual_x11
export MPLBACKEND=TkAgg
python crafts_tod_visual_mask.py 'Dec+2654_22_09:0002' \
  --input-dir /data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/gzn_20260831/five_stripes_current_modules_0001_0010_v2 \
  --mask-dir /data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/manual_masks
```

Print the grouped list:

```bash
python crafts_tod_visual_mask.py --list
```

Open an entry using only its one-based list number:

```bash
python crafts_tod_visual_mask.py 1
```

Custom automatic-mask directories can be supplied explicitly:

```bash
python crafts_tod_visual_mask.py 2 --input-dir /path/to/stripe \
  --one-channel-dir /path/to/one_channel_rfi_v2_products \
  --sat1380-dir /path/to/satellite_rfi_1380_products \
  --require-all-auto-masks
```

## Controls

The controls below describe both implementations. The native browser UI is the
recommended interface; its complete workflow, Mean/Single authoring scopes,
interactive spectrum scaling, draggable colorbar handles, save confirmation,
and troubleshooting are documented in the
[Chinese user guide](docs/USER_GUIDE_CN.md).

- `Freq` or `f`: drag a frequency interval that applies to all records.
- `Time` or `t`: drag a record interval that applies to all frequencies.
- `Box` or `b`: drag a local time-frequency rectangle.
- `Delete` or `d`: remove mask items intersecting the dragged rectangle.
- `both pols`: apply the next edit to both polarizations; off by default.
- `apply auto RFI`: switch the displayed mean between raw 19-beam mean and the
  mean formed after per-beam OneChannel/SAT1380 exclusion.
- `show auto RFI`: show/hide the automatic RFI overlay. Its color represents
  how many of the 19 beams are flagged at each time-frequency-polarization cell.
- `Undo` or `u`: undo the last edit in the current entry.
- `Revert`: reload the last saved JSON mask.
- `Save` or `s`: save the JSON sidecar and mark the list entry `done`.
- `Prev`/`Next` or `p`/`n`: move between list entries after saving/reverting.
- `Pan` or `g`: enable left-drag frequency panning.
- Mouse wheel: zoom around the cursor.
- `Shift` + mouse wheel over a waterfall: zoom the record axis around the cursor;
  both polarization waterfalls remain synchronized.
- `vertical wheel` or `v`: reliable vertical-wheel mode for GUI backends that do
  not report the Shift modifier; while enabled, an ordinary wheel zooms records.
- `,`/`.`: move by half of the visible frequency window.
- `[`/`]`: move by 8 MHz.
- `0`: restore the full frequency range.
- `Contrast` or `c`: cycle through four robust display stretches.
- Right-drag: adjust both polarization color scales together. Horizontal motion
  shifts the color center; vertical motion changes the displayed dynamic range.
- `q`: close the window.

Red overlays are manual masks; the yellow/orange overlay is automatic RFI.
Seeded-RFI products are deliberately excluded because their pipeline role is
`FIT_ONLY`, not a science RFI mask. Edits in one panel affect only that
polarization unless `both pols` is checked.

## Output And Reuse

Save writes one authoritative, compact JSON edit record plus 19 compressed,
beam-specific NPZ products. Each NPZ contains only the dense manual mask
(`0=retain`, `1=manual RFI science-bad`), exact frequency/MJD axes, input beam,
nfile, review state, and JSON checksum. It deliberately does not copy or combine
OneChannel/SAT1380; downstream modules combine those independent products when
needed. A reviewed clean nfile still writes all-zero products with
`reviewed_clean=True`, which distinguishes it from a missing/unreviewed mask.

The mask can be expanded or applied in later processing without loading the GUI:

```python
from crafts_tod_mask import apply_mask, load_mask_document

document = load_mask_document(mask_json_path, group)
masked_tod = apply_mask(tod_time_frequency_pol, frequency_mhz, document)
```

Use `build_mask(...)` when a Boolean mask is preferred over NaN-filled data.

## Validation

Check that all beams in every group have matching time/frequency axes:

```bash
python crafts_tod_visual_mask.py --validate
```

Run the small mask-model tests:

```bash
python -m pytest -q test_crafts_tod_mask.py
```

## Update Log

### 2026-10-27 — Mask controls and session safety

- Configurable shortcuts and unified automatic-mask controls.
- Guard unsaved navigation and restart idle RFI sessions safely.
- Add the campaign launcher with a local configuration template.

### 2026-09-16 — Web viewport navigation and beam comparison

- Pointer-anchored zoom on the waterfalls and spectrum: `Option/Alt + wheel`
  zooms frequency, `Shift + wheel` zooms the record axis. `Option/Alt +
  left-drag`, `Shift + left-drag`, or both modifiers together enlarge a
  dragged range; the same tools work without modifiers via
  `显示设置 → 频率放大 / 时间放大`. Zoom gestures only change the viewing
  range and never edit the mask.
- `返回视图` returns to the previous viewing range and `重置视图 0` restores
  the full frequency and record range; both are view-only and do not undo
  manual edits.
- While navigating, the page previews the cached image locally and merges
  consecutive requests into the latest viewport. Marking and deleting wait
  for the final `清晰视图` image, preventing edits against a stale view.
- In Single-beam mode the spectrum draws the current beam as a solid line and
  keeps the 19-beam Mean as a dashed reference at 48% opacity. The reference
  remains `global_manual_only`: per-beam manual regions are not merged into
  it, and the automatic-RFI switch applies to both curves. Spectrum drawing
  preserves the per-pixel extrema and NaN gaps.
- Adjustable layout: `显示设置 → 频谱高度` sets the spectrum share (default
  24%, range 18%–40%; the two waterfalls stay equal in height). The
  status/log sidebar can be resized via its divider or collapsed from the
  top bar; these preferences persist in the browser.
- The automatic-RFI overlay pooling now reduces records before channels,
  cutting single-view render time by about 23%–35% on CRAFTS-sized inputs.
