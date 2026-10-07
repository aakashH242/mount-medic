# Optional Mount Medic application workflow

Use this route when Mount Medic is installed and the user wants its guarded workflow. If the user declines installation, use [native Linux tools](linux-recovery.md); do not fetch the repository or install packages to make the skill work.

For an explicit application-install request, consult the project's maintained [installation instructions](https://github.com/aakashH242/mount-medic#install). Use its guided installer: it previews missing dependencies and system changes, requests administrator authentication once, installs missing packages automatically, and asks separately about startup. Do not maintain another distro package list in this skill.

## Diagnosis

Use `mount-medic doctor --json` for capabilities and `mount-medic list --json` to identify the selected volume. Match the current hardware and filesystem identity, resolving ambiguous labels with the user. Listing discovers devices; it does not authorize probing all of them.

Run `mount-medic check VOLUME_ID --json` for the selected drive. This explicit read-only check does not enroll it. Bare `check` checks enrolled drives only. Do not start `gui` or `watch` for a diagnosis-only request: they can run previously authorized background actions. Use `mount-medic COMMAND --help` for the installed version's syntax.

Read fresh results and preserve exit status: 0 means success/no attention, 1 a finding requiring attention, and 2 unavailable/failed execution. Missing dependencies, inaccessible services, and incomplete checks do not establish that a drive is clean.

## Repair and permissions

The app permits repair only for eligible dirty-flag/unclean-journal cases after its checks pass. Mounted/busy devices, hibernation/cached metadata, I/O errors, mirror mismatch, unsupported layouts, and incomplete checks stop repair. Do not bypass a refusal by calling the engine, raw D-Bus methods, direct `ntfsfix`, or modifying its stored grants/attempts.

Preview a proposed repair with `mount-medic repair VOLUME_ID --dry-run --json`. This performs discovery only; it does not certify eligibility. The default repair clears the dirty flag and the Windows-check request. `--keep-dirty` retains/sets that request. Explain the selected behavior before a write.

After authorization for the identified drive, use `mount-medic repair VOLUME_ID` (or the agreed `--keep-dirty` variant) with normal CLI confirmation and administrator authentication. Leave user prompts to the user; noninteractive cancellation is not a reason to pipe confirmation or bypass the CLI. If necessary, give the exact command for their terminal.

Enrollment, automatic repair, automatic mounting, and manual actions are separate permissions. A one-time repair does not authorize enabling automatic behavior. Inspect existing configuration when relevant; do not change it silently.

A failed/interrupted/overdue attempt requires review, not an automatic `--retry`. The privileged repair may continue after the client exits: establish its outcome before any new attempt. Report repair and mounting separately, since repair can succeed while mounting fails. Device replacement or identity changes require fresh selection and authorization.
