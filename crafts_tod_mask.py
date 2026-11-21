"""I/O and compact mask representation for CRAFTS TOD FITS files."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Callable, Iterable

import numpy as np
from astropy.io import fits


FILENAME_RE = re.compile(
    r"^(?P<scan>.+?)_arcdrift-(?P<beam>M\d{2})_"
    r"(?P<window>[^_]+)_(?P<serial>\d{4})(?P<tail>_.+)\.fits$",
    re.IGNORECASE,
)
MASK_FORMAT = "crafts-tod-manual-mask-v2"
BEAM_MASK_SCHEMA_VERSION = "CRAFTS_MANUAL_RFI_MASK_V1"
AUTHORING_SCOPE_SCHEMA = "global-plus-per-beam-v1"
EXPECTED_BEAMS = tuple(f"M{index:02d}" for index in range(1, 20))
AUTO_LAYER_NAMES = ("one_channel", "sat1380")


@dataclass(frozen=True)
class TodFile:
    path: Path
    scan_name: str
    beam: str
    window: str
    serial: str
    tail: str


@dataclass(frozen=True)
class TodGroup:
    scan_name: str
    serial: str
    window: str
    tail: str
    files: tuple[TodFile, ...]

    @property
    def key(self) -> str:
        return f"{self.scan_name}__{self.window}__{self.serial}__T"

    @property
    def beams(self) -> tuple[str, ...]:
        return tuple(item.beam for item in self.files)


@dataclass
class PolarizationMask:
    frequency_ranges_mhz: list[list[float]] = field(default_factory=list)
    time_ranges_records: list[list[int]] = field(default_factory=list)
    boxes: list[dict[str, list[float] | list[int]]] = field(default_factory=list)


@dataclass
class MaskDocument:
    scan_name: str
    serial: str
    window: str = "W"
    stage: str = "T"
    beam_files: dict[str, str] = field(default_factory=dict)
    auto_mask_files: dict[str, dict[str, str]] = field(default_factory=dict)
    auto_mask_roles: dict[str, str] = field(
        default_factory=lambda: {
            "one_channel": "science_bad",
            "sat1380": "science_bad",
        }
    )
    polarizations: list[PolarizationMask] = field(
        default_factory=lambda: [PolarizationMask(), PolarizationMask()]
    )
    beam_polarizations: dict[str, list[PolarizationMask]] = field(default_factory=dict)
    done: bool = False
    format: str = MASK_FORMAT
    authoring_scope_schema: str = AUTHORING_SCOPE_SCHEMA
    saved_at_utc: str = ""
    frequency_unit: str = "MHz"
    time_unit: str = "record"
    interval_convention: str = "frequency closed; record intervals half-open [start, stop)"

    @classmethod
    def empty(cls, group: TodGroup) -> "MaskDocument":
        return cls(
            scan_name=group.scan_name,
            serial=group.serial,
            window=group.window,
            stage="T",
            beam_files={item.beam: str(item.path.resolve()) for item in group.files},
            beam_polarizations={
                item.beam: [PolarizationMask(), PolarizationMask()]
                for item in group.files
            },
        )


@dataclass(frozen=True)
class AutoMaskDirectories:
    one_channel: Path | None = None
    sat1380: Path | None = None


@dataclass
class BeamAveragedTod:
    frequency_mhz: np.ndarray
    mjd: np.ndarray
    raw_mean: np.ndarray
    auto_masked_mean: np.ndarray
    layer_flagged_beam_count: dict[str, np.ndarray]
    union_flagged_beam_count: np.ndarray
    layer_available_beams: dict[str, int]
    auto_mask_files: dict[str, dict[str, str]]
    warnings: list[str]
    native_record_counts: dict[str, int] = field(default_factory=dict)

    @property
    def beam_count(self) -> int:
        return max(self.layer_available_beams.values(), default=0)


@dataclass
class SingleBeamTod:
    beam: str
    frequency_mhz: np.ndarray
    mjd: np.ndarray
    raw: np.ndarray
    union_mask: np.ndarray
    auto_mask_files: dict[str, str]
    warnings: list[str]


def parse_tod_filename(path: str | Path) -> TodFile | None:
    path = Path(path)
    match = FILENAME_RE.match(path.name)
    if match is None:
        return None
    values = match.groupdict()
    return TodFile(
        path=path,
        scan_name=values["scan"],
        beam=values["beam"].upper(),
        window=values["window"],
        serial=values["serial"],
        tail=values["tail"],
    )


def discover_groups(input_dir: str | Path) -> list[TodGroup]:
    """Discover exact calibrated ``*_T.fits`` files and group all 19 beams."""
    input_dir = Path(input_dir)
    grouped: dict[tuple[str, str, str, str], list[TodFile]] = {}
    for path in sorted(input_dir.rglob("*_T.fits")):
        item = parse_tod_filename(path)
        if item is None or item.tail.lower() != "_t":
            continue
        key = (item.scan_name, item.serial, item.window, item.tail)
        grouped.setdefault(key, []).append(item)

    result: list[TodGroup] = []
    for (scan_name, serial, window, tail), files in grouped.items():
        files = sorted(files, key=lambda item: item.beam)
        result.append(
            TodGroup(
                scan_name=scan_name,
                serial=serial,
                window=window,
                tail=tail,
                files=tuple(files),
            )
        )
    return sorted(result, key=lambda group: (group.scan_name, int(group.serial)))


def mask_path(mask_dir: str | Path, group: TodGroup) -> Path:
    safe_scan = re.sub(r"[^A-Za-z0-9+_.-]+", "_", group.scan_name)
    safe_window = re.sub(r"[^A-Za-z0-9+_.-]+", "_", group.window)
    return Path(mask_dir) / (
        f"{safe_scan}_{safe_window}_{group.serial}_T_manual_mask.json"
    )


def beam_mask_path(mask_dir: str | Path, item: TodFile) -> Path:
    return Path(mask_dir) / f"{item.path.stem}_manual_rfi_mask.npz"


def _merge_float_ranges(ranges: Iterable[Iterable[float]]) -> list[list[float]]:
    normalized = sorted(
        ([min(float(a), float(b)), max(float(a), float(b))] for a, b in ranges),
        key=lambda item: item[0],
    )
    merged: list[list[float]] = []
    for lo, hi in normalized:
        if not merged or lo > merged[-1][1]:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)
    return merged


def _merge_record_ranges(ranges: Iterable[Iterable[int]]) -> list[list[int]]:
    normalized = sorted(
        ([min(int(a), int(b)), max(int(a), int(b))] for a, b in ranges),
        key=lambda item: item[0],
    )
    merged: list[list[int]] = []
    for start, stop in normalized:
        if stop <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, stop])
        else:
            merged[-1][1] = max(merged[-1][1], stop)
    return merged


def _normalize_polarizations(
    polarizations: list[PolarizationMask],
) -> list[PolarizationMask]:
    while len(polarizations) < 2:
        polarizations.append(PolarizationMask())
    polarizations = polarizations[:2]
    for pol_mask in polarizations:
        pol_mask.frequency_ranges_mhz = _merge_float_ranges(
            pol_mask.frequency_ranges_mhz
        )
        pol_mask.time_ranges_records = _merge_record_ranges(
            pol_mask.time_ranges_records
        )
        normalized_boxes = []
        for box in pol_mask.boxes:
            freq_lo, freq_hi = box["frequency_mhz"]
            rec_start, rec_stop = box["records"]
            rec_start, rec_stop = sorted((int(rec_start), int(rec_stop)))
            if rec_stop <= rec_start:
                continue
            normalized_boxes.append(
                {
                    "frequency_mhz": [
                        min(float(freq_lo), float(freq_hi)),
                        max(float(freq_lo), float(freq_hi)),
                    ],
                    "records": [rec_start, rec_stop],
                }
            )
        pol_mask.boxes = normalized_boxes
    return polarizations


def normalize_document(document: MaskDocument) -> MaskDocument:
    document.polarizations = _normalize_polarizations(document.polarizations)
    normalized_beams: dict[str, list[PolarizationMask]] = {}
    for beam in sorted(set(document.beam_files) | set(document.beam_polarizations)):
        if beam not in EXPECTED_BEAMS:
            raise ValueError(f"Unsupported beam-specific manual mask: {beam!r}")
        normalized_beams[beam] = _normalize_polarizations(
            document.beam_polarizations.get(
                beam, [PolarizationMask(), PolarizationMask()]
            )
        )
    document.beam_polarizations = normalized_beams
    document.authoring_scope_schema = AUTHORING_SCOPE_SCHEMA
    return document


def save_mask_document(path: str | Path, document: MaskDocument) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.saved_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    normalize_document(document)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(document), indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_mask_document(path: str | Path, group: TodGroup) -> MaskDocument:
    path = Path(path)
    if not path.exists():
        return MaskDocument.empty(group)
    values = json.loads(path.read_text(encoding="utf-8"))
    if values.get("format") != MASK_FORMAT:
        raise ValueError(f"Unsupported mask format in {path}: {values.get('format')!r}")
    polarizations = [PolarizationMask(**item) for item in values.pop("polarizations", [])]
    beam_polarizations = {
        str(beam).upper(): [PolarizationMask(**item) for item in items]
        for beam, items in values.pop("beam_polarizations", {}).items()
    }
    document = MaskDocument(
        polarizations=polarizations,
        beam_polarizations=beam_polarizations,
        **values,
    )
    if (
        document.scan_name != group.scan_name
        or document.serial != group.serial
        or document.window != group.window
        or document.stage != "T"
    ):
        raise ValueError(f"Mask file {path} does not match {group.key}")
    document.beam_files = {
        item.beam: str(item.path.resolve()) for item in group.files
    }
    return normalize_document(document)


def mask_is_done(path: str | Path) -> bool:
    try:
        return bool(json.loads(Path(path).read_text(encoding="utf-8")).get("done"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def _select_hdus(hdul: fits.HDUList) -> tuple[int, int, int]:
    arrays = [(index, hdu, getattr(hdu, "data", None)) for index, hdu in enumerate(hdul)]
    arrays = [(index, hdu, data) for index, hdu, data in arrays if data is not None]

    tod_candidates = [item for item in arrays if np.ndim(item[2]) == 3]
    if not tod_candidates:
        raise ValueError("No three-dimensional TOD image was found")
    tod_index, _, tod = max(tod_candidates, key=lambda item: np.size(item[2]))

    one_dimensional = [item for item in arrays if np.ndim(item[2]) == 1]
    frequency = next(
        (item for item in one_dimensional if "freq" in item[1].name.lower()),
        None,
    )
    time = next(
        (
            item
            for item in one_dimensional
            if any(token in item[1].name.lower() for token in ("mjd", "time", "date"))
        ),
        None,
    )
    if frequency is None:
        frequency = next(
            (item for item in one_dimensional if len(item[2]) in tod.shape),
            None,
        )
    if time is None:
        remaining = [item for item in one_dimensional if frequency is None or item[0] != frequency[0]]
        time = next((item for item in remaining if len(item[2]) in tod.shape), None)
    if frequency is None or time is None:
        raise ValueError("Could not identify the frequency and time vectors")
    return frequency[0], time[0], tod_index


def _read_axes(path: Path) -> tuple[np.ndarray, np.ndarray, tuple[int, ...], tuple[int, int, int]]:
    with fits.open(path, memmap=True) as hdul:
        freq_index, time_index, tod_index = _select_hdus(hdul)
        frequency = np.asarray(hdul[freq_index].data, dtype=np.float64)
        time = np.asarray(hdul[time_index].data, dtype=np.float64)
        unit = str(hdul[freq_index].header.get("BUNIT", "")).strip().lower()
        if unit in {"hz", "hertz"} or np.nanmedian(np.abs(frequency)) > 1.0e5:
            frequency = frequency / 1.0e6
        shape = tuple(int(value) for value in hdul[tod_index].data.shape)
    return frequency, time, shape, (freq_index, time_index, tod_index)


def _tod_axes(shape: tuple[int, ...], ntime: int, nfreq: int) -> tuple[int, int, int]:
    time_axes = [index for index, length in enumerate(shape) if length == ntime]
    freq_axes = [index for index, length in enumerate(shape) if length == nfreq]
    for time_axis in time_axes:
        for freq_axis in freq_axes:
            if time_axis == freq_axis:
                continue
            remaining = ({0, 1, 2} - {time_axis, freq_axis}).pop()
            if shape[remaining] >= 2:
                return time_axis, freq_axis, remaining
    raise ValueError(
        f"TOD shape {shape} cannot be matched to time={ntime}, frequency={nfreq}, pol>=2"
    )


def require_time_prefix(native: np.ndarray, reference: np.ndarray) -> None:
    """Accept missing trailing records only, with exact observed timestamps."""
    if (
        not len(native) or len(native) > len(reference)
        or not np.all(np.isfinite(reference))
        or not np.all(np.diff(reference) > 0)
        or not np.array_equal(native, reference[:len(native)])
    ):
        raise ValueError("Time axis is not an exact prefix of the reference grid")


def _validated_axes(group: TodGroup, allow_short_tail: bool):
    if not group.files:
        raise ValueError(f"No beam files in group {group.key}")
    beams = tuple(sorted(item.beam for item in group.files))
    if beams != EXPECTED_BEAMS:
        missing = sorted(set(EXPECTED_BEAMS).difference(beams))
        extra = sorted(set(beams).difference(EXPECTED_BEAMS))
        raise ValueError(
            f"Group {group.key} must contain exactly M01-M19; "
            f"missing={missing}, extra={extra}"
        )
    axes = {item.beam: _read_axes(item.path) for item in group.files}
    reference = max(axes.values(), key=lambda axis: len(axis[1]))
    reference_freq, reference_time = reference[:2]
    mixed = any(len(time) != len(reference_time) for _, time, _, _ in axes.values())
    for item in group.files:
        frequency, time, shape, _ = axes[item.beam]
        _tod_axes(shape, len(time), len(frequency))
        if frequency.shape != reference_freq.shape or not np.allclose(
            frequency, reference_freq, rtol=0.0, atol=1.0e-7, equal_nan=True
        ):
            raise ValueError(f"Frequency axis mismatch: {item.path}")
        if allow_short_tail and mixed:
            if not np.array_equal(frequency, reference_freq) or not np.isfinite(frequency).all():
                raise ValueError(f"Frequency axis mismatch in short-tail group: {item.path}")
            require_time_prefix(time, reference_time)
        elif time.shape != reference_time.shape or not np.allclose(
            time, reference_time, rtol=0.0, atol=1.0e-10, equal_nan=True
        ):
            raise ValueError(f"Time axis mismatch: {item.path}")
    return reference_freq, reference_time, axes


def validate_group(group: TodGroup, *, allow_short_tail: bool = False) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int]]:
    frequency, mjd, _ = _validated_axes(group, allow_short_tail)
    return frequency, mjd, (len(mjd), len(frequency), 2)


def load_beam_averaged_tod(
    group: TodGroup,
    block_records: int = 128,
    progress: Callable[[str], None] | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load two polarizations and average matching TOD pixels across available beams."""
    frequency, time, output_shape = validate_group(group)
    total = np.zeros(output_shape, dtype=np.float32)
    count = np.zeros(output_shape, dtype=np.uint8)

    for beam_number, item in enumerate(group.files, start=1):
        if progress is not None:
            progress(f"Loading {item.beam} ({beam_number}/{len(group.files)}): {item.path.name}")
        with fits.open(item.path, memmap=True) as hdul:
            freq_index, time_index, tod_index = _select_hdus(hdul)
            del freq_index, time_index
            raw = hdul[tod_index].data
            source_axes = _tod_axes(raw.shape, len(time), len(frequency))
            canonical = np.moveaxis(raw, source_axes, (0, 1, 2))
            for start in range(0, len(time), block_records):
                stop = min(start + block_records, len(time))
                block = np.asarray(canonical[start:stop, :, :2], dtype=np.float32)
                valid = np.isfinite(block)
                total[start:stop] += np.where(valid, block, 0.0)
                count[start:stop] += valid

    np.divide(total, count, out=total, where=count > 0)
    total[count == 0] = np.nan
    del count

    if frequency[0] > frequency[-1]:
        frequency = frequency[::-1].copy()
        total = total[:, ::-1, :].copy()
    return frequency, time, total


