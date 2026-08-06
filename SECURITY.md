# Security policy and execution threat model

## Supported safe mode

EdgeVerdict treats dependency installation, build scripts, repository code, and
model-generated tests as untrusted. The supported hardened mode is:

```bash
docker build -f docker/Dockerfile.sandbox -t edgeverdict-sandbox:latest .
export EDGEVERDICT_EXECUTION_BACKEND=docker
export EDGEVERDICT_SANDBOX_NETWORK=none
edgeverdict prove
```

The host process may hold the model-provider key. The sandbox receives neither
that key nor the host environment. It receives only a small allowlist of
non-secret variables. The sandbox has no network, no host home mount, no Docker
socket, a read-only root filesystem, dropped capabilities, no-new-privileges,
and CPU, memory, process, file-size, and output limits.

## Important limitation: dependencies

The default `none` network policy is the only mode intended for hostile or
unknown repositories. It requires dependencies to already be available in the
sandbox image, vendored in the repository, or supplied through a separately
managed offline cache.

`EDGEVERDICT_SANDBOX_NETWORK=install` permits network access for recognized
package-install commands. Repository lifecycle and build hooks can execute
during installation, so this mode can exfiltrate data available inside the
sandbox and must be used only for repositories you trust. `all` is still less
safe and is intended only for debugging trusted projects.

## Database reach mode (`EDGEVERDICT_DB_URL`)

Setting `EDGEVERDICT_DB_URL` changes the boundary for the test phase
only: it attaches the test container (never the install) to a bridge
network with a host gateway and injects the URL as `DATABASE_URL`,
rewriting `localhost`/`127.0.0.1` to the container's host alias. The
consequence is explicit: model-generated test code gets network access
and a live database during the test phase.

Use it only with repositories you trust and a database that is
disposable by construction, a throwaway container or per-test databases
holding nothing you care about. Never point it at a database with real
data or shared credentials. All other hardening (read-only root,
non-root user, dropped capabilities, environment filtering, resource
limits) is unchanged, and with the variable unset the execution path is
identical to the default hardened mode.

## Unsafe local compatibility mode

Local execution is disabled by default. It requires both:

```bash
export EDGEVERDICT_EXECUTION_BACKEND=local
export EDGEVERDICT_ALLOW_UNSAFE_LOCAL=1
```

This runs repository code as your OS user and is not safe for untrusted code.

## Residual risks

Docker is a risk-reduction boundary, not a mathematical proof of isolation.
Keep Docker and the host kernel patched. For high-value or openly hostile input,
run the Docker daemon inside a disposable VM or replace the backend with a
microVM implementation. The writable repository bind mount is not backed by an
independent disk quota; run the worker on a disposable filesystem with a quota
when host disk exhaustion is in scope.

Never mount the Docker socket, SSH directory, cloud credentials, home directory,
or internal source trees into the sandbox. Never expose a secret-bearing job to
PR-controlled executables.

## Reporting vulnerabilities

Do not open a public issue for a suspected secret leak or sandbox escape. Use a
private security advisory in the fork or contact the maintainer privately.
