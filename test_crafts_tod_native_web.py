from io import BytesIO
from pathlib import Path
import threading

import numpy as np
from PIL import Image

from crafts_tod_mask import MaskDocument, PolarizationMask
from crafts_tod_native_web import INVALID_RGB, NativeMaskState, _mask_pool, render_view_image


def test_render_view_image_is_bounded_and_valid():
    values = np.arange(40 * 80, dtype=np.float32).reshape(40, 80)
    values[0, 0] = np.nan
    mask = np.zeros((40, 80), dtype=np.uint8)
    mask[7, 11] = 19
    payload, low, high, auto_low, auto_high = render_view_image(
        values,
        mask,
        y0=0,
        y1=40,
        x0=0,
        x1=80,
        width=32,
        height=20,
        contrast=0,
    )
    image = Image.open(BytesIO(payload))
    assert image.format == "JPEG"
    assert image.size == (32, 20)
    assert low < high
    assert (low, high) == (auto_low, auto_high)


def test_render_view_image_accepts_custom_color_limits():
    values = np.arange(100, dtype=np.float32).reshape(10, 10)
    payload, low, high, auto_low, auto_high = render_view_image(
        values,
        None,
        y0=0,
        y1=10,
        x0=0,
        x1=10,
        width=10,
        height=10,
        contrast=0,
        vmin=20.0,
        vmax=40.0,
    )
    assert Image.open(BytesIO(payload)).size == (10, 10)
    assert (low, high) == (20.0, 40.0)
    assert auto_low < low < high < auto_high


def test_render_view_image_clamps_requested_size():
    values = np.ones((3, 4), dtype=np.float32)
    image = Image.open(
        BytesIO(
            render_view_image(
                values,
                None,
                y0=0,
                y1=3,
                x0=0,
                x1=4,
                width=10,
                height=10,
                contrast=3,
            )[0]
        )
    )
    assert image.size == (4, 3)


def test_render_view_image_shows_nan_as_neutral_grey():
    values = np.linspace(-1.0, 1.0, 20 * 20, dtype=np.float32).reshape(20, 20)
    values[5:15, 5:15] = np.nan
    payload = render_view_image(
        values,
        np.full((20, 20), 19, dtype=np.uint8),
        y0=0,
        y1=20,
        x0=0,
        x1=20,
        width=20,
        height=20,
        contrast=0,
    )[0]

    # JPEG is lossy, so inspect the centre of the large invalid block with a
    # small tolerance.  The automatic-RFI overlay must not recolour NaNs.
    centre = np.asarray(Image.open(BytesIO(payload)).convert("RGB"))[10, 10]
    assert np.all(np.abs(centre.astype(int) - INVALID_RGB.astype(int)) <= 12)


def test_render_view_image_applies_single_beam_value_mask():
    values = np.linspace(-1.0, 1.0, 20 * 20, dtype=np.float32).reshape(20, 20)
    value_mask = np.zeros((20, 20), dtype=bool)
    value_mask[5:15, 5:15] = True
    payload = render_view_image(
        values,
        value_mask,
        y0=0,
        y1=20,
        x0=0,
        x1=20,
        width=20,
        height=20,
        contrast=0,
        value_mask=value_mask,
        auto_total_beams=1,
    )[0]

    centre = np.asarray(Image.open(BytesIO(payload)).convert("RGB"))[10, 10]
    neutral_distance = np.linalg.norm(centre.astype(float) - INVALID_RGB.astype(float))
    viridis_low_distance = np.linalg.norm(
        centre.astype(float) - np.asarray([68.0, 1.0, 84.0])
    )
    assert neutral_distance < 35
    assert neutral_distance < viridis_low_distance


def test_ui_explains_fully_invalid_beam_mean():
    html = Path(__file__).with_name("crafts_tod_native_ui.html").read_text()
    assert "19 beams 均无有效值（NaN，不是负值）" in html


def test_mean_spectrum_breaks_lines_at_nan_gaps():
    html = Path(__file__).with_name("crafts_tod_native_ui.html").read_text()
    assert "if(!Number.isFinite(v)){flush();" in html and "points.push(null)" in html
    assert "if(point===null){started=false;continue}" in html
    assert "Number.isFinite(data.pol0[i])&&Number.isFinite(data.pol1[i])" not in html