def _scalar_text(value: np.ndarray) -> str:
    return str(np.asarray(value).item())


def _load_npz_native_mask(
    path: Path,
    *,
    layer: str,
    input_path: Path,
    frequency: np.ndarray,
    mjd: np.ndarray,
    shape: tuple[int, int, int],
) -> np.ndarray:
    with np.load(path, allow_pickle=False) as product:
        required = {"mask", "freq", "mjd", "axis_order", "processing_mode"}
        missing = sorted(required.difference(product.files))
        if missing:
            raise ValueError(f"{layer} mask {path} is missing keys {missing}")
        mask = np.asarray(product["mask"])
        mask_frequency = np.asarray(product["freq"], dtype=float)
        mask_mjd = np.asarray(product["mjd"], dtype=float)
        axis_order = _scalar_text(product["axis_order"])
        processing_mode = _scalar_text(product["processing_mode"])
        if layer == "sat1380":
            sat_required = {"science_data_modified", "science_mask_eligible"}
            sat_missing = sorted(sat_required.difference(product.files))
            if sat_missing:
                raise ValueError(f"SAT1380 mask lacks keys {sat_missing}: {path}")
            if bool(np.asarray(product["science_data_modified"]).item()):
                raise ValueError(f"SAT1380 product modified science data: {path}")
            if not bool(np.asarray(product["science_mask_eligible"]).item()):
                raise ValueError(f"SAT1380 product is not science-mask eligible: {path}")
            expected_input = input_path.name
            if "input_file" in product.files and _scalar_text(product["input_file"]) != expected_input:
                raise ValueError(f"SAT1380 input provenance mismatch: {path}")
    if mask.shape != shape:
        raise ValueError(f"{layer} mask shape {mask.shape} != FLUX {shape}: {path}")
    if axis_order != "time, frequency, polarization":
        raise ValueError(f"Unexpected {layer} axis order {axis_order!r}: {path}")
    if processing_mode != "mask_only":
        raise ValueError(f"{layer} product is not mask_only: {path}")
    if not np.array_equal(mask_frequency, frequency):
        raise ValueError(f"{layer} frequency grid mismatch: {path}")
    if not np.array_equal(mask_mjd, mjd):
        raise ValueError(f"{layer} MJD grid mismatch: {path}")
    return mask != 0


