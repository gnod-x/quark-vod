# Contributing

Quark-VOD is developed as a portable Python service. Keep tests independent of real Quark accounts, OpenList credentials, private media libraries and operator AI services. The public package and the local home-server integration have separate ownership and validation paths.

## Development setup

Use Python 3.11 or newer. The CI matrix currently checks Python 3.11, 3.12 and 3.13. From the repository root:

```sh
python -m pip install -e '.[test]'
make test
python -m build
```

`make test` runs the standalone Python regression suite with synthetic data and
local fixtures. It does not require a sibling home-server repository or real
service credentials. The CI workflow also builds the wheel and source
distribution, installs the wheel outside the source tree, checks the packaged
admin page and CLI, and starts the container as a non-root user with a
read-only root filesystem and resource limits.

Deployment-specific portal, host-job and playback-wrapper tests are maintained
with the deployment adapter. They are outside this standalone release's source
tree and test targets.

## Change and test expectations

- Preserve identity checks before quality ranking, durable recovery of uncertain transfers, bounded streaming resources and failure-aware storage lifecycle updates.
- Keep credential sources explicit, avoid logging secrets, and keep destructive maintenance disabled by default unless its protection checks are available.
- For search changes, cover invalid and partial results, response limits, provider failures, reload snapshots and cache separation. Treat installed Python providers as trusted code.
- Add a focused regression for a reproduced defect. Tests must use synthetic credentials and temporary state; they must not modify a running media library or contact a real account.
- Update the documentation that defines any changed public behavior. Validate wheel installation outside the source tree for packaging changes, and validate non-root/read-only startup and resource limits for container changes.
- Preserve existing source ownership and third-party notices. Do not add bundled binaries without the corresponding redistribution rights and notices.

Never commit cookies, tokens, signed media URLs, account databases, user state, private deployment paths, generated build output or local search configuration. See [security boundaries](SECURITY.md) before changing authentication, network access or maintenance behavior.
