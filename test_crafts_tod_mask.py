from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from astropy.io import fits

from crafts_tod_mask import (
    AutoMaskDirectories,
    MaskDocument,
    PolarizationMask,
    apply_mask,
    build_mask,
    discover_groups,
    load_mask_document,
    load_beam_averaged_tod_with_auto_masks,
    load_single_beam_tod_with_auto_masks,
    mask_is_done,
    mask_document_region_summary,
    masked_time_average,
    parse_tod_filename,
    save_mask_document,
    save_beam_mask_products,
)


def test_parse_and_group_sample_files(tmp_path: Path) -> None:
    names = [
        "Dec+2654_22_09_arcdrift-M09_W_0007_T.fits",
        "Dec+2654_22_09_arcdrift-M01_W_0007_T.fits",
        "Dec+2654_22_09_arcdrift-M05_W_0007_T.fits",
    ]
    for name in names:
        (tmp_path / name).touch()
    (tmp_path / "Dec+2654_22_09_arcdrift-M01_W_0007_T_swr.fits").touch()

    parsed = parse_tod_filename(tmp_path / names[0])
    assert parsed is not None
    assert parsed.scan_name == "Dec+2654_22_09"
    assert parsed.beam == "M09"
    assert parsed.serial == "0007"

    groups = discover_groups(tmp_path)
    assert len(groups) == 1
    assert groups[0].beams == ("M01", "M05", "M09")


def test_19_beam_average_applies_only_science_rfi_masks(tmp_path: Path) -> None:
    one_dir = tmp_path / "one_channel_rfi_v2_products"
    sat_dir = tmp_path / "satellite_rfi_1380_products"
    one_dir.mkdir()
    sat_dir.mkdir()
    frequency = np.array([1000.0, 1001.0, 1002.0])
    mjd = np.array([60000.0, 60000.00001])
    shape = (2, 3, 2)
    for number in range(1, 20):
        name = f"scan_arcdrift-M{number:02d}_W_0001_T.fits"
        path = tmp_path / name
        flux = np.full(shape, float(number), dtype=np.float32)
        fits.HDUList(
            [
                fits.PrimaryHDU(),
                fits.ImageHDU(frequency, name="FREQ"),
                fits.ImageHDU(mjd, name="MJD"),
                fits.ImageHDU(flux, name="FLUX"),
            ]
        ).writeto(path)
        one_mask = np.zeros(shape, dtype=np.uint8)
        sat_mask = np.zeros(shape, dtype=np.uint8)
        if number == 1:
            one_mask[0, 0, 0] = 1
        if number == 2:
            sat_mask[0, 0, 0] = 1
        common = dict(
            freq=frequency,
            mjd=mjd,
            axis_order=np.asarray("time, frequency, polarization"),
            processing_mode=np.asarray("mask_only"),
        )
        np.savez_compressed(
            one_dir / f"{path.stem}_1channel_rfi_v2_mask.npz",
            mask=one_mask,
            **common,
        )
        np.savez_compressed(
            sat_dir / f"{path.stem}_satellite_rfi_1380_mask.npz",
            mask=sat_mask,
            science_data_modified=np.asarray(False),
            science_mask_eligible=np.asarray(True),
            input_file=np.asarray(path.name),
            **common,
        )

    group = discover_groups(tmp_path)[0]
    result = load_beam_averaged_tod_with_auto_masks(
        group,
        AutoMaskDirectories(one_channel=one_dir, sat1380=sat_dir),
        require_all_masks=True,
    )

    assert np.allclose(result.raw_mean, 10.0)
    assert np.isclose(result.auto_masked_mean[0, 0, 0], 11.0)
    assert result.union_flagged_beam_count[0, 0, 0] == 2
    assert result.layer_flagged_beam_count["one_channel"][0, 0, 0] == 1
    assert result.layer_flagged_beam_count["sat1380"][0, 0, 0] == 1
    assert result.layer_available_beams == {"one_channel": 19, "sat1380": 19}

    single = load_single_beam_tod_with_auto_masks(
        group.files[0],
        AutoMaskDirectories(one_channel=one_dir, sat1380=sat_dir),
        require_all_masks=True,
    )
    assert single.beam == "M01"
    assert np.allclose(single.raw, 1.0)
    assert single.union_mask[0, 0, 0]
    assert not single.union_mask[1, 1, 0]