def expected_auto_mask_paths(
    input_path: Path,
    directories: AutoMaskDirectories,
) -> dict[str, Path]:
    stem = input_path.stem
    one_channel_directory = (
        directories.one_channel
        if directories.one_channel is not None
        else input_path.parent / "one_channel_rfi_v2_products"
    )
    sat1380_directory = (
        directories.sat1380
        if directories.sat1380 is not None
        else input_path.parent / "satellite_rfi_1380_products"
    )
    return {
        "one_channel": one_channel_directory / f"{stem}_1channel_rfi_v2_mask.npz",
        "sat1380": sat1380_directory / f"{stem}_satellite_rfi_1380_mask.npz",
    }


def load_auto_mask_layers(
    input_path: Path,
    directories: AutoMaskDirectories,
    frequency: np.ndarray,
    mjd: np.ndarray,
    shape: tuple[int, int, int],
    *,
    require_all: bool = False,
) -> tuple[dict[str, np.ndarray], dict[str, str], list[str]]:
    layers: dict[str, np.ndarray] = {}
    files: dict[str, str] = {}
    warnings: list[str] = []
    for layer, path in expected_auto_mask_paths(input_path, directories).items():
        if not path.exists():
            message = f"Missing {layer} mask for {input_path.name}: {path}"
            if require_all:
                raise FileNotFoundError(message)
            warnings.append(message)
            continue
        mask = _load_npz_native_mask(
            path,
            layer=layer,
            input_path=input_path,
            frequency=frequency,
            mjd=mjd,
            shape=shape,
        )
        layers[layer] = mask
        files[layer] = str(path.resolve())
    return layers, files, warnings


