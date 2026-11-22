# ADR-018: Firecracker MicroVM Snapshot Restore for Serverless Workers

- **Status:** Accepted
- **Decision Date:** 2026-08-15

## Context

Cold booting a Linux 6.6 guest kernel plus language runtime inside a Firecracker microVM previously took `1,400ms` at p95, violating interactive webhook latency targets.

## Decision

We pre-bake copy-on-write `firecracker-snapshot` memory images per runtime image digest on NVMe local cache:
- `userfaultfd` lazy page-fault handler streams hot working-set pages (< `18 MiB`) within `35ms`.
- End-to-end p95 `cold-start` latency from HTTP arrival to worker handler execution is capped at `<= 115ms`.
- Snapshot entropy re-seeding (`virtio-rng` + `/dev/urandom` ioctl) is mandatory before network namespace activation to prevent PRNG state reuse across cloned microVMs.