def test_mean_spectrum_has_robust_switchable_y_scale():
    html = Path(__file__).with_name("crafts_tod_native_ui.html").read_text()
    assert "<select id=\"spectrumY\"" in html
    assert '<option value="strong">强抗 RFI</option>' in html
    assert '<option value="robust">抗 RFI</option>' in html
    assert "SPECTRUM_Y_LABELS" in html
    assert "fence=strong?1.5:2.5" in html
    assert "tail=strong?.02:.01" in html
    assert "mode==='percentile'" in html
    assert "mode==='full'" in html
    assert "$('#spectrumY').onchange" in html
    plot = html.split("function drawSpectrum(data)", 1)[1].split("async function pollSpectrum", 1)[0]
    assert plot.index("ctx.clip()") < plot.index("for(const line of prepared.series)")
    assert "Mean (K)" in html


def test_ui_clears_every_loading_cover():
    html = Path(__file__).with_name("crafts_tod_native_ui.html").read_text()
    assert "querySelectorAll('.loading-cover').forEach" in html
    assert "$('.loading-cover').hidden" not in html


def test_ui_has_synchronized_frequency_reference_cursor():
    html = Path(__file__).with_name("crafts_tod_native_ui.html").read_text()
    assert "referenceFrequency" in html
    assert "$('#spectrum').addEventListener('click'" in html
    assert "drawReference(ctx,p,true)" in html


def test_waterfall_colorbar_has_independent_drag_handles_and_auto_reset():
    html = Path(__file__).with_name("crafts_tod_native_ui.html").read_text()
    assert 'id="colorAuto"' in html
    assert "colorRanges:[null,null]" in html
    assert "function colorbarHit" in html
    assert "function updateColorDrag" in html
    assert "app.colorDrag={pol,handle:hit.handle" in html
    assert "const fixed=job.colorRanges[pol]" in html
    assert "[params.vmin,params.vmax]=fixed" in html
    assert "X-Auto-Color-Low" in html
    assert "恢复两个偏振的自动色标" in html


def test_spectrum_and_waterfalls_share_frequency_plot_bounds():
    html = Path(__file__).with_name("crafts_tod_native_ui.html").read_text()
    bounds = "w:w-(PAD.l+PAD.r)*dpr"
    assert bounds in html.split("function plotRect(c)", 1)[1].split("function dataAt", 1)[0]
    assert bounds in html.split("function drawSpectrum(data)", 1)[1].split("const single=", 1)[0]
    assert "w-77*dpr" not in html
    assert "c.width-77*dpr" not in html


def test_spectrum_payload_selects_raw_or_auto_masked_mean():
    state = NativeMaskState.__new__(NativeMaskState)
    state.lock = threading.RLock()
    state.frequency = np.array([1320.0, 1320.1])
    state.spectra = {
        False: np.array([[1.0, 2.0], [3.0, 4.0]]),
        True: np.array([[10.0, 20.0], [30.0, 40.0]]),
    }
    state.local_medians = [0.0, 0.0]
    state.spectrum_revision = 7

    raw = state.spectrum_payload(False)
    masked = state.spectrum_payload(True)

    assert raw["apply_auto"] is False
    assert masked["apply_auto"] is True
    assert raw["pol0"] == [1.0, 3.0]
    assert masked["pol0"] == [10.0, 30.0]


def test_spectrum_payload_selects_loaded_single_beam():
    state = NativeMaskState.__new__(NativeMaskState)
    state.lock = threading.RLock()
    state.frequency = np.array([1320.0, 1320.1])
    state.selected_beam = "M03"
    state.single = type("Single", (), {"beam": "M03"})()
    state.single_spectra = {
        False: np.array([[1.0, 2.0], [3.0, 4.0]]),
        True: np.array([[10.0, 20.0], [30.0, 40.0]]),
    }
    state.single_medians = [100.0, 200.0]
    state.spectrum_revision = 8

    payload = state.spectrum_payload(True, "single", "M03")

    assert payload["view_mode"] == "single"
    assert payload["view_label"] == "M03"
    assert payload["pol0"] == [110.0, 130.0]
    assert payload["pol1"] == [220.0, 240.0]


