# VPS Validation Checklist

Do not execute these scenarios without explicit operator approval for SSH/staging access and fault injection.

## 1. Normal publish (baseline)

Status: pending

## 2. Restart — no overwrite/duplicate

Status: pending

## 3. Full queue (backpressure) — no data loss

Status: pending

## 4. SIGTERM during active work — shutdown_flush_done/shutdown_spool_done

Status: pending

## 5. Schema failure — quarantine accounting correct

Status: pending

## 6. Spool quota exceeded — fail-fast, no silent growth

Status: pending

## 7. Mount loss (clean disconnect via umount) — spool_written, recovery after restart

Status: pending

## 8. Slow/hanging mount (via iptables DROP, not umount) — write timeout triggers spool fallback

Status: pending

## 9. Recovery — spool correctly replays to mount after restoration

Status: pending

## 10. Final accounting check

`accepted_rows = published_rows + spooled_rows + quarantined_rows + pending_rows`

Status: pending

## 11. Mount loss with non-empty memory buffer before shutdown

Regression for the last discovered P0 blocker.

Status: pending

## 12. Canary — 1–2 hours continuous run, no injected faults, monitor logs/files

Status: pending
