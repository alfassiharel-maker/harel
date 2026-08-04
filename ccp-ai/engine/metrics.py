"""Savings and cost arithmetic.

Two rules govern this file, because an investor demo that inflates a number is
worth less than no demo at all:

1. Every ratio is measured on real bytes on real disk. Nothing here invents a
   compression figure — the caller passes in what was actually observed.
2. Every extrapolation is labelled as one, and carries the unit prices it used.
   `project_at_scale` takes a measured ratio and applies it to a hypothetical
   model size and download count. That is a projection, and the payload says so.

Unit prices, as of 2026-08. They are constants in one place so a reviewer can
change them and see every downstream number move.
"""

from __future__ import annotations

from dataclasses import dataclass

GIB = 1024**3
TIB = 1024**4

# AWS S3 internet egress, first-10-TB tier, us-east-1. The lowest credible public
# number for a distribution product; a CDN with committed volume lands lower, a
# provider serving from a single region lands higher.
EGRESS_USD_PER_GIB = 0.08

# AWS S3 Standard, first-50-TB tier, us-east-1, per GiB-month.
STORAGE_USD_PER_GIB_MONTH = 0.023

# fp16/bf16 weights: two bytes per parameter. This is what a 7B checkpoint
# actually weighs on a hub today.
BYTES_PER_PARAM_FP16 = 2


def fmt_bytes(n: int | float | None) -> str:
    """Binary-prefix formatting, matching what a storage bill is measured in."""
    if n is None:
        return "—"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(value) < 1024 or unit == "PiB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.2f} {unit}"
        value /= 1024
    return f"{value:,.2f} PiB"


@dataclass(frozen=True)
class StoreSavings:
    """Measured storage outcome for one repository."""

    raw_bytes: int
    stored_bytes: int
    baseline_gzip_bytes: int | None = None

    @property
    def saved_bytes(self) -> int:
        return self.raw_bytes - self.stored_bytes

    @property
    def savings_ratio(self) -> float | None:
        """None, not zero, when there is nothing stored yet. A fake 0% reads as a
        measurement; None reads as 'no data', which is the truth."""
        if self.raw_bytes == 0:
            return None
        return self.saved_bytes / self.raw_bytes

    @property
    def compression_factor(self) -> float | None:
        if self.stored_bytes == 0:
            return None
        return self.raw_bytes / self.stored_bytes

    @property
    def savings_ratio_vs_gzip(self) -> float | None:
        """Savings against a compressed baseline rather than against raw files.

        The honest comparison. Any registry can gzip a checkpoint; the question
        an investor should ask is what CCP adds beyond that, so the dashboard
        shows both.
        """
        if not self.baseline_gzip_bytes:
            return None
        return (self.baseline_gzip_bytes - self.stored_bytes) / self.baseline_gzip_bytes

    def as_dict(self) -> dict[str, object]:
        return {
            "raw_bytes": self.raw_bytes,
            "stored_bytes": self.stored_bytes,
            "saved_bytes": self.saved_bytes,
            "savings_ratio": self.savings_ratio,
            "compression_factor": self.compression_factor,
            "baseline_gzip_bytes": self.baseline_gzip_bytes,
            "savings_ratio_vs_gzip": self.savings_ratio_vs_gzip,
        }


def monthly_storage_usd(total_bytes: int) -> float:
    return (total_bytes / GIB) * STORAGE_USD_PER_GIB_MONTH


def egress_usd(total_bytes: int) -> float:
    return (total_bytes / GIB) * EGRESS_USD_PER_GIB


@dataclass(frozen=True)
class Projection:
    """A measured ratio applied to a hypothetical fleet. Explicitly a model."""

    params_billions: float
    variants: int
    downloads_per_variant: int
    measured_variant_ratio: float
    model_bytes: int
    baseline_storage_bytes: int
    ccp_storage_bytes: int
    baseline_egress_bytes: int
    ccp_egress_bytes: int

    def as_dict(self) -> dict[str, object]:
        base_store = monthly_storage_usd(self.baseline_storage_bytes)
        ccp_store = monthly_storage_usd(self.ccp_storage_bytes)
        base_egress = egress_usd(self.baseline_egress_bytes)
        ccp_egress = egress_usd(self.ccp_egress_bytes)
        return {
            "assumptions": {
                "params_billions": self.params_billions,
                "bytes_per_param": BYTES_PER_PARAM_FP16,
                "model_bytes": self.model_bytes,
                "variants": self.variants,
                "downloads_per_variant": self.downloads_per_variant,
                "measured_variant_ratio": self.measured_variant_ratio,
                "egress_usd_per_gib": EGRESS_USD_PER_GIB,
                "storage_usd_per_gib_month": STORAGE_USD_PER_GIB_MONTH,
                "note": (
                    "Storage and egress volumes are the measured per-variant ratio from this "
                    "repository applied to the model size above. Unit prices are AWS S3 list."
                ),
            },
            "storage": {
                "baseline_bytes": self.baseline_storage_bytes,
                "ccp_bytes": self.ccp_storage_bytes,
                "baseline_usd_month": base_store,
                "ccp_usd_month": ccp_store,
                "saved_usd_month": base_store - ccp_store,
                "saved_usd_year": (base_store - ccp_store) * 12,
            },
            "egress": {
                "baseline_bytes": self.baseline_egress_bytes,
                "ccp_bytes": self.ccp_egress_bytes,
                "baseline_usd": base_egress,
                "ccp_usd": ccp_egress,
                "saved_usd": base_egress - ccp_egress,
            },
            "savings_ratio": (
                1.0 - (self.ccp_egress_bytes / self.baseline_egress_bytes) if self.baseline_egress_bytes else None
            ),
        }


def project_at_scale(
    *,
    measured_variant_ratio: float,
    params_billions: float = 7.0,
    variants: int = 10,
    downloads_per_variant: int = 100_000,
) -> Projection:
    """Scale a measured per-variant ratio to a production fleet.

    `measured_variant_ratio` is stored/raw for a derived variant, taken from the
    live repository. A client that already holds the base pays only the delta on
    a variant download; the base itself is counted once per client in the
    baseline and once in CCP, so the base is *not* treated as free.
    """
    if not 0.0 < measured_variant_ratio <= 1.0:
        raise ValueError(f"measured_variant_ratio must be in (0,1], got {measured_variant_ratio}")
    if variants < 1 or downloads_per_variant < 1 or params_billions <= 0:
        raise ValueError("variants, downloads_per_variant and params_billions must be positive")

    model_bytes = int(params_billions * 1e9 * BYTES_PER_PARAM_FP16)
    variant_delta_bytes = int(model_bytes * measured_variant_ratio)

    # Storage: a registry keeps one full copy per variant. CCP keeps one base
    # plus one delta per variant.
    baseline_storage = model_bytes * variants
    ccp_storage = model_bytes + variant_delta_bytes * variants

    # Egress: today every download ships a whole checkpoint. With CCP a client
    # fetches the base once, then a delta per variant it wants.
    total_downloads = variants * downloads_per_variant
    baseline_egress = model_bytes * total_downloads
    ccp_egress = model_bytes * downloads_per_variant + variant_delta_bytes * total_downloads

    return Projection(
        params_billions=params_billions,
        variants=variants,
        downloads_per_variant=downloads_per_variant,
        measured_variant_ratio=measured_variant_ratio,
        model_bytes=model_bytes,
        baseline_storage_bytes=baseline_storage,
        ccp_storage_bytes=ccp_storage,
        baseline_egress_bytes=baseline_egress,
        ccp_egress_bytes=ccp_egress,
    )
