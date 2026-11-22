# RFC-042: AuroraEdge Gateway v2 Rate Limiting & Circuit Breaking

- **Status:** Accepted
- **Version:** 2.4
- **Effective Date:** 2026-09-01

## 1. Architecture & Token Bucket Sync

Each AuroraEdge ingress worker maintains lock-free sharded `burst-bucket` counters in shared memory and gossips delta increments over UDP multicast every `250ms`.

When a tenant exceeds either sustained `rate-limit` RPS or instantaneous `burst-bucket` tokens:
1. The gateway immediately rejects the request with status `HTTP-429` (`Too Many Requests`).
2. Every `HTTP-429` response MUST include `Retry-After` (integer seconds) and `X-RateLimit-Reset` (Unix epoch millis).
3. Proxy p99 forwarding overhead MUST remain `<= 4.5ms` at `50,000 RPS` per ingress node.

## 2. Upstream Circuit Breaking

If an upstream origin pool returns `>= 5%` HTTP 502/503/504 errors over a rolling `10s` window (minimum `50` requests), the circuit breaker trips `OPEN` for `15s`, then probes `3` canary requests in `HALF-OPEN` state.
