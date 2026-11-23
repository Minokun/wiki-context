# Multi-Vault Gateway, Authentication & Prefix ACL

`wctx gateway` exposes one or multiple vaults over **MCP Streamable HTTP (`2025-03-26`)** and lightweight **REST JSON endpoints** with zero host-shell exposure.

---

## 1. Hosting Multiple Vaults (and Security Boundary vs `wctx ui`)

> **Security Boundary (`wctx ui` vs `wctx gateway`)**:
> - `wctx ui` is a **local loopback (`127.0.0.1`) admin console** for onboarding vaults, issuing tokens, syncing local AI clients, and editing Layer-4 memory. Its `/api/*` admin endpoints do not use a login password (relying on loopback binding + CSRF protection).
> - `wctx gateway` is the **production multi-user team server**. It exposes **no admin UI or `/api/*` endpoints**—only `/healthz`, `/mcp*`, and `/v1/*`—and enforces SHA-256 Bearer Token authentication on every query/read route. When `--vault` is omitted, it automatically loads vaults and tokens managed in `~/.wiki-context/`.

```bash
# Explicit vault & token file:
wctx gateway \
  --vault eng=/srv/vaults/engineering \
  --vault sec=/srv/vaults/security \
  --token-file /etc/wiki-context/tokens.json \
  --host 127.0.0.1 --port 8090

# Or reuse vaults & tokens configured in `wctx ui` (~/.wiki-context/), and install as a background daemon:
wctx service install --target gateway --host 0.0.0.0 --port 8090

# On Computer B (teammate's machine): connect via Web UI ("🌐 连接远程知识库") OR 1-line CLI (auto-configures all local AI clients, zero snippet copy-paste):
wctx remote add --url http://192.168.1.25:8090 --token wctx_...
```

### HTTP Endpoints

| Method & Path | Auth | Description |
| :--- | :---: | :--- |
| `GET /healthz` | None | Liveness probe + active `generation_id` for every hosted vault |
| `POST /mcp` | Bearer | Streamable HTTP MCP endpoint for the default (first) vault |
| `POST /mcp/<vault_id>` | Bearer | Streamable HTTP MCP endpoint for a specific vault (e.g. `/mcp/sec`) |
| `GET /mcp[/<vault_id>]` | Bearer | Returns `405 Method Not Allowed` (`Allow: POST, DELETE`) per Streamable HTTP spec |
| `POST /v1/context` | Bearer | REST Context Pack (`{"vault": "sec", "question": "..."}`) |
| `POST /v1/read` | Bearer | Sandboxed mirror/wiki reader (`{"vault": "eng", "source": "...", "start": 1, "end": 80}`) |
| `GET /v1/vaults` | Bearer | List hosted vault IDs, names, and layouts |
| `GET /v1/whoami` | Bearer | Inspect authenticated `uid` and `groups` |

---

## 2. Token File & Directory-Prefix RBAC (`tokens.json`)

Both `tokens` and `acl` rules in `--token-file` are **hot-reloaded on file save** (revoking a token or tightening a rule takes effect on the very next request without restarting the gateway):

```json
{
  "tokens": [
    {
      "uid": "alice",
      "token_sha256": "2bb80d537b1da3e38bd30361aa855686bde0eacd7162fef6a25fe97bf527a25b",
      "groups": ["admin"]
    },
    {
      "uid": "contractor_bot",
      "token": "plain_secret_supported_for_dev_only",
      "groups": ["partner"],
      "allowed_vaults": ["eng"]
    }
  ],
  "acl": {
    "partner": {
      "allow": ["sources/public/**", "sources/specs/**"],
      "deny": ["sources/confidential/**", "sources/finance/**"]
    }
  }
}
```

> **Tip**: Run `wctx ui` to manage tokens (`token_sha256`-only storage), `allowed_vaults` vault scoping, role `acl` prefixes, and live ACL sandbox simulation visually.

### Provenance-Inherited Wiki Security

`PrefixACLPolicy` enforces two invariants:
1. **`deny` always overrides `allow`** on primary source paths (`wiki_context`, `wiki_search`, `wiki_read`, `wiki_list`).
2. **No Confidentiality Downgrade via Wiki Summaries**: A user can view a compiled `wiki/*.md` page **if and only if** they are authorized to access **every** primary document listed in that Wiki page's `sources:` frontmatter.

---

## 3. Nginx HTTPS Reverse Proxy Example

```nginx
server {
    listen 443 ssl http2;
    server_name kb.example.com;

    ssl_certificate     /etc/nginx/certs/kb.crt;
    ssl_certificate_key /etc/nginx/certs/kb.key;

    location /mcp {
        proxy_pass http://127.0.0.1:8090;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_buffering off;
        proxy_read_timeout 30s;
    }

    location /v1/ {
        proxy_pass http://127.0.0.1:8090/v1/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_buffering off;
        proxy_read_timeout 30s;
    }
}
```