def test_beam_average_uses_available_beams_and_keeps_all_masked_pixels_nan(tmp_path: Path) -> None:
    shape = (2, 3, 2)
    frequency = np.arange(1300.0, 1303.0)
    mjd = np.arange(2.0)
    one_dir = tmp_path / "one"
    sat_dir = tmp_path / "sat"
    one_dir.mkdir()
    sat_dir.mkdir()

    for number in range(1, 20):
        beam = f"M{number:02d}"
        path = tmp_path / f"scan_arcdrift-{beam}_W_0001_T.fits"
        flux = np.full(shape, float(number), dtype=np.float32)
        fits.HDUList(
            [
                fits.PrimaryHDU(),
                fits.ImageHDU(frequency, name="FREQ"),
                fits.ImageHDU(mjd, name="MJD"),
                fits.ImageHDU(flux, name="FLUX"),
            ]
        ).writeto(path)
        one_mask = np.zeros(shape, dtype=np.uint8)
        one_mask[0, 0, :] = 1
        if number == 1:
            one_mask[0, 1, :] = 1
        common = dict(
            freq=frequency,
            mjd=mjd,
            axis_order=np.asarray("time, frequency, polarization"),
            processing_mode=np.asarray("mask_only"),
        )
        np.savez_compressed(one_dir / f"{path.stem}_1channel_rfi_v2_mask.npz", mask=one_mask, **common)
        np.savez_compressed(
            sat_dir / f"{path.stem}_satellite_rfi_1380_mask.npz",
            mask=np.zeros(shape, dtype=np.uint8),
            science_data_modified=np.asarray(False),
            science_mask_eligible=np.asarray(True),
            **common,
        )

    result = load_beam_averaged_tod_with_auto_masks(
        discover_groups(tmp_path)[0],
        AutoMaskDirectories(one_channel=one_dir, sat1380=sat_dir),
        require_all_masks=True,
    )

    assert np.isnan(result.auto_masked_mean[0, 0]).all()
    assert np.allclose(result.auto_masked_mean[0, 1], 10.5)


def test_expand_and_apply_independent_polarization_masks() -> None:
    frequency = np.arange(100.0, 106.0)
    document = MaskDocument(
        scan_name="test",
        serial="0001",
        beam_files={},
        polarizations=[
            PolarizationMask(
                frequency_ranges_mhz=[[101.0, 102.0]],
                boxes=[{"frequency_mhz": [104.0, 105.0], "records": [1, 3]}],
            ),
            PolarizationMask(time_ranges_records=[[2, 4]]),
        ],
    )

    mask0 = build_mask(frequency, 4, document, 0)
    mask1 = build_mask(frequency, 4, document, 1)
    assert mask0[:, 1:3].all()
    assert mask0[1:3, 4:6].all()
    assert not mask0[0, 0]
    assert mask1[2:4].all()
    assert not mask1[:2].any()

    data = np.ones((4, 6, 2), dtype=np.float32)
    masked = apply_mask(data, frequency, document)
    assert np.isnan(masked[:, :, 0]).sum() == mask0.sum()
    assert np.isnan(masked[:, :, 1]).sum() == mask1.sum()
    assert np.isfinite(data).all()