def load_beam_averaged_tod_with_auto_masks(
    group: TodGroup,
    directories: AutoMaskDirectories,
    block_records: int = 128,
    progress: Callable[[str], None] | None = None,
    *,
    require_all_masks: bool = False,
    allow_short_tail: bool = False,
) -> BeamAveragedTod:
    """Average 19 beams before/after each beam's science-RFI mask union."""
    frequency, mjd, axes = _validated_axes(group, allow_short_tail)
    output_shape = (len(mjd), len(frequency), 2)
    raw_total = np.zeros(output_shape, dtype=np.float32)
    clean_total = np.zeros(output_shape, dtype=np.float32)
    raw_count = np.zeros(output_shape, dtype=np.uint8)
    clean_count = np.zeros(output_shape, dtype=np.uint8)
    layer_counts = {
        name: np.zeros(output_shape, dtype=np.uint8) for name in AUTO_LAYER_NAMES
    }
    union_count = np.zeros(output_shape, dtype=np.uint8)
    available = {name: 0 for name in AUTO_LAYER_NAMES}
    source_files = {item.beam: {} for item in group.files}
    warnings: list[str] = []

    for beam_number, item in enumerate(group.files, start=1):
        if progress is not None:
            progress(f"Loading {item.beam} ({beam_number}/19): {item.path.name}")
        native_frequency, native_mjd, native_raw_shape, hdus = axes[item.beam]
        records = len(native_mjd)
        native_shape = (records, len(frequency), 2)
        if records < len(mjd):
            warnings.append(f"{item.beam}: {records}/{len(mjd)} records; missing trailing records shown as NaN")
        layers, files, layer_warnings = load_auto_mask_layers(
            item.path,
            directories,
            native_frequency,
            native_mjd,
            native_shape,
            require_all=require_all_masks,
        )
        source_files[item.beam] = files
        warnings.extend(layer_warnings)
        union = np.zeros(native_shape, dtype=bool)
        for name, mask in layers.items():
            layer_counts[name][:records] += mask
            union |= mask
            available[name] += 1
        union_count[:records] += union

        with fits.open(item.path, memmap=True) as hdul:
            raw = hdul[hdus[2]].data
            source_axes = _tod_axes(native_raw_shape, records, len(frequency))
            canonical = np.moveaxis(raw, source_axes, (0, 1, 2))
            for start in range(0, records, max(1, int(block_records))):
                stop = min(start + max(1, int(block_records)), records)
                block = np.asarray(canonical[start:stop, :, :2], dtype=np.float32)
                finite = np.isfinite(block)
                clean = finite & ~union[start:stop]
                raw_total[start:stop] += np.where(finite, block, 0.0)
                clean_total[start:stop] += np.where(clean, block, 0.0)
                raw_count[start:stop] += finite
                clean_count[start:stop] += clean
        del union, layers

    np.divide(raw_total, raw_count, out=raw_total, where=raw_count > 0)
    np.divide(clean_total, clean_count, out=clean_total, where=clean_count > 0)
    raw_total[raw_count == 0] = np.nan
    clean_total[clean_count == 0] = np.nan
    if frequency[0] > frequency[-1]:
        frequency = frequency[::-1].copy()
        raw_total = raw_total[:, ::-1, :].copy()
        clean_total = clean_total[:, ::-1, :].copy()
        union_count = union_count[:, ::-1, :].copy()
        layer_counts = {name: values[:, ::-1, :].copy() for name, values in layer_counts.items()}
    return BeamAveragedTod(
        frequency_mhz=frequency,
        mjd=mjd,
        raw_mean=raw_total,
        auto_masked_mean=clean_total,
        layer_flagged_beam_count=layer_counts,
        union_flagged_beam_count=union_count,
        layer_available_beams=available,
        auto_mask_files=source_files,
        warnings=warnings,
        native_record_counts={beam: len(axis[1]) for beam, axis in axes.items()},
    )


