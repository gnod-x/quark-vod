# Quark-VOD unified development contract

This tree is the 3.2 alpha integration candidate. The package owns the cinema
management UI, playback core, optional hometheater jobs and source-only Jellyfin
wrappers. There is one implementation per feature; do not add imports from a
sibling hometheater checkout or restore app/jobs mirrors.

Read README.md, docs/architecture.md, docs/unified-cinema.md,
docs/automation.md and docs/unified-migration.md before changing the respective
contract. The old host is a deployment, not a source dependency.

Keep all automatic jobs, AI, Jellyfin and destructive maintenance opt-in.
Management APIs require the shared Bearer dependency. Model endpoint, primary,
fallbacks and reasoning settings are revisioned data; credentials stay in
explicit environment/files. A setting change applies to the next task snapshot.

Run scheduled work in independent bounded processes using fixed task IDs.
Preserve identity matching, exact-file save intents, library/work locks,
read-only WAL-aware external databases, watch/favourite protection and atomic
owner/mode/ACL-preserving metadata replacement. Unknown protection or cloud
write outcomes stop mutations. Never clear the recycle bin or replace later
user activity with an old database.

Validate changes with make test. UI and wiring checks are described in
CONTRIBUTING.md; tests use local fixtures, synthetic credentials and temporary
state. Packaging changes require isolated wheel/sdist installation and
non-root/read-only container checks. Include only source wrappers, never
third-party FFmpeg/jq binaries. Clean temporary processes, caches and build
directories, retaining deliberate release artifacts and verification evidence.

Development and isolated verification do not authorize a public release,
production deployment or replacement of a production cron table.
