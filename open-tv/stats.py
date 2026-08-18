"""Read-only: return saved health stats from health.parquet (no probing)."""

# healthcheck (imported below) reads health.parquet via pyarrow.
import healthcheck


def main() -> dict:
    records = list(healthcheck._load_records().values())
    if not records:
        return {"records": [], "checked": 0, "responsive_now": 0, "health_pct": 100.0}
    ok = sum(1 for r in records if r["responsive_now"])
    return {
        "records": sorted(records, key=lambda r: -r["fail_pct"]),
        "checked": len(records),
        "responsive_now": ok,
        "health_pct": round(100.0 * ok / len(records), 1),
        "last_checked": max(r["last_checked"] for r in records),
    }


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
