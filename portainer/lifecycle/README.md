# Restricted Portainer lifecycle gateway

Feature implementation, **not deployed**. Portainer remains the central UI for
the local server, Odroid and AdGuard Pi. Its image/update policy is unchanged.

## Boundary

- Dashboard credential: list **all** containers and start/stop/restart them on
  the three configured environments, including containers created later there.
- Separate LiteLLM and ComfyUI-proxy credentials: inspect running state and
  start/stop only `llama-server`, `llama-companion`, `llama-bolt`, `comfyui` on
  local environment 3. No remote environment, restart or arbitrary list access.
- No create/delete/exec, configuration changes, arbitrary request bodies,
  query parameters, outbound destinations, forwarded headers or generic proxy.
- Listing is projected to dashboard metadata. Inspection returns only ID and
  running state. No environment variables, labels, mounts or raw upstream errors.
- TLS in both directions. Portainer's existing exact certificate is checked
  **before** the administrator credential is sent, without hostname assumptions.
  Client-to-gateway TLS verifies the `lifecycle-gateway` hostname normally.
- Fixed environment IDs prevent silent expansion to newly registered hosts.
  Review the policy if environments are removed/recreated with different IDs.

The gateway is still a privileged trust boundary: compromise of its process or
its administrator key can compromise all managed hosts. Application credentials
do not grant that authority. It has no Docker socket or host-published port and
runs non-root, read-only, capability-free with bounded workers and response sizes.
It shares an internal control network with its clients plus a separate egress
network to reach Portainer. That is isolation, not protection against a compromised
Docker administrator or host user who can read all secret files.

All-container dashboard control deliberately permits service disruption. The
UI asks for confirmation on stop/restart of names in `critical_containers`;
this is an accident-prevention prompt, **not** an authorization barrier. Review
actual remote agent/DNS names and add them to this warning list before deployment.
No containers are hidden or excluded from actions. Stopping Portainer, the gateway,
its agent, or the dashboard can disable the path needed to start it again. Keep
direct host SSH/console recovery available. Starting a pre-existing privileged
container can also have powerful side effects; start/stop is not risk-free.

## Provisioning (future, separately approved deployment)

Do not deploy from a feature worktree: existing stack bind mounts deliberately
point at deployed `/opt/stacks` paths. Review/merge the stacks and separate
dashboard feature branches together before following the rollout below.
No application restarts, live secrets or networks were changed by this feature.

1. Back up Portainer's data consistently using its supported backup workflow;
   confirm access to host SSH/console on all three machines.
2. Confirm IDs in `policy.example.json` against the central UI. Copy it to
   `/opt/secrets/lifecycle-policy.json`, owned by the deployment user UID 1000,
   mode `0600`. Replace each placeholder with an **independent** random token
   (`secrets.token_urlsafe(32)`). Do not commit or print tokens.
3. Create separate mode-0600 client env files, each containing only the matching
   `LIFECYCLE_API_KEY=...`:
   - `/opt/secrets/lifecycle-dashboard.env`
   - `/opt/secrets/lifecycle-litellm.env`
   - `/opt/secrets/lifecycle-comfyui-proxy.env`
   Check the other existing consumer env files do not also contain a legacy
   `PORTAINER_API_KEY`. Only the gateway may receive that key after migration.
4. Provision a private TLS key and certificate at
   `/opt/secrets/lifecycle-server.key` (0600, readable by UID 1000) and
   `/opt/secrets/lifecycle-server.pem`. The cert needs
   `subjectAltName=DNS:lifecycle-gateway`, serverAuth and an expiry/update plan.
   A dedicated self-signed certificate is sufficient for these pinned clients;
   never use the TLS private key as an API credential. All clients mount only the
   public certificate, never the private key. Existing Portainer pin remains at
   `/opt/secrets/portainer-ca.pem`.
   All four gateway-mounted files (policy, private key, server certificate and
   Portainer certificate) must be readable by its UID 1000; private files stay 0600.
5. Gateway Compose requires `LAN_IP` (e.g. supply the existing Portainer `.env`
   through `--env-file /opt/stacks/portainer/.env`). The gateway initially uses
   `/opt/secrets/portainer.env`. Prefer a separately provisioned migration token
   so the old shared token can be revoked independently after cutover. If reusing
   the old token for the trial, replace it in the gateway **before** revoking it.

No automatic token generation, network creation or secret-file overwrite is
performed by the code. Example placeholders intentionally fail startup.

## Staged rollout / recovery

1. After explicit deployment approval, build/start only gateway Compose first:
   `docker compose --env-file /opt/stacks/portainer/.env -f /opt/stacks/portainer/lifecycle/compose.yml up -d --build`.
   This creates `lifecycle_control`; clients require that external network.
2. From disposable test clients on that network, check TLS, dashboard listing on
   IDs 3/4/7 and state reads for GPU callers. Test denials for missing/wrong tokens,
   cross-host GPU requests, unapproved containers, exec/create/delete and payloads.
   Do not use a destructive live call as a denial test: use fake upstream fixtures
   first and an upstream non-admin migration identity if testing live negatives.
3. Recreate one consumer at a time using the matching source revision and new
   environment file/network/certificate mounts. Preserve all other env secrets,
   GPU mutual exclusion, idle timeout, Cloudflare and Portainer access. Test cold
   start, GPU handover, idle stop and dashboard actions on safe test containers.
   Restart remains supported in the dashboard to preserve its existing feature.
4. Check running consumer configurations contain `LIFECYCLE_API_KEY` but **not**
   `PORTAINER_API_KEY` (check presence, never print values). Remove stale legacy
   values from local development `.env` files too. A configured gateway failure
   never falls back to Portainer. Direct API mode remains only for legacy local
   development when `LIFECYCLE_URL` is absent; production Compose always sets it.
5. Revoke the old shared admin token only after all callers pass. Verify that
   old token is rejected. Keep the new gateway token out of application containers.

Before revocation, rollback is restoring previous consumer Compose/source and
recreating those consumers with their old configuration. After revocation, that
configuration will not work: recovery needs a deliberately reissued credential
or fixing the gateway. Never silently restore broad administrator access.

Emergency recovery is **host-side** `docker start lifecycle-gateway`,
`docker start portainer`, or starting the remote agent/dashboard on its host.
Do not rely on the gateway to restart itself after it has been stopped.

## Validation

```sh
python3 -B -m unittest discover -s portainer/lifecycle -p 'test_*.py'
```

Tests use dummy credentials and disposable localhost fixtures, not live Portainer.
They cover policy denials, metadata projection, immutable IDs, wire request
validation, exact TLS pinning and redirect rejection. Test the consumer suites
in both repositories as well. Offline results do not prove live network paths,
remote agents, GPU behavior or revocation; those are deployment acceptance gates.
