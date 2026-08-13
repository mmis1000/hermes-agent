# Protected delegation images

These images are operator-built execution profiles for protected delegated tasks.
They are not selected by model-authored Docker arguments.

- `delegation/Dockerfile` provides a non-root source-tree worker with Python,
  Git, Node.js/npm, pytest, and PyYAML available before network isolation.
- `delegation-browser/Dockerfile` pins Playwright 1.55.0 and its matching Chromium image for headless browser work through terminal/code execution.

Protected runtime policy supplies structured reveals, an ephemeral filesystem, network/resource limits, and per-attempt identity. Host-launched `browser_*` and `computer_use` tools are intentionally not part of the browser profile. Screenshot, trace, and download paths must remain under the private workspace or an explicit RW reveal.

## Prepare the general worker image

Image preparation is optional. The commands in this section are a manual
operator/agent runbook, not automated test commands. Do not make Docker CLI or
daemon execution for these profile recipes part of the default test suite or a
required CI check: contributor environments are not guaranteed to have Docker.
Static tests inspect the Dockerfiles and configuration examples without
invoking Docker. Live Docker coverage remains available only through an exact
environment opt-in plus pytest's integration selector. Without the exact
environment opt-in, collection must not probe the Docker CLI or daemon; without
the marker selector, pytest does not execute the live tests.

When working in an environment intentionally equipped to prepare images, build
the text recipe manually. Do not commit an exported image, root filesystem,
OCI layout, or Docker save archive:

```bash
docker build \
  --file containers/delegation/Dockerfile \
  --tag hermes-delegation:local \
  .
```

The default recipe intentionally includes the tools a coding or validation
subagent repeatedly needs when its profile has `network: none`:

- `git`, including system `safe.directory=*` so a fixed non-root container UID
  can inspect an explicitly revealed repository owned by another host UID;
- `python`, `pytest`, and `PyYAML` for the repository's Python tests and config;
- `node` and `npm` for JavaScript inspection and test entrypoints;
- CA certificates for profiles whose operator policy permits network access.

This is a practical baseline, not a promise that every project dependency is
present. Add a derived Dockerfile for workflow-specific compilers, package
managers, or native libraries instead of installing packages at task time or
silently widening the generic profile.

As a separate optional manual step, probe a built image through its fixed
non-root user before referencing it in an execution profile:

```bash
docker run --rm --network none hermes-delegation:local sh -lc '
  test "$(id -u)" = 10001
  test "$(id -g)" = 10001
  git --version
  python --version
  python -m pytest --version
  python -c "import yaml; print(yaml.__version__)"
  node --version
  npm --version
  test "$(git config --system --get-all safe.directory)" = "*"
  test ! -e /var/run/docker.sock
  test ! -e "$HOME/.ssh"
'
```

To run the repository's optional live build/materialization coverage in an
environment intentionally equipped with Docker, opt in explicitly:

```bash
HERMES_RUN_OPTIONAL_DOCKER_PROFILE_TESTS=1 scripts/run_tests.sh \
  tests/integration/test_delegation_filesystem_isolation.py \
  tests/integration/test_delegation_scope_browser_profile.py \
  -m integration -q
```

Do not set this flag or integration selector in the default test suite or a
required CI check.

After publishing the image to an operator-controlled registry, configure the
execution profile with the immutable registry digest produced by that publish:

```yaml
delegation:
  filesystem_isolation:
    enabled: true
    allowed_profiles: [filesystem-isolated]
    profiles:
      filesystem-isolated:
        backend: docker
        image: registry.example/hermes-delegation@sha256:<published-digest>
        default_workdir: /workspace
        allowed_toolsets: [terminal, file, code_execution, vision]
        network: none
```

The repository contribution consists of reviewable text—the Dockerfile,
documentation/config samples, Docker-free static checks, and opt-in live test
code. The built image and its layers remain in the local daemon or registry;
they are not Git artifacts. If Docker is unavailable, leave the opt-in unset and
omit the manual build/probe; that does not block the normal test suite or use of
Hermes without this optional profile.