def test_mask_json_round_trip_and_done_state(tmp_path: Path) -> None:
    filename = "Dec+2654_22_09_arcdrift-M01_W_0008_T.fits"
    (tmp_path / filename).touch()
    group = discover_groups(tmp_path)[0]
    document = MaskDocument.empty(group)
    document.polarizations[0].frequency_ranges_mhz = [[1380.2, 1380.1]]
    document.polarizations[1].time_ranges_records = [[20, 10]]
    document.done = True
    output = tmp_path / "mask.json"

    save_mask_document(output, document)
    restored = load_mask_document(output, group)

    assert mask_is_done(output)
    assert restored.polarizations[0].frequency_ranges_mhz == [[1380.1, 1380.2]]
    assert restored.polarizations[1].time_ranges_records == [[10, 20]]


def test_legacy_json_becomes_global_mean_layer(tmp_path: Path) -> None:
    filename = "scan_arcdrift-M01_W_0008_T.fits"
    (tmp_path / filename).touch()
    group = discover_groups(tmp_path)[0]
    document = MaskDocument.empty(group)
    document.polarizations[0].frequency_ranges_mhz = [[100.0, 101.0]]
    output = tmp_path / "legacy.json"
    save_mask_document(output, document)
    values = json.loads(output.read_text())
    values.pop("beam_polarizations")
    values.pop("authoring_scope_schema")
    output.write_text(json.dumps(values))

    restored = load_mask_document(output, group)

    assert restored.polarizations[0].frequency_ranges_mhz == [[100.0, 101.0]]
    assert not restored.beam_polarizations["M01"][0].frequency_ranges_mhz


def test_masked_time_average_keeps_unmasked_box_samples() -> None:
    frequency = np.array([100.0, 101.0, 102.0])
    data = np.zeros((4, 3, 2), dtype=np.float32)
    data[:, :, 0] = np.arange(4, dtype=np.float32)[:, None]
    data[:, :, 1] = (10.0 + np.arange(4, dtype=np.float32))[:, None]
    document = MaskDocument(
        scan_name="test",
        serial="0001",
        beam_files={},
        polarizations=[
            PolarizationMask(
                frequency_ranges_mhz=[[102.0, 102.0]],
                boxes=[{"frequency_mhz": [101.0, 101.0], "records": [0, 2]}],
            ),
            PolarizationMask(time_ranges_records=[[0, 2]]),
        ],
    )

    spectra = masked_time_average(data, frequency, document)

    assert np.isclose(spectra[0, 0], 1.5)
    assert np.isclose(spectra[1, 0], 2.5)
    assert np.isnan(spectra[2, 0])
    assert np.allclose(spectra[:, 1], 12.5)


def test_masked_time_average_has_nanmean_semantics() -> None:
    frequency = np.array([100.0, 101.0])
    data = np.array(
        [
            [[1.0, 10.0], [np.nan, np.nan]],
            [[np.nan, 20.0], [np.nan, np.nan]],
            [[3.0, 30.0], [np.nan, np.nan]],
        ],
        dtype=np.float32,
    )
    document = MaskDocument(
        scan_name="test",
        serial="0001",
        polarizations=[PolarizationMask(), PolarizationMask()],
    )

    spectra = masked_time_average(data, frequency, document)

    assert np.allclose(spectra[0], [2.0, 20.0])
    assert np.isnan(spectra[1]).all()


def test_masked_time_average_accepts_single_beam_auto_mask() -> None:
    frequency = np.array([100.0, 101.0])
    data = np.arange(3 * 2 * 2, dtype=np.float32).reshape(3, 2, 2)
    auto_mask = np.zeros_like(data, dtype=bool)
    auto_mask[:2, 0, 0] = True
    auto_mask[:, 1, 1] = True
    document = MaskDocument(
        scan_name="test",
        serial="0001",
        polarizations=[PolarizationMask(), PolarizationMask()],
    )

    spectra = masked_time_average(data, frequency, document, additional_mask=auto_mask)

    assert np.isclose(spectra[0, 0], data[2, 0, 0])
    assert np.isnan(spectra[1, 1])


