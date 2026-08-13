# Protected delegation images

These images are operator-built execution profiles for protected delegated tasks.
They are not selected by model-authored Docker arguments.

- `delegation/Dockerfile` provides a minimal non-root private workspace.
- `delegation-browser/Dockerfile` pins Playwright 1.55.0 and its matching Chromium image for headless browser work through terminal/code execution.

Protected runtime policy supplies structured reveals, an ephemeral filesystem, network/resource limits, and per-attempt identity. Host-launched `browser_*` and `computer_use` tools are intentionally not part of the browser profile. Screenshot, trace, and download paths must remain under the private workspace or an explicit RW reveal.
