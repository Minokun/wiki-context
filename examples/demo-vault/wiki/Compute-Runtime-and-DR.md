---
title: "Compute-Runtime-and-DR"
type: synthesis
status: reviewed
updated: 2026-11-22
sources:
  - sources/specs/adr-018-firecracker-runtime.md
  - sources/specs/dr-failover-runbook.md
source_hashes:
  "sources/specs/adr-018-firecracker-runtime.md": "1b328a3e8acaa9cea97813b6e3860c6974d7afb586c04c0e89bc9a5b862c4009"
  "sources/specs/dr-failover-runbook.md": "8b34bf6ea3c5b32f7c1de7e16042352adbf816504ddd9081bd7e57d8d0760bfd"
---

# Compute-Runtime-and-DR

## Overview

Covers AuroraEdge microVM cold-start snapshots (`ADR-018`) and multi-region disaster recovery failover targets (`dr-failover-runbook.md`).

## Key Facts

- **Cold-Start Target**: `firecracker-snapshot` memory diff restore keeps microVM p95 `cold-start` latency at `<= 115ms` (down from `1,400ms` cold kernel boot).
- **Multi-Region DR**: Automated DNS + Anycast health-check failover achieves `failover-rto` `<= 45s` and `RPO = 0` for synchronous metadata stores.

## Related Pages

[[Overview]]; [[Gateway-Rate-Limiting-and-SLOs]]

## Sources

- sources/specs/adr-018-firecracker-runtime.md
- sources/specs/dr-failover-runbook.md
