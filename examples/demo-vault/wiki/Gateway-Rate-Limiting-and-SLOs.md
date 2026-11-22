---
title: "Gateway-Rate-Limiting-and-SLOs"
type: synthesis
status: reviewed
updated: 2026-11-22
sources:
  - sources/specs/rfc-042-edge-gateway.md
  - sources/specs/tier-quotas.csv
source_hashes:
  "sources/specs/rfc-042-edge-gateway.md": "aa2a7d5bd5ed2d2d83af91cc2f5127669c8da76b109d3fd15da4992fb3a21b6e"
  "sources/specs/tier-quotas.csv": "3fb80b1f5dc2629d004481d1f58ed3d03a3d4dc54b229de1ad1453d6593a6ecb"
---

# Gateway-Rate-Limiting-and-SLOs

## Overview

Synthesizes AuroraEdge Gateway v2 rate-limiting mechanics (`RFC-042`) and per-tier burst quotas (`tier-quotas.csv`).

## Key Facts

- **Token Bucket Enforcement**: Edge nodes enforce per-tenant token buckets using local atomic counters synced every `250ms`; exceeding burst capacity returns `HTTP-429` with a mandatory `Retry-After` header.
- **Latency SLO**: Gateway p99 proxy overhead must stay `<= 4.5ms` at `50,000 RPS` per node.
- **Enterprise Dedicated Tier**: Exact per-tier RPS and burst ceilings are defined in `sources/specs/tier-quotas.csv` (verify exact row values in primary table).

## Caveats

- gRPC streaming frame-level throttling (`RFC-049`) is still a draft and not yet deployed in production.

## Related Pages

[[Overview]]; [[Compute-Runtime-and-DR]]

## Sources

- sources/specs/rfc-042-edge-gateway.md
- sources/specs/tier-quotas.csv
