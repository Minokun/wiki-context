# Multi-Region Disaster Recovery & Failover Runbook

## 1. RTO and RPO Objectives

| Tier | Recovery Time Objective (`failover-rto`) | Recovery Point Objective (`RPO`) | Mechanism |
| --- | --- | --- | --- |
| Control Plane Metadata | `<= 45s` | `0s` (Zero data loss) | Raft 3-region quorum (`us-east`, `us-west`, `eu-central`) |
| Telemetry Analytics | `<= 15m` | `<= 60s` | Async object-store WAL shipping |

## 2. Automated Anycast Drain Procedure

1. Edge BGP health daemon probes regional `/healthz` every `3s`.
2. After `3` consecutive probe failures (`9s`), BGP community `65000:666` withdraws the degraded region from Anycast routing.
3. Active WebSocket connections receive `GOAWAY` frame with `10s` grace drain window.
