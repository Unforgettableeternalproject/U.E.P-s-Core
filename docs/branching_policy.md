# Branching Policy

This project follows the same lightweight flow used by the neighboring U.E.P test projects.

## Permanent Branches

- `master`: stable baseline and integration checkpoint. Keep it releasable.
- `develop`: active development branch. New work starts here.

## Working Branches

Create feature branches from `develop`:

```powershell
git switch develop
git switch -c feature/<short-topic>
```

Use concise branch names:

- `feature/profiling-stage-timers`
- `feature/first-audio-pipeline`
- `feature/vocoder-benchmark`

Use `fix/<short-topic>` only for targeted bug fixes discovered after a feature has landed.

## Merge Direction

- `feature/*` -> `develop`
- `fix/*` -> `develop`
- `develop` -> `master` only after the test project has a verified stable checkpoint

## Verification Before Merge

At minimum, run:

```powershell
C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core\env\Scripts\python.exe -m pytest
C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core\env\Scripts\python.exe Entry.py smoke
```

For TTS runtime changes, also run a real generation smoke test and document the result in `docs/generation_parity.md` or a profiling note.