def test_save_writes_19_manual_only_beam_products(tmp_path: Path) -> None:
    frequency = np.array([100.0, 101.0])
    mjd = np.array([60000.0, 60000.1])
    document = MaskDocument(
        scan_name="scan",
        serial="0001",
        polarizations=[
            PolarizationMask(frequency_ranges_mhz=[[101.0, 101.0]]),
            PolarizationMask(),
        ],
    )
    for number in range(1, 20):
        (tmp_path / f"scan_arcdrift-M{number:02d}_W_0001_T.fits").touch()
    group = discover_groups(tmp_path)[0]
    json_path = tmp_path / "manual.json"
    json_path.write_text("{}\n", encoding="utf-8")
    targets = save_beam_mask_products(
        tmp_path / "masks",
        group=group,
        frequency_mhz=frequency,
        mjd=mjd,
        document=document,
        json_path=json_path,
    )

    assert len(targets) == 19
    assert targets[0].name == "scan_arcdrift-M01_W_0001_T_manual_rfi_mask.npz"
    assert targets[-1].name == "scan_arcdrift-M19_W_0001_T_manual_rfi_mask.npz"
    with np.load(targets[0], allow_pickle=False) as product:
        assert product["mask"][:, 1, 0].all()
        assert not product["mask"][:, :, 1].any()
        assert product["mask"].dtype == np.uint8
        assert set(np.unique(product["mask"])) <= {0, 1}
        assert product["input_file"].item() == "scan_arcdrift-M01_W_0001_T.fits"
        assert product["mask_role"].item() == "manual_rfi_science_bad"
        assert not bool(product["reviewed_clean"].item())
        assert "auto_union_flagged_beam_count" not in product.files


def test_save_reviewed_clean_still_writes_19_zero_products(tmp_path: Path) -> None:
    frequency = np.array([100.0, 101.0])
    mjd = np.array([60000.0, 60000.1])
    document = MaskDocument(
        scan_name="scan",
        serial="0002",
        polarizations=[PolarizationMask(), PolarizationMask()],
    )
    for number in range(1, 20):
        (tmp_path / f"scan_arcdrift-M{number:02d}_W_0002_T.fits").touch()
    group = discover_groups(tmp_path)[0]
    json_path = tmp_path / "manual_clean.json"
    json_path.write_text("{}\n", encoding="utf-8")

    targets = save_beam_mask_products(
        tmp_path / "masks",
        group=group,
        frequency_mhz=frequency,
        mjd=mjd,
        document=document,
        json_path=json_path,
    )

    assert len(targets) == 19
    for target in targets:
        with np.load(target, allow_pickle=False) as product:
            assert not product["mask"].any()
            assert int(product["manual_sample_count"].item()) == 0
            assert bool(product["reviewed_clean"].item())


def test_save_writes_global_union_each_beam_specific_mask(tmp_path: Path) -> None:
    frequency = np.array([100.0, 101.0, 102.0])
    mjd = np.array([60000.0, 60000.1, 60000.2])
    for number in range(1, 20):
        (tmp_path / f"scan_arcdrift-M{number:02d}_W_0003_T.fits").touch()
    group = discover_groups(tmp_path)[0]
    document = MaskDocument.empty(group)
    document.polarizations[0].frequency_ranges_mhz = [[100.0, 100.0]]
    document.beam_polarizations["M01"][0].time_ranges_records = [[0, 1]]
    document.beam_polarizations["M02"][1].boxes = [
        {"frequency_mhz": [102.0, 102.0], "records": [1, 3]}
    ]
    json_path = tmp_path / "manual_scoped.json"
    save_mask_document(json_path, document)

    targets = save_beam_mask_products(
        tmp_path / "masks",
        group=group,
        frequency_mhz=frequency,
        mjd=mjd,
        document=document,
        json_path=json_path,
    )

    with np.load(targets[0], allow_pickle=False) as m01:
        assert m01["mask"][:, 0, 0].all()
        assert m01["mask"][0, :, 0].all()
        assert not m01["mask"][:, :, 1].any()
        assert int(m01["manual_global_sample_count"].item()) == 3
        assert int(m01["manual_beam_specific_sample_count"].item()) == 3
        assert int(m01["manual_sample_count"].item()) == 5
    with np.load(targets[1], allow_pickle=False) as m02:
        assert m02["mask"][:, 0, 0].all()
        assert m02["mask"][1:3, 2, 1].all()
        assert int(m02["manual_sample_count"].item()) == 5
    with np.load(targets[2], allow_pickle=False) as m03:
        assert m03["mask"][:, 0, 0].all()
        assert int(m03["manual_sample_count"].item()) == 3
        assert int(m03["manual_beam_specific_sample_count"].item()) == 0

    summary = mask_document_region_summary(document)
    assert summary["mean"] == {"pol0": 1, "pol1": 0, "total": 1}
    assert summary["beams"]["M01"] == {"pol0": 1, "pol1": 0, "total": 1}
    assert summary["beams"]["M02"] == {"pol0": 0, "pol1": 1, "total": 1}
    assert summary["beams"]["M19"] == {"pol0": 0, "pol1": 0, "total": 0}


