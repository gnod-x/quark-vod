# Contributing

Quark-VOD is developed as a portable Python service. Keep tests independent of real Quark accounts, OpenList credentials, private media libraries and operator AI services. The public package and the local home-server integration have separate ownership and validation paths.

## Development setup

Use Python 3.11 or newer. The CI matrix currently checks Python 3.11, 3.12 and 3.13. From the repository root:

```sh
python -m pip install -e '.[test]'
make test
python -m build --sdist --wheel
```

`make test` runs the standalone Python regression suite with synthetic data and local fixtures. It intentionally skips the two legacy host integration tests. CI checks Compose configuration, builds both distribution formats, installs the wheel and source distribution outside the source tree, verifies the CLI/API/package resources, and inspects the container's OCI version and revision labels before its non-root, read-only startup check.

## Version and release metadata

`app/quark_vod/__init__.py` is the package version source. Setuptools reads that value for wheel and source distribution metadata; the CLI and API health/OpenAPI responses use the same value. When creating a release, update the Dockerfile and Compose fallback defaults together; the consistency test fails if either drifts. Compose passes the selected image version into `VOD_VERSION`, and CI also passes the package version explicitly. Release builds should pass the package version as `VOD_VERSION` and the source commit as `VCS_REF` so OCI labels identify the exact artifact.

Runtime dependencies remain declared as supported compatibility ranges in `pyproject.toml`, not frozen to one machine's resolution. CI resolves those ranges on each supported Python version. Update dependency bounds intentionally and keep the 3.11–3.13 test matrix green; do not copy a local environment's exact package versions into the runtime contract.

Deployment-specific portal, host-job and playback-wrapper tests are maintained
with the deployment adapter. Its optional Makefile targets and fixtures are not
shipped in this standalone source tree or source distribution. Run those checks
from the deployment checkout when changing an integration.

## Publishing a release

1. Update the package version and matching Dockerfile/Compose defaults. Add public release notes at `docs/releases/<version>.md`; include migration and rollback requirements.
2. Merge the final commit to `main`. Wait for that exact commit's CI to succeed on Python 3.11, 3.12 and 3.13 and for the container job to pass. Do not tag an unverified or different commit.
3. Create and push an annotated `v<version>` tag pointing to that verified commit. The tag must match `app/quark_vod/__init__.py`; for example, package version `3.0.1` requires `v3.0.1`.
4. Watch the Release workflow. It checks out the pushed tag, builds the wheel and source distribution with Python 3.12, tests the shipped source tree, and verifies both installed distributions outside the checkout. Only then does its release job use GitHub's temporary token to create a GitHub Release.
5. Confirm the release points to the intended tag and contains exactly the wheel, source distribution, and `SHA256SUMS`. Download the assets and verify checksums before deployment.

The workflow grants `contents: write` only to its release job and makes credentials available only to the publication step. It never uploads operator delivery archives, host patches, credentials, or private logs. There is no PyPI or container-registry publication in this workflow.

Release creation fails if a release with that tag already exists. Investigate a failed publication before retrying; do not move a published tag or overwrite assets silently. GitHub Release success does not imply that a deployment was upgraded or that real playback was verified.

## Change and test expectations

- Preserve identity checks before quality ranking, durable recovery of uncertain transfers, bounded streaming resources and failure-aware storage lifecycle updates.
- Keep credential sources explicit, avoid logging secrets, and keep destructive maintenance disabled by default unless its protection checks are available.
- For search changes, cover invalid and partial results, response limits, provider failures, reload snapshots and cache separation. Treat installed Python providers as trusted code.
- Add a focused regression for a reproduced defect. Tests must use synthetic credentials and temporary state; they must not modify a running media library or contact a real account.
- Update the documentation that defines any changed public behavior. Validate wheel installation outside the source tree for packaging changes, and validate non-root/read-only startup and resource limits for container changes.
- Preserve existing source ownership and third-party notices. Do not add bundled binaries without the corresponding redistribution rights and notices.

Never commit cookies, tokens, signed media URLs, account databases, user state, private deployment paths, generated build output or local search configuration. See [security boundaries](SECURITY.md) before changing authentication, network access or maintenance behavior.