def test_ui_has_mean_and_single_beam_view_controls():
    html = Path(__file__).with_name("crafts_tod_native_ui.html").read_text()
    assert 'id="viewMode"' in html
    assert '<option value="mean">19-beam Mean</option>' in html
    assert '<option value="single">Single beam</option>' in html
    assert 'id="beamSelect"' in html
    assert "for(let beam=1;beam<=19;beam++)" in html
    assert "action:'set_view'" in html
    assert "view_mode:job.viewMode" in html


def test_manual_edits_are_scoped_to_mean_or_selected_beam():
    state = NativeMaskState.__new__(NativeMaskState)
    state.lock = threading.RLock()
    state.aggregate = object()
    state.frequency = np.array([100.0, 101.0])
    state.mjd = np.array([60000.0, 60000.1])
    state.document = MaskDocument(
        scan_name="scan",
        serial="0001",
        beam_files={"M03": "M03.fits"},
        polarizations=[PolarizationMask(), PolarizationMask()],
    )
    state.undo_stack = []
    state.revision = 0
    state.dirty = False
    state.selected_beam = "M03"
    state.view_mode = "single"
    state._log = lambda *args, **kwargs: None
    state.schedule_spectrum = lambda: None

    state.edit(
        {"mode": "box", "pol": 0, "both": False, "f0": 100.0, "f1": 101.0, "t0": 0, "t1": 1}
    )
    assert not state.document.polarizations[0].boxes
    assert len(state.document.beam_polarizations["M03"][0].boxes) == 1

    state.view_mode = "mean"
    state.edit(
        {"mode": "box", "pol": 1, "both": False, "f0": 100.0, "f1": 101.0, "t0": 0, "t1": 1}
    )
    assert len(state.document.polarizations[1].boxes) == 1
    assert not state.document.beam_polarizations["M03"][1].boxes


def test_auto_mask_preview_is_one_pixel_wider_without_changing_source():
    source = np.zeros((5, 7), dtype=np.uint8)
    source[2, 3] = 19

    preview = _mask_pool(source, 0, 5, 0, 7, 5, 7)

    assert np.array_equal(np.flatnonzero(preview[2]), [2, 3, 4])
    assert np.array_equal(np.flatnonzero(source[2]), [3])


def test_auto_mask_preview_width_is_bounded_from_one_to_nine_pixels():
    source = np.zeros((5, 15), dtype=np.uint8)
    source[2, 7] = 19

    thin = _mask_pool(source, 0, 5, 0, 15, 5, 15, thickness=-10)
    thick = _mask_pool(source, 0, 5, 0, 15, 5, 15, thickness=99)

    assert np.count_nonzero(thin[2]) == 1
    assert np.count_nonzero(thick[2]) == 9


def test_ui_has_bounded_auto_rfi_width_controls():
    html = Path(__file__).with_name("crafts_tod_native_ui.html").read_text()
    assert 'id="rfiThin"' in html
    assert 'id="rfiThick"' in html
    assert "Math.max(0,Math.min(4,app.autoWidth+delta))" in html
    assert "auto_width:job.autoWidth" in html


def test_ui_has_bounded_image_resolution_controls():
    html = Path(__file__).with_name("crafts_tod_native_ui.html").read_text()
    assert 'id="dpiDown"' in html
    assert 'id="dpiUp"' in html
    assert "const IMAGE_SCALES=[.5,.75,1,1.25,1.5,2,3,4]" in html
    assert "app.imageScaleIndex=Math.max(0,Math.min(IMAGE_SCALES.length-1" in html
    assert "p.w/p.dpr*job.scale" in html
    assert "p.h/p.dpr*job.scale" in html
    assert "ctx.imageSmoothingEnabled=false" in html


def test_ui_requires_confirmation_and_lists_scoped_region_counts():
    html = Path(__file__).with_name("crafts_tod_native_ui.html").read_text()
    assert 'id="saveConfirm"' in html
    assert 'id="confirmSave"' in html
    assert "action:'save_summary'" in html
    assert "每个最终 mask = Mean 公共层 ∪ 对应 beam 专属层" in html
    assert "$('#confirmSave').onclick" in html
    assert "action({action:'save'})" in html