def test_short_beam_tail_uses_available_samples_and_exports_native_grid(tmp_path: Path) -> None:
    frequency = np.arange(5.) + 1400
    mjd = np.arange(4.) + 60000
    counts = {}
    for number in range(1, 20):
        beam = f"M{number:02d}"
        times = mjd[:2] if beam == "M17" else mjd
        counts[beam] = len(times)
        flux = np.full((len(times), len(frequency), 2), number, dtype=np.float32)
        fits.HDUList([
            fits.PrimaryHDU(frequency),
            fits.ImageHDU(times, name="MJD"),
            fits.ImageHDU(flux, name="FLUX"),
        ]).writeto(tmp_path / f"scan_arcdrift-{beam}_W_0044_T.fits")

    group = discover_groups(tmp_path)[0]
    directories = AutoMaskDirectories()
    with np.testing.assert_raises(ValueError):
        load_beam_averaged_tod_with_auto_masks(group, directories)
    aggregate = load_beam_averaged_tod_with_auto_masks(group, directories, allow_short_tail=True)
    np.testing.assert_allclose(aggregate.raw_mean[:2], 10)
    np.testing.assert_allclose(aggregate.raw_mean[2:], (sum(range(1, 20)) - 17) / 18)
    item = next(item for item in group.files if item.beam == "M17")
    single = load_single_beam_tod_with_auto_masks(item, directories, reference_mjd=mjd)
    np.testing.assert_allclose(single.raw[:2], 17)
    assert single.raw.shape == (4, 5, 2)
    assert np.isnan(single.raw[2:]).all()

    document = MaskDocument.empty(group)
    document.polarizations[0].time_ranges_records = [[1, 4]]
    json_path = tmp_path / "manual.json"
    save_mask_document(json_path, document)
    paths = save_beam_mask_products(
        tmp_path / "out", group=group, frequency_mhz=frequency, mjd=mjd,
        document=document, json_path=json_path, native_record_counts=counts,
    )
    assert len(paths) == 19
    for item, path in zip(group.files, paths):
        records = counts[item.beam]
        with np.load(path) as product:
            assert product["mask"].shape == (records, len(frequency), 2)
            np.testing.assert_array_equal(product["mjd"], mjd[:records])
            np.testing.assert_array_equal(product["freq"], frequency)
            assert int(product["manual_sample_count"]) == (records - 1) * len(frequency)
            assert int(product["manual_global_sample_count"]) == (records - 1) * len(frequency)


def test_short_beam_with_interior_time_gap_is_rejected() -> None:
    from crafts_tod_mask import require_time_prefix

    with np.testing.assert_raises(ValueError):
        require_time_prefix(np.array([1., 3.]), np.array([1., 2., 3.]))