def load_single_beam_tod_with_auto_masks(
    item: TodFile,
    directories: AutoMaskDirectories,
    block_records: int = 128,
    *,
    require_all_masks: bool = False,
    reference_mjd: np.ndarray | None = None,
) -> SingleBeamTod:
    """Load one beam and its own science-RFI mask union on the native TOD grid."""
    frequency, mjd, source_shape, _ = _read_axes(item.path)
    source_axes = _tod_axes(source_shape, len(mjd), len(frequency))
    output_shape = (len(mjd), len(frequency), 2)
    layers, files, warnings = load_auto_mask_layers(
        item.path,
        directories,
        frequency,
        mjd,
        output_shape,
        require_all=require_all_masks,
    )
    union = np.zeros(output_shape, dtype=bool)
    for mask in layers.values():
        union |= mask

    raw_values = np.empty(output_shape, dtype=np.float32)
    with fits.open(item.path, memmap=True) as hdul:
        _, _, tod_index = _select_hdus(hdul)
        canonical = np.moveaxis(hdul[tod_index].data, source_axes, (0, 1, 2))
        step = max(1, int(block_records))
        for start in range(0, len(mjd), step):
            stop = min(start + step, len(mjd))
            raw_values[start:stop] = np.asarray(
                canonical[start:stop, :, :2], dtype=np.float32
            )

    if frequency[0] > frequency[-1]:
        frequency = frequency[::-1].copy()
        raw_values = raw_values[:, ::-1, :].copy()
        union = union[:, ::-1, :].copy()
    if reference_mjd is not None and len(mjd) != len(reference_mjd):
        require_time_prefix(mjd, reference_mjd)
        records = len(mjd)
        padded = np.full((len(reference_mjd), len(frequency), 2), np.nan, dtype=np.float32)
        padded[:records] = raw_values
        padded_union = np.zeros(padded.shape, dtype=bool)
        padded_union[:records] = union
        raw_values, union = padded, padded_union
        mjd = reference_mjd.copy()
        warnings.append(f"{item.beam}: {records}/{len(mjd)} records; missing trailing records shown as NaN")
    return SingleBeamTod(
        beam=item.beam,
        frequency_mhz=frequency,
        mjd=mjd,
        raw=raw_values,
        union_mask=union,
        auto_mask_files=files,
        warnings=warnings,
    )


