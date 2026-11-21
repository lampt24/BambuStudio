# Updating upstream

Keep `upstream` pointed at bambulab/BambuStudio and `origin` at the fork.
AI code lives in `src/slic3r/GUI/AI/`; standalone services live in
`services/printpilot/`. The original files should contain only UI registration,
send entry-point calls and the CMake source list. Never reformat upstream files.

For each upstream upgrade, use a clean integration branch:

```sh
git fetch upstream --tags
git switch -c integrate/bambu-next feat/ai-auto-setting
git merge --no-commit <reviewed-upstream-tag>
```

Review any changed send entry points, including print-all, multi-machine,
queue and already-sliced files. Confirm that AI cancellation cannot send a job,
applying settings invalidates slicing, and unsupported G-code has an explicit
choice. Recheck config serialization and the allowlist on both client/server.
Run service tests and the PrintPilot native CI workflow before merging the
integration branch. Resolve conflicts in adapter code, not by rewriting
upstream implementations. No promise of zero conflicts is possible.

PrintPilot CI is in its own workflow. Native artifacts expire after one day;
dependency caches and intermediate archives are not uploaded by this workflow.
