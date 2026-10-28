#!/usr/bin/env python3
"""Responsive browser UI for manual CRAFTS TOD masking.

The browser draws selections, axes, and spectra locally.  The server only loads
the exact-grid arrays, emits bounded PNG views, and writes the existing
manual-only JSON/NPZ products.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from io import BytesIO
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import traceback
from typing import Any

import numpy as np
from PIL import Image
import tornado.ioloop
import tornado.web

from crafts_tod_mask import (
    AutoMaskDirectories,
    BeamAveragedTod,
    MaskDocument,
    PolarizationMask,
    SingleBeamTod,
    beam_mask_path,
    discover_groups,
    load_beam_averaged_tod_with_auto_masks,
    load_single_beam_tod_with_auto_masks,
    load_mask_document,
    mask_document_region_summary,
    mask_path,
    masked_time_average,
    normalize_document,
    save_beam_mask_products,
    save_mask_document,
)


APP_VERSION = "native-web-v1"
DEFAULT_INPUT_DIR = Path(
    "/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/"
    "gzn_20260831/five_stripes_current_modules_0001_0010_v2/server_data/"
    "ZD2022_1_2/Dec+2654_22_09__20230716"
)
DEFAULT_MASK_DIR = Path(
    "/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/"
    "crafts_rfi_manual_masks/Dec+2654_22_09__20230716"
)


def _jsonable_document(document: MaskDocument) -> dict[str, Any]:
    return asdict(document)


def _viridis_lut() -> np.ndarray:
    # Compact samples from viridis, interpolated to 256 entries.  Keeping the
    # LUT here avoids importing Matplotlib and its rendering stack.
    anchors = np.asarray(
        [
            (68, 1, 84), (71, 44, 122), (59, 82, 139), (44, 113, 142),
            (33, 145, 140), (39, 173, 129), (92, 200, 99),
            (170, 220, 50), (253, 231, 37),
        ],
        dtype=np.float32,
    )
    x = np.linspace(0.0, 1.0, len(anchors))
    xi = np.linspace(0.0, 1.0, 256)
    return np.stack([np.interp(xi, x, anchors[:, channel]) for channel in range(3)], axis=1).astype(np.uint8)


VIRIDIS = _viridis_lut()
INVALID_RGB = np.asarray((108, 116, 126), dtype=np.uint8)


def _bounded_indices(length: int, low: int, high: int, maximum: int) -> np.ndarray:
    low = max(0, min(int(low), length - 1))
    high = max(low + 1, min(int(high), length))
    count = min(maximum, high - low)
    if count >= high - low:
        return np.arange(low, high, dtype=np.int64)
    # Center sampling is intentionally used for speed.  Zooming reveals the
    # exact channels/records, while automatic masks are separately max-pooled.
    return np.linspace(low, high - 1, count, dtype=np.int64)


def _mask_pool(
    mask: np.ndarray,
    y0: int,
    y1: int,
    x0: int,
    x1: int,
    height: int,
    width: int,
    thickness: int = 1,
) -> np.ndarray:
    """Max-pool a mask/count layer so narrow RFI remains visible."""
    source = np.ascontiguousarray(mask[y0:y1, x0:x1], dtype=np.uint8)
    source_height, source_width = source.shape
    factor_y = max(1, int(np.ceil(source_height / height)))
    factor_x = max(1, int(np.ceil(source_width / width)))
    pooled_height = int(np.ceil(source_height / factor_y))
    pooled_width = int(np.ceil(source_width / factor_x))
    padded = np.zeros((pooled_height * factor_y, pooled_width * factor_x), dtype=np.uint8)
    padded[:source_height, :source_width] = source
    pooled = padded.reshape(pooled_height, factor_y, pooled_width, factor_x)
    # Reduce records first to scan contiguous frequency rows before channel bins.
    pooled = pooled.max(axis=1).max(axis=2)
    if pooled.shape != (height, width):
        pooled = np.asarray(
            Image.fromarray(pooled).resize((width, height), resample=Image.Resampling.NEAREST),
            dtype=np.uint8,
        )
    # Display-only dilation along frequency.  Thin one-channel RFI remains easy
    # to see through the SSH preview without changing the underlying mask data.
    thickness = max(0, min(int(thickness), 4))
    thickened = pooled.copy()
    for offset in range(1, thickness + 1):
        thickened[:, offset:] = np.maximum(thickened[:, offset:], pooled[:, :-offset])
        thickened[:, :-offset] = np.maximum(thickened[:, :-offset], pooled[:, offset:])
    return thickened


def render_view_image(
    values: np.ndarray,
    auto_counts: np.ndarray | None,
    *,
    y0: int,
    y1: int,
    x0: int,
    x1: int,
    width: int,
    height: int,
    contrast: int,
    auto_width: int = 1,
    value_mask: np.ndarray | None = None,
    auto_total_beams: int = 19,
    vmin: float | None = None,
    vmax: float | None = None,
) -> tuple[bytes, float, float, float, float]:
    width = max(1, min(int(width), 3200))
    height = max(1, min(int(height), 1600))
    yi = _bounded_indices(values.shape[0], y0, y1, height)
    xi = _bounded_indices(values.shape[1], x0, x1, width)
    sampled = values[np.ix_(yi, xi)]
    if value_mask is not None:
        if value_mask.shape != values.shape:
            raise ValueError("value_mask must match values")
        sampled = sampled.copy()
        sampled[value_mask[np.ix_(yi, xi)]] = np.nan
    finite = sampled[np.isfinite(sampled)]
    percentiles = ((0.5, 99.5), (1.0, 99.0), (2.0, 98.0), (5.0, 95.0))
    if finite.size:
        auto_lo, auto_hi = np.percentile(
            finite, percentiles[int(contrast) % len(percentiles)]
        )
    else:
        auto_lo, auto_hi = -1.0, 1.0
    if not np.isfinite(auto_lo) or not np.isfinite(auto_hi) or auto_lo == auto_hi:
        auto_lo, auto_hi = -1.0, 1.0
    custom_limits_valid = (
        vmin is not None
        and vmax is not None
        and np.isfinite(vmin)
        and np.isfinite(vmax)
        and vmax > vmin
    )
    lo, hi = (float(vmin), float(vmax)) if custom_limits_valid else (auto_lo, auto_hi)
    invalid = ~np.isfinite(sampled)
    scaled = np.nan_to_num((sampled - lo) / (hi - lo), nan=0.0, posinf=1.0, neginf=0.0)
    rgb = VIRIDIS[np.clip(np.rint(scaled * 255.0), 0, 255).astype(np.uint8)]
    if auto_counts is not None:
        pooled = _mask_pool(
            auto_counts,
            y0,
            y1,
            x0,
            x1,
            len(yi),
            len(xi),
            thickness=auto_width,
        )
        denominator = max(1, int(auto_total_beams))
        alpha = np.minimum(0.62, 0.25 + pooled.astype(np.float32) / denominator * 0.37)
        active = pooled > 0
        orange = np.asarray((255, 126, 20), dtype=np.float32)
        rgb[active] = (
            rgb[active].astype(np.float32) * (1.0 - alpha[active, None])
            + orange * alpha[active, None]
        ).astype(np.uint8)
    # A fully invalid 19-beam pixel is NaN, not a very low measurement.  Paint
    # it neutral grey after all overlays so it cannot be mistaken for either
    # the bottom of viridis or an automatic-RFI highlight.
    rgb[invalid] = INVALID_RGB
    image = Image.fromarray(np.flipud(rgb))
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=80, subsampling=1, optimize=False)
    return (
        stream.getvalue(),
        float(lo),
        float(hi),
        float(auto_lo),
        float(auto_hi),
    )


class NativeMaskState:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.groups = discover_groups(args.input_dir)
        if not self.groups:
            raise FileNotFoundError(f"No *_T.fits groups found under {args.input_dir}")
        self.index = self._target_index(args.target)
        self.aggregate: BeamAveragedTod | None = None
        self.document = MaskDocument.empty(self.groups[self.index])
        self.frequency = np.empty(0, dtype=np.float64)
        self.mjd = np.empty(0, dtype=np.float64)
        self.local_medians = [0.0, 0.0]
        self.spectra = {
            False: np.empty((0, 2), dtype=np.float64),
            True: np.empty((0, 2), dtype=np.float64),
        }
        self.view_mode = "mean"
        self.selected_beam = "M01"
        self.single: SingleBeamTod | None = None
        self.single_medians = [0.0, 0.0]
        self.single_spectra = {
            False: np.empty((0, 2), dtype=np.float64),
            True: np.empty((0, 2), dtype=np.float64),
        }
        self.single_loading = False
        self.spectrum_revision = 0
        self.spectrum_content_revision = 0
        self._spectrum_request_token = 0
        self._spectrum_task: asyncio.Task[None] | None = None
        self._view_request_token = 0
        self.revision = 0
        self.dirty = False
        self.phase = "starting"
        self.busy = True
        self.navigation_busy = False
        self.shutting_down = False
        self.session_instance = secrets.token_hex(16)
        self.control_token = secrets.token_urlsafe(32)
        self.message = "Starting native web UI"
        self.logs: list[dict[str, Any]] = []
        self.undo_stack: list[MaskDocument] = []
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crafts-data")
        self.render_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="crafts-view")
        self.image_cache: dict[
            tuple[Any, ...], tuple[bytes, float, float, float, float]
        ] = {}
        self.load_token = 0
        self._log(self.message, "starting", True)

    @property
    def group(self):
        return self.groups[self.index]

    def _target_index(self, target: str | None) -> int:
        if not target:
            return 0
        target = target.strip()
        for index, group in enumerate(self.groups):
            if target in {group.key, group.serial, f"{group.scan_name}:{group.serial}"}:
                return index
        raise ValueError(f"Target {target!r} not found")

    def _log(self, message: str, phase: str | None = None, busy: bool | None = None) -> None:
        with self.lock:
            if phase is not None:
                self.phase = phase
            if busy is not None:
                self.busy = busy
            self.message = message
            self.logs.append({"time": time.strftime("%H:%M:%S"), "message": message})
            self.logs = self.logs[-120:]
        print(message, flush=True)

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            ready = self.aggregate is not None and self.phase not in {"loading", "beam-loading", "error", "stopping"}
            group = self.group
            return {
                "version": APP_VERSION,
                "session": {"protocol": 1, "pid": os.getpid(), "instance": self.session_instance,
                            "input_dir": str(Path(self.args.input_dir).resolve()),
                            "mask_dir": str(Path(self.args.mask_dir).resolve())},
                "phase": self.phase,
                "busy": self.busy,
                "message": self.message,
                "ready": ready,
                "dirty": self.dirty,
                "revision": self.revision,
                "spectrum_revision": self.spectrum_revision,
                "index": self.index,
                "group_count": len(self.groups),
                "group": {"scan": group.scan_name, "serial": group.serial, "key": group.key, "beams": list(group.beams)},
                "groups": [{"index": i, "scan": g.scan_name, "serial": g.serial, "done": mask_path(self.args.mask_dir, g).exists()} for i, g in enumerate(self.groups)],
                "shape": [len(self.mjd), len(self.frequency)],
                "frequency_range": [float(self.frequency[0]), float(self.frequency[-1])] if len(self.frequency) else None,
                "frequency_step_mhz": float(np.nanmedian(np.diff(self.frequency))) if len(self.frequency) > 1 else None,
                "mjd_range": [float(self.mjd[0]), float(self.mjd[-1])] if len(self.mjd) else None,
                "local_medians": self.local_medians,
                "auto_available": dict(self.aggregate.layer_available_beams) if self.aggregate else {},
                "view_mode": self.view_mode,
                "selected_beam": self.selected_beam,
                "single_loaded_beam": self.single.beam if self.single is not None else None,
                "single_loading": self.single_loading,
                "document": _jsonable_document(self.document),
                "logs": list(self.logs),
            }

    def prepare_shutdown(self, payload: dict[str, Any], token: str) -> None:
        """Atomically recheck the visible state and reject further edits before stopping."""
        with self.lock:
            if not token or not hmac.compare_digest(token, self.control_token):
                raise PermissionError("关闭请求缺少有效的本用户控制凭据。")
            if payload.get("pid") != os.getpid() or payload.get("instance") != self.session_instance:
                raise ValueError("会话实例已变化，未关闭任何进程。")
            if self.shutting_down or self.busy or self.navigation_busy:
                raise ValueError("会话正在加载、保存或切换，暂不能重启。")
            if self.dirty:
                raise ValueError("存在未保存的人工标记，请先在网页保存。")
            if payload.get("group_key") != self.group.key or payload.get("revision") != self.revision:
                raise ValueError("条目或编辑状态已变化，请重新检查后再选择。")
            self.shutting_down = True
            self._log("Safe shutdown requested by launcher; no unsaved edits", "stopping", True)

    async def load(self, index: int | None = None, *, discard_unsaved: bool = False,
                   raise_errors: bool = False) -> None:
        with self.lock:
            if self.shutting_down:
                raise ValueError("会话正在关闭，不再加载新条目。")
            if self.dirty and index is not None and index != self.index and not discard_unsaved:
                raise ValueError("Unsaved edits: save or revert before changing nfile")
            if index is not None:
                self.index = index % len(self.groups)
            self.load_token += 1
            token = self.load_token
            self.aggregate = None
            self.frequency = np.empty(0)
            self.mjd = np.empty(0)
            self.spectra = {
                False: np.empty((0, 2), dtype=np.float64),
                True: np.empty((0, 2), dtype=np.float64),
            }
            self.view_mode = "mean"
            self.single = None
            self.single_medians = [0.0, 0.0]
            self.single_spectra = {
                False: np.empty((0, 2), dtype=np.float64),
                True: np.empty((0, 2), dtype=np.float64),
            }
            self.single_loading = False
            self.image_cache.clear()
            self._log(f"Loading {self.group.scan_name}:{self.group.serial} (19 beams)", "loading", True)
        loop = asyncio.get_running_loop()
        try:
            aggregate = await loop.run_in_executor(
                self.executor,
                lambda: load_beam_averaged_tod_with_auto_masks(
                    self.group,
                    AutoMaskDirectories(self.args.one_channel_dir, self.args.sat1380_dir),
                    block_records=self.args.block_records,
                    progress=lambda message: self._log(message, "loading", True),
                    require_all_masks=self.args.require_all_auto_masks,
                ),
            )
            with self.lock:
                if token != self.load_token:
                    return
                self.aggregate = aggregate
                self.frequency = aggregate.frequency_mhz
                self.mjd = aggregate.mjd
                self.document = load_mask_document(mask_path(self.args.mask_dir, self.group), self.group)
                self.document.auto_mask_files = aggregate.auto_mask_files
                self.local_medians = []
                for pol in (0, 1):
                    sample = aggregate.raw_mean[::max(1, aggregate.raw_mean.shape[0] // 400), ::max(1, aggregate.raw_mean.shape[1] // 1200), pol]
                    median = float(np.nanmedian(sample))
                    self.local_medians.append(median)
                    aggregate.raw_mean[:, :, pol] -= median
                    aggregate.auto_masked_mean[:, :, pol] -= median
                self.undo_stack.clear()
                self.dirty = False
                self.revision += 1
                self._log(f"Ready: {self.group.scan_name}:{self.group.serial}; drag to create a manual mask", "ready", False)
            self.schedule_spectrum()
        except Exception as error:
            self._log(f"ERROR: {error}", "error", False)
            traceback.print_exc()
            if raise_errors:
                raise

    async def navigate(self, direction: str, resolution: str, group_key: str,
                       revision: int) -> dict[str, Any]:
        """Resolve unsaved edits before changing groups; retain the current group on failure."""
        with self.lock:
            if self.busy or self.navigation_busy or self.aggregate is None:
                raise ValueError("当前操作尚未完成，请稍后再切换。")
            if group_key != self.group.key or revision != self.revision:
                raise ValueError("当前条目或标记已变化，请取消后重新选择跳转。")
            if direction not in {"previous", "next"} or resolution not in {"ask", "save", "discard"}:
                raise ValueError("无效的跳转选项。")
            target = (self.index + (-1 if direction == "previous" else 1)) % len(self.groups)
            if target == self.index:
                return {"saved": False, "paths": []}
            if self.dirty and resolution == "ask":
                raise ValueError("有未保存的标记，请选择保存或舍弃后再跳转。")
            self.navigation_busy = True
        paths = []
        try:
            if resolution == "save":
                paths = await self.save()
            # Snapshot after a successful save: a failed load must not undo that save.
            fields = ("index", "aggregate", "frequency", "mjd", "document", "dirty",
                      "view_mode", "selected_beam", "single", "single_medians", "single_spectra",
                      "spectra", "local_medians", "spectrum_revision", "spectrum_content_revision")
            with self.lock:
                previous = {name: getattr(self, name) for name in fields}
                previous["undo_stack"] = list(self.undo_stack)
            try:
                await self.load(target, discard_unsaved=resolution == "discard", raise_errors=True)
            except Exception as error:
                with self.lock:
                    for name, value in previous.items():
                        setattr(self, name, value)
                    self.image_cache.clear()
                    self.revision += 1
                    self._log(f"LOAD ERROR: {error}; kept current nfile and manual edits", "ready", False)
                self.schedule_spectrum()
                raise
            return {"saved": bool(paths), "paths": paths}
        finally:
            with self.lock:
                self.navigation_busy = False

    async def set_view(self, view_mode: str, beam: str | None = None) -> None:
        view_mode = str(view_mode).lower()
        if view_mode not in {"mean", "single"}:
            raise ValueError(f"Unknown view mode: {view_mode!r}")
        with self.lock:
            if self.aggregate is None:
                raise ValueError("Data are still loading")
            if view_mode == "mean":
                self._view_request_token += 1
                self.view_mode = "mean"
                self.single_loading = False
                self.image_cache.clear()
                self._log("Showing 19-beam mean", "ready", False)
                return
            beam = str(beam or self.selected_beam).upper()
            item = next((candidate for candidate in self.group.files if candidate.beam == beam), None)
            if item is None:
                raise ValueError(f"Beam {beam!r} is not available")
            self._view_request_token += 1
            request_token = self._view_request_token
            load_token = self.load_token
            aggregate = self.aggregate
            self.selected_beam = beam
            if self.single is not None and self.single.beam == beam:
                self.view_mode = "single"
                self.single_loading = False
                self.image_cache.clear()
                self._log(f"Showing single beam {beam}", "ready", False)
                return
            group_key = self.group.key
            reference_frequency = self.frequency.copy()
            reference_mjd = self.mjd.copy()
            self.view_mode = "single"
            self.single_loading = True
            self._log(f"Loading single beam {beam}", "beam-loading", True)

        def read_single() -> tuple[SingleBeamTod, list[float]]:
            single = load_single_beam_tod_with_auto_masks(
                item,
                AutoMaskDirectories(self.args.one_channel_dir, self.args.sat1380_dir),
                block_records=self.args.block_records,
                require_all_masks=self.args.require_all_auto_masks,
            )
            if not np.allclose(single.frequency_mhz, reference_frequency, rtol=0.0, atol=1.0e-7):
                raise ValueError(f"Frequency axis mismatch for {beam}")
            if not np.allclose(single.mjd, reference_mjd, rtol=0.0, atol=1.0e-10):
                raise ValueError(f"MJD axis mismatch for {beam}")
            medians = []
            for pol in (0, 1):
                sample = single.raw[
                    ::max(1, single.raw.shape[0] // 400),
                    ::max(1, single.raw.shape[1] // 1200),
                    pol,
                ]
                median = float(np.nanmedian(sample))
                medians.append(median)
                single.raw[:, :, pol] -= median
            return single, medians

        try:
            single, medians = await asyncio.get_running_loop().run_in_executor(
                self.executor, read_single
            )
            with self.lock:
                if (
                    request_token != self._view_request_token
                    or load_token != self.load_token
                    or aggregate is not self.aggregate
                    or self.group.key != group_key
                ):
                    return
                self.single = single
                self.single_medians = medians
                self.single_spectra = {
                    False: np.empty((0, 2), dtype=np.float64),
                    True: np.empty((0, 2), dtype=np.float64),
                }
                self.view_mode = "single"
                self.single_loading = False
                self.image_cache.clear()
                self._log(f"Showing single beam {beam}", "ready", False)
            self.schedule_spectrum()
        except Exception as error:
            with self.lock:
                if (
                    request_token != self._view_request_token
                    or load_token != self.load_token
                    or aggregate is not self.aggregate
                ):
                    return
                self.single_loading = False
                self.view_mode = "mean"
                self._log(f"SINGLE BEAM ERROR: {error}", "ready", False)
            raise

    def _push_undo(self) -> None:
        self.undo_stack.append(copy.deepcopy(self.document))
        self.undo_stack = self.undo_stack[-50:]

    def edit(self, payload: dict[str, Any]) -> None:
        with self.lock:
            if self.aggregate is None:
                raise ValueError("Data are still loading")
            mode = str(payload["mode"])
            pol = int(payload["pol"])
            targets = (0, 1) if bool(payload.get("both")) else (pol,)
            x0, x1 = sorted((float(payload["f0"]), float(payload["f1"])))
            start, stop = sorted((int(np.floor(payload["t0"])), int(np.ceil(payload["t1"]))))
            x0 = max(float(self.frequency[0]), x0)
            x1 = min(float(self.frequency[-1]), x1)
            start = max(0, start)
            stop = min(len(self.mjd), stop)
            if x1 <= x0 or stop <= start:
                raise ValueError("Empty selection")
            self._push_undo()
            if self.view_mode == "single":
                layers = self.document.beam_polarizations.setdefault(
                    self.selected_beam,
                    [PolarizationMask(), PolarizationMask()],
                )
                scope_label = self.selected_beam
            else:
                layers = self.document.polarizations
                scope_label = "Mean"
            for target in targets:
                item = layers[target]
                if mode == "frequency":
                    item.frequency_ranges_mhz.append([x0, x1])
                elif mode == "time":
                    item.time_ranges_records.append([start, stop])
                elif mode == "box":
                    item.boxes.append({"frequency_mhz": [x0, x1], "records": [start, stop]})
                elif mode == "delete":
                    item.frequency_ranges_mhz = [r for r in item.frequency_ranges_mhz if r[1] < x0 or r[0] > x1]
                    item.time_ranges_records = [r for r in item.time_ranges_records if r[1] <= start or r[0] >= stop]
                    item.boxes = [b for b in item.boxes if b["frequency_mhz"][1] < x0 or b["frequency_mhz"][0] > x1 or b["records"][1] <= start or b["records"][0] >= stop]
                else:
                    raise ValueError(f"Unknown edit mode: {mode}")
            normalize_document(self.document)
            self.document.done = False
            self.dirty = True
            self.revision += 1
            self._log(f"Manual {mode} edit added to {scope_label} ({'both pols' if len(targets) == 2 else f'pol {pol}'})", "ready", False)
        self.schedule_spectrum()

    def save_summary(self) -> dict[str, object]:
        with self.lock:
            return mask_document_region_summary(copy.deepcopy(self.document))

    def undo(self) -> None:
        with self.lock:
            if not self.undo_stack:
                raise ValueError("Nothing to undo")
            self.document = self.undo_stack.pop()
            self.dirty = True
            self.revision += 1
            self._log("Undid the last manual edit", "ready", False)
        self.schedule_spectrum()

    def revert(self) -> None:
        with self.lock:
            self.document = load_mask_document(mask_path(self.args.mask_dir, self.group), self.group)
            if self.aggregate is not None:
                self.document.auto_mask_files = self.aggregate.auto_mask_files
            self.undo_stack.clear()
            self.dirty = False
            self.revision += 1
            self._log("Reverted to the saved manual mask", "ready", False)
        self.schedule_spectrum()

    async def save(self) -> list[str]:
        with self.lock:
            if self.aggregate is None:
                raise ValueError("No loaded data")
            document = copy.deepcopy(self.document)
            document.done = True
            document.auto_mask_files = self.aggregate.auto_mask_files
            group = self.group
            frequency = self.frequency.copy()
            mjd = self.mjd.copy()
            self._log("Saving manual JSON and 19 per-beam NPZ products", "saving", True)
        json_target = mask_path(self.args.mask_dir, group)

        def write() -> list[Path]:
            save_mask_document(json_target, document)
            return save_beam_mask_products(self.args.mask_dir, group=group, frequency_mhz=frequency, mjd=mjd, document=document, json_path=json_target)

        try:
            paths = await asyncio.get_running_loop().run_in_executor(self.executor, write)
            with self.lock:
                self.document = document
                self.dirty = False
                self.revision += 1
                self._log(f"Saved {json_target.name} + {len(paths)} beam NPZ files", "ready", False)
            return [str(json_target), *map(str, paths)]
        except Exception as error:
            self._log(f"SAVE ERROR: {error}", "ready", False)
            raise

    def schedule_spectrum(self) -> None:
        with self.lock:
            if self.aggregate is None:
                return
            # One driver owns at most one queued/running calculation. Edits
            # arriving meanwhile replace its next request rather than adding
            # another full-array calculation to the data executor.
            self._spectrum_request_token += 1
            if self._spectrum_task is not None and not self._spectrum_task.done():
                return

        async def calculate_latest() -> None:
            try:
                while True:
                    with self.lock:
                        token = self._spectrum_request_token
                    await asyncio.sleep(0.18)
                    with self.lock:
                        if self.aggregate is None:
                            return
                        if token != self._spectrum_request_token:
                            continue
                        aggregate = self.aggregate
                        load_token = self.load_token
                        document = copy.deepcopy(self.document)
                        frequency = self.frequency
                        single = self.single

                    def calculate_all():
                        # Loading/saving can occupy the executor during the
                        # debounce. Skip obsolete work before touching arrays.
                        with self.lock:
                            if (
                                token != self._spectrum_request_token
                                or aggregate is not self.aggregate
                                or load_token != self.load_token
                            ):
                                return None
                        mean_result = {
                            False: masked_time_average(aggregate.raw_mean, frequency, document),
                            True: masked_time_average(aggregate.auto_masked_mean, frequency, document),
                        }
                        single_result = None
                        if single is not None:
                            single_result = {
                                False: masked_time_average(
                                    single.raw, frequency, document, beam=single.beam,
                                ),
                                True: masked_time_average(
                                    single.raw, frequency, document,
                                    additional_mask=single.union_mask, beam=single.beam,
                                ),
                            }
                        return mean_result, single_result

                    result = await asyncio.get_running_loop().run_in_executor(
                        self.executor, calculate_all
                    )
                    with self.lock:
                        if self.aggregate is None:
                            return
                        if (
                            result is None
                            or token != self._spectrum_request_token
                            or aggregate is not self.aggregate
                            or load_token != self.load_token
                        ):
                            continue
                        self.spectra, single_result = result
                        if single is self.single and single_result is not None:
                            self.single_spectra = single_result
                        # Save changes the UI/status revision but not the
                        # numerical mask content. It must not invalidate this
                        # calculation without requesting a replacement.
                        self.spectrum_content_revision = token
                        self.spectrum_revision += 1
                        return
            except Exception as error:
                self._log(f"SPECTRUM ERROR: {error}")
                traceback.print_exc()
            finally:
                with self.lock:
                    self._spectrum_task = None

        self._spectrum_task = asyncio.create_task(calculate_latest())

    def spectrum_payload(
        self,
        apply_auto: bool,
        view_mode: str = "mean",
        beam: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            view_mode = str(view_mode).lower()
            empty = np.empty((0, 2), dtype=np.float64)
            mean = getattr(self, "spectra", {}).get(bool(apply_auto), empty)
            mean_medians = getattr(self, "local_medians", [0.0, 0.0])
            if view_mode == "single":
                requested_beam = str(beam or self.selected_beam).upper()
                if self.single is None or self.single.beam != requested_beam:
                    selected = empty
                    medians = [0.0, 0.0]
                else:
                    selected = self.single_spectra[bool(apply_auto)]
                    medians = self.single_medians
                view_label = requested_beam
            else:
                selected = mean
                medians = mean_medians
                view_mode = "mean"
                view_label = "19-beam Mean"

            def serialized_pol(values: np.ndarray, offsets: list[float], pol: int) -> list[float | None]:
                if not len(self.frequency) or values.shape != (len(self.frequency), 2):
                    return []
                restored = values[:, pol] + offsets[pol]
                return [float(value) if np.isfinite(value) else None for value in restored]

            payload = {
                "revision": self.spectrum_revision,
                "content_revision": getattr(self, "spectrum_content_revision", 0),
                "group_key": self.group.key if getattr(self, "groups", None) else None,
                "apply_auto": bool(apply_auto),
                "view_mode": view_mode,
                "view_label": view_label,
                # Keep every channel: viewport-only zoom is performed locally
                # and must not permanently omit narrow RFI or NaN gaps.
                "frequency": self.frequency.tolist(),
                "pol0": serialized_pol(selected, medians, 0),
                "pol1": serialized_pol(selected, medians, 1),
            }
            payload["selected_ready"] = bool(payload["pol0"])
            if view_mode == "single":
                payload["mean_pol0"] = serialized_pol(mean, mean_medians, 0)
                payload["mean_pol1"] = serialized_pol(mean, mean_medians, 1)
                payload["mean_ready"] = bool(payload["mean_pol0"])
                payload["mean_scope"] = "global_manual_only"
            return payload

    def render(
        self, params: dict[str, Any]
    ) -> tuple[bytes, float, float, float, float]:
        with self.lock:
            if self.aggregate is None:
                raise ValueError("Data are still loading")
            pol = int(params["pol"])
            f0, f1 = sorted((float(params["f0"]), float(params["f1"])))
            t0, t1 = sorted((float(params["t0"]), float(params["t1"])))
            x0 = max(0, int(np.searchsorted(self.frequency, f0, side="left")))
            x1 = min(len(self.frequency), int(np.searchsorted(self.frequency, f1, side="right")))
            y0, y1 = max(0, int(np.floor(t0))), min(len(self.mjd), int(np.ceil(t1)))
            x1, y1 = max(x0 + 1, x1), max(y0 + 1, y1)
            apply_auto = bool(int(params.get("apply_auto", 1)))
            show_auto = bool(int(params.get("show_auto", 1)))
            width, height = int(params.get("width", 1000)), int(params.get("height", 300))
            contrast = int(params.get("contrast", 0)) % 4
            auto_width = max(0, min(int(params.get("auto_width", 1)), 4))
            vmin = float(params["vmin"]) if "vmin" in params else None
            vmax = float(params["vmax"]) if "vmax" in params else None
            view_mode = str(params.get("view_mode", "mean")).lower()
            beam = str(params.get("beam", self.selected_beam)).upper()
            if view_mode == "single":
                if self.single is None or self.single.beam != beam:
                    raise ValueError(f"Single beam {beam} is not loaded")
                values = self.single.raw[:, :, pol]
                value_mask = self.single.union_mask[:, :, pol] if apply_auto else None
                auto = self.single.union_mask[:, :, pol] if show_auto else None
                auto_total_beams = 1
            else:
                view_mode = "mean"
                beam = ""
                values = self.aggregate.auto_masked_mean[:, :, pol] if apply_auto else self.aggregate.raw_mean[:, :, pol]
                value_mask = None
                auto = self.aggregate.union_flagged_beam_count[:, :, pol] if show_auto else None
                auto_total_beams = 19
            key = (self.revision, view_mode, beam, pol, x0, x1, y0, y1, width, height, apply_auto, show_auto, contrast, auto_width, vmin, vmax)
            cached = self.image_cache.get(key)
            if cached is not None:
                return cached
        result = render_view_image(
            values,
            auto,
            y0=y0,
            y1=y1,
            x0=x0,
            x1=x1,
            width=width,
            height=height,
            contrast=contrast,
            auto_width=auto_width,
            value_mask=value_mask,
            auto_total_beams=auto_total_beams,
            vmin=vmin,
            vmax=vmax,
        )
        with self.lock:
            if len(self.image_cache) >= 20:
                self.image_cache.pop(next(iter(self.image_cache)))
            self.image_cache[key] = result
        return result


class BaseHandler(tornado.web.RequestHandler):
    @property
    def state(self) -> NativeMaskState:
        return self.settings["mask_state"]

    def write_json(self, value: Any, status: int = 200) -> None:
        self.set_status(status)
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.write(json.dumps(value, allow_nan=True))


class IndexHandler(BaseHandler):
    def get(self) -> None:
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.set_header("Cache-Control", "no-store, max-age=0")
        self.write(self.settings["ui_html"])


class StateHandler(BaseHandler):
    def get(self) -> None:
        self.write_json(self.state.public_state())


class SpectrumHandler(BaseHandler):
    def get(self) -> None:
        self.write_json(
            self.state.spectrum_payload(
                bool(int(self.get_argument("apply_auto", "1"))),
                self.get_argument("view_mode", "mean"),
                self.get_argument("beam", None),
            )
        )


class ViewHandler(BaseHandler):
    async def get(self) -> None:
        try:
            data, low, high, auto_low, auto_high = await asyncio.get_running_loop().run_in_executor(self.state.render_executor, self.state.render, {key: self.get_argument(key) for key in self.request.arguments})
            self.set_header("Content-Type", "image/jpeg")
            self.set_header("Cache-Control", "no-store")
            self.set_header("X-Color-Low", f"{low:.12g}")
            self.set_header("X-Color-High", f"{high:.12g}")
            self.set_header("X-Auto-Color-Low", f"{auto_low:.12g}")
            self.set_header("X-Auto-Color-High", f"{auto_high:.12g}")
            self.write(data)
        except Exception as error:
            self.send_error(503, reason=str(error))


class ActionHandler(BaseHandler):
    async def post(self) -> None:
        try:
            payload = json.loads(self.request.body or b"{}")
            action = payload.get("action")
            result: dict[str, Any] = {}
            if self.state.shutting_down or self.state.navigation_busy or (self.state.busy and action != "save_summary"):
                raise ValueError("正在加载、保存或切换条目，请等待完成。")
            if action == "navigate":
                result.update(await self.state.navigate(
                    str(payload.get("direction", "")), str(payload.get("resolution", "ask")),
                    str(payload.get("group_key", "")), payload.get("revision"),
                ))
            elif action == "edit":
                self.state.edit(payload)
            elif action == "undo":
                self.state.undo()
            elif action == "revert":
                self.state.revert()
            elif action == "save":
                result["paths"] = await self.state.save()
            elif action == "save_summary":
                result["summary"] = self.state.save_summary()
            elif action == "set_view":
                await self.state.set_view(
                    str(payload.get("view_mode", "mean")),
                    payload.get("beam"),
                )
            elif action in {"previous", "next", "load"}:
                if action == "previous":
                    index = self.state.index - 1
                elif action == "next":
                    index = self.state.index + 1
                else:
                    index = int(payload["index"])
                asyncio.create_task(self.state.load(index))
            else:
                raise ValueError(f"Unknown action: {action!r}")
            result["state"] = self.state.public_state()
            self.write_json(result)
        except Exception as error:
            self.write_json({"error": str(error), "state": self.state.public_state()}, 400)


class ShutdownHandler(BaseHandler):
    async def post(self) -> None:
        try:
            if self.request.remote_ip not in {"127.0.0.1", "::1"}:
                raise PermissionError("只允许本机启动器请求关闭。")
            payload = json.loads(self.request.body or b"{}")
            self.state.prepare_shutdown(payload, self.request.headers.get("X-RFI-Control-Token", ""))
        except PermissionError as error:
            self.write_json({"error": str(error)}, 403)
            return
        except (ValueError, TypeError) as error:
            self.write_json({"error": str(error)}, 409)
            return
        self.write_json({"stopping": True, "pid": os.getpid(), "instance": self.state.session_instance})
        self.finish()
        tornado.ioloop.IOLoop.current().call_later(0.1, self.settings["shutdown_callback"])


def register_session_control(state: NativeMaskState) -> Path:
    directory = Path.home() / '.cache/crafts-rfi/sessions'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.stat()
    if directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f'控制目录必须属于当前用户且仅本人可访问：{directory}')
    record = directory / f'{state.args.port}-{os.getpid()}.json'
    with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='.session-', delete=False) as stream:
        json.dump({'protocol': 1, 'pid': os.getpid(), 'instance': state.session_instance,
                   'port': state.args.port, 'token': state.control_token,
                   'editor': str(Path(__file__).resolve())}, stream)
        temporary = Path(stream.name)
    temporary.replace(record)
    return record


def remove_session_control(record: Path | None, state: NativeMaskState) -> None:
    if record is not None:
        try:
            if json.loads(record.read_text()).get('instance') == state.session_instance:
                record.unlink()
        except (OSError, ValueError):
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", help="nfile, group key, or SCAN:NFILE")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--mask-dir", type=Path, default=DEFAULT_MASK_DIR)
    parser.add_argument("--one-channel-dir", type=Path)
    parser.add_argument("--sat1380-dir", type=Path)
    parser.add_argument("--require-all-auto-masks", action="store_true")
    parser.add_argument("--block-records", type=int, default=64)
    parser.add_argument("--port", type=int, default=8992)
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--ui-html", type=Path, default=Path(__file__).with_name("crafts_tod_native_ui.html"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    state = NativeMaskState(args)
    app = tornado.web.Application(
        [(r"/", IndexHandler), (r"/api/state", StateHandler), (r"/api/spectrum", SpectrumHandler), (r"/api/view.jpg", ViewHandler), (r"/api/action", ActionHandler), (r"/api/control/shutdown", ShutdownHandler)],
        mask_state=state,
        ui_html=args.ui_html.read_text(encoding="utf-8"),
        compress_response=True,
    )
    server = app.listen(args.port, address=args.address)
    loop = tornado.ioloop.IOLoop.current()
    def stop_server():
        server.stop()
        loop.stop()
    app.settings['shutdown_callback'] = stop_server
    record = None
    try:
        record = register_session_control(state)
        print(f"Native UI listening on http://{args.address}:{args.port}/", flush=True)
        asyncio.get_event_loop().create_task(state.load())
        loop.start()
    finally:
        server.stop()
        state.executor.shutdown(wait=True)
        state.render_executor.shutdown(wait=True)
        remove_session_control(record, state)


if __name__ == "__main__":
    main()