def _expand_polarization_mask(
    frequency_mhz: np.ndarray,
    ntime: int,
    pol_mask: PolarizationMask,
) -> np.ndarray:
    mask = np.zeros((ntime, len(frequency_mhz)), dtype=bool)
    for lo, hi in pol_mask.frequency_ranges_mhz:
        mask[:, (frequency_mhz >= lo) & (frequency_mhz <= hi)] = True
    for start, stop in pol_mask.time_ranges_records:
        mask[max(0, start) : min(ntime, stop), :] = True
    for box in pol_mask.boxes:
        lo, hi = box["frequency_mhz"]
        start, stop = box["records"]
        freq_selection = (frequency_mhz >= lo) & (frequency_mhz <= hi)
        mask[max(0, int(start)) : min(ntime, int(stop)), freq_selection] = True
    return mask


def build_mask(
    frequency_mhz: np.ndarray,
    ntime: int,
    document: MaskDocument,
    polarization: int,
    beam: str | None = None,
) -> np.ndarray:
    """Expand the global layer, optionally OR-ing one beam-specific layer."""
    if polarization not in (0, 1):
        raise ValueError("polarization must be 0 or 1")
    mask = _expand_polarization_mask(
        frequency_mhz, ntime, document.polarizations[polarization]
    )
    if beam is not None:
        beam = str(beam).upper()
        beam_layers = document.beam_polarizations.get(beam)
        if beam_layers is not None:
            mask |= _expand_polarization_mask(
                frequency_mhz, ntime, beam_layers[polarization]
            )
    return mask


