"""โหลด config สถานีจาก config/stations.yml"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "stations.yml"


@dataclass
class Station:
    code: str
    name_th: str
    name_en: str
    enabled: bool
    url: str
    loop_gif: str | None
    lat: float
    lon: float
    range_km: float
    center_px: tuple[float, float] | None
    km_per_px: float | None
    plot_box: tuple[int, int, int, int]
    colorbar_box: tuple[int, int, int, int]
    footer_box: tuple[int, int, int, int]
    # ค่า default ที่ inherit มา
    n_colorbar_bands: int
    dbz_top: float
    dbz_bottom: float
    lab_tolerance: float
    min_blob_px: int
    drop_top_band: bool
    refine: bool
    refine_params: dict
    qc: bool
    qc_params: dict
    clutter_thresh: float
    projection: str

    @property
    def is_calibrated(self) -> bool:
        return self.center_px is not None and self.km_per_px is not None


def _to_tuple(v: Any) -> Any:
    return tuple(v) if isinstance(v, list) else v


def load_stations(path: Path | str = CONFIG_PATH) -> dict[str, Station]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {})
    out: dict[str, Station] = {}
    for code, cfg in raw["stations"].items():
        merged = {**defaults, **cfg}
        out[code] = Station(
            code=code,
            name_th=merged["name_th"],
            name_en=merged["name_en"],
            enabled=merged["enabled"],
            url=merged["url"],
            loop_gif=merged.get("loop_gif"),
            lat=merged["lat"],
            lon=merged["lon"],
            range_km=merged["range_km"],
            center_px=_to_tuple(merged.get("center_px")),
            km_per_px=merged.get("km_per_px"),
            plot_box=_to_tuple(merged["plot_box"]),
            colorbar_box=_to_tuple(merged["colorbar_box"]),
            footer_box=_to_tuple(merged["footer_box"]),
            n_colorbar_bands=merged["n_colorbar_bands"],
            dbz_top=merged["dbz_top"],
            dbz_bottom=merged["dbz_bottom"],
            lab_tolerance=merged["lab_tolerance"],
            min_blob_px=merged["min_blob_px"],
            drop_top_band=merged["drop_top_band"],
            refine=merged.get("refine", True),
            refine_params=dict(merged.get("refine_params") or {}),
            qc=merged.get("qc", True),
            qc_params=dict(merged.get("qc_params") or {}),
            clutter_thresh=float(merged.get("clutter_thresh", 0.6)),
            projection=merged.get("projection", ""),
        )
    return out


def get_station(code: str, path: Path | str = CONFIG_PATH) -> Station:
    stations = load_stations(path)
    if code not in stations:
        raise KeyError(f"ไม่พบสถานี {code!r} ใน {path} (มี: {list(stations)})")
    return stations[code]


def enabled_stations(path: Path | str = CONFIG_PATH) -> list[Station]:
    return [s for s in load_stations(path).values() if s.enabled]