def apply_mask(
    tod: np.ndarray,
    frequency_mhz: np.ndarray,
    document: MaskDocument,
    copy: bool = True,
    beam: str | None = None,
) -> np.ndarray:
    """Apply both polarization masks to canonical TOD shaped (time, frequency, pol)."""
    if tod.ndim != 3 or tod.shape[2] < 2:
        raise ValueError("TOD must have shape (time, frequency, polarization>=2)")
    result = np.array(tod, copy=True) if copy else tod
    for polarization in (0, 1):
        expanded = build_mask(
            frequency_mhz, tod.shape[0], document, polarization, beam=beam
        )
        result[:, :, polarization][expanded] = np.nan
    return result


def masked_time_average(
    tod: np.ndarray,
    frequency_mhz: np.ndarray,
    document: MaskDocument,
    additional_mask: np.ndarray | None = None,
    beam: str | None = None,
) -> np.ndarray:
    """Average unmasked records for each frequency and polarization.

    A fully frequency-masked channel becomes NaN. Time and box masks remove only
    their selected records, so the remaining samples still contribute.
    """
    return _masked_average(tod, frequency_mhz, document, additional_mask, beam, axis=0)[0]


def masked_frequency_average(
    tod: np.ndarray, frequency_mhz: np.ndarray, document: MaskDocument,
    additional_mask: np.ndarray | None = None, beam: str | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Full-band means and valid channel counts, indexed by record and polarization."""
    return _masked_average(tod, frequency_mhz, document, additional_mask, beam, axis=1)


def _masked_average(tod, frequency_mhz, document, additional_mask, beam, *, axis):
    if tod.ndim != 3 or tod.shape[2] < 2:
        raise ValueError("TOD must have shape (time, frequency, polarization>=2)")
    if tod.shape[1] != len(frequency_mhz):
        raise ValueError("TOD frequency axis does not match frequency_mhz")
    if additional_mask is not None and additional_mask.shape != tod.shape:
        raise ValueError("additional_mask must match the TOD shape")

    length = len(frequency_mhz) if axis == 0 else tod.shape[0]
    means = np.full((length, 2), np.nan, dtype=np.float64)
    counts = np.empty(means.shape, dtype=np.int64)
    for polarization in (0, 1):
        values = np.asarray(tod[:, :, polarization])
        mask = build_mask(
            frequency_mhz, tod.shape[0], document, polarization, beam=beam
        )
        valid = np.isfinite(values) & ~mask
        if additional_mask is not None:
            valid &= ~additional_mask[:, :, polarization]
        count = valid.sum(axis=axis, dtype=np.int64)
        counts[:, polarization] = count
        total = np.sum(values, axis=axis, dtype=np.float64, where=valid)
        np.divide(total, count, out=means[:, polarization], where=count > 0)
    return means, counts


def _region_counts(polarizations: list[PolarizationMask]) -> dict[str, int]:
    by_pol = [
        len(layer.frequency_ranges_mhz)
        + len(layer.time_ranges_records)
        + len(layer.boxes)
        for layer in polarizations
    ]
    return {"pol0": by_pol[0], "pol1": by_pol[1], "total": sum(by_pol)}


def mask_document_region_summary(document: MaskDocument) -> dict[str, object]:
    """Count normalized authoring regions without expanding the native grid."""
    normalized = normalize_document(document)
    beams = sorted(set(normalized.beam_files) | set(normalized.beam_polarizations))
    return {
        "mean": _region_counts(normalized.polarizations),
        "beams": {
            beam: _region_counts(
                normalized.beam_polarizations.get(
                    beam, [PolarizationMask(), PolarizationMask()]
                )
            )
            for beam in beams
        },
    }


def save_beam_mask_products(
    mask_dir: str | Path,
    *,
    group: TodGroup,
    frequency_mhz: np.ndarray,
    mjd: np.ndarray,
    document: MaskDocument,
    json_path: str | Path,
    native_record_counts: dict[str, int] | None = None,
) -> list[Path]:
    """Write one manual-only, exact-grid mask product for each input beam."""
    if native_record_counts is not None:
        if set(native_record_counts) != set(group.beams) or any(
            not 0 < count <= len(mjd) for count in native_record_counts.values()
        ):
            raise ValueError(f"Incomplete or invalid native record counts for {group.key}")
    global_manual = np.stack(
        [
            build_mask(frequency_mhz, len(mjd), document, polarization)
            for polarization in (0, 1)
        ],
        axis=2,
    ).astype(np.uint8)
    json_path = Path(json_path).resolve()
    json_sha256 = hashlib.sha256(json_path.read_bytes()).hexdigest()
    output_paths = []
    for item in group.files:
        records = len(mjd) if native_record_counts is None else native_record_counts[item.beam]
        native_global = global_manual[:records]
        manual = native_global.copy()
        beam_layers = document.beam_polarizations.get(item.beam)
        beam_specific_count = 0
        if beam_layers is not None:
            for polarization in (0, 1):
                specific = _expand_polarization_mask(
                    frequency_mhz,
                    records,
                    beam_layers[polarization],
                )
                beam_specific_count += int(np.count_nonzero(specific))
                manual[:, :, polarization] |= specific
        if not set(np.unique(manual)).issubset({0, 1}):
            raise ValueError("Manual mask must contain only 0 and 1")
        flagged_count = int(np.count_nonzero(manual))
        target = beam_mask_path(mask_dir, item)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("wb") as stream:
            np.savez_compressed(
                stream,
                mask=manual,
                freq=np.asarray(frequency_mhz, dtype=np.float64),
                mjd=np.asarray(mjd[:records], dtype=np.float64),
                axis_order=np.asarray("time, frequency, polarization"),
                processing_mode=np.asarray("mask_only"),
                science_data_modified=np.asarray(False),
                mask_role=np.asarray("manual_rfi_science_bad"),
                flag_manual=np.asarray(1, dtype=np.uint8),
                input_file=np.asarray(item.path.name),
                beam=np.asarray(item.beam),
                scan_name=np.asarray(group.scan_name),
                window=np.asarray(group.window),
                nfile=np.asarray(group.serial),
                reviewed_clean=np.asarray(flagged_count == 0),
                manual_sample_count=np.asarray(flagged_count, dtype=np.int64),
                manual_global_sample_count=np.asarray(
                    np.count_nonzero(native_global), dtype=np.int64
                ),
                manual_beam_specific_sample_count=np.asarray(
                    beam_specific_count, dtype=np.int64
                ),
                manual_counts_by_pol=np.asarray(
                    [np.count_nonzero(manual[:, :, pol]) for pol in (0, 1)],
                    dtype=np.int64,
                ),
                source_json=np.asarray(str(json_path)),
                source_json_sha256=np.asarray(json_sha256),
                format=np.asarray(MASK_FORMAT),
                schema_version=np.asarray(BEAM_MASK_SCHEMA_VERSION),
            )
        temporary.replace(target)
        output_paths.append(target)
    return output_paths
