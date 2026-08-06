---
name: ship
description: Put DictationMic's PWA live and push the repo to GitHub. Use whenever Steve says "push it live", "ship it", "deploy", "push to GitHub", "make it live", "update the PWA", or asks for changes to reach his phone. Runs the full sequence — service-worker bump, mirror, Firebase deploy, live verification, commit, push.
---

# Ship DictationMic

"Push it live" and "push it to GitHub" both mean **all of it goes** — deploy *and*
repo, including work other agents left in the tree. Don't hold anything back and
don't ask which half he meant.

Run every step. Report at the end what went live and what got committed.

## 1. Bump the service worker

`web/sw.js` has `const CACHE = "dictmic-vNN";`. Increment NN.

The shell is stale-while-revalidate, so **without a bump the first launch after a
deploy still runs the old JS** and only picks up the new one on the launch after.
Skipping this is why a fix appears not to have worked.

## 2. Mirror web/ into the deploy staging directory

```powershell
robocopy web hosting /MIR /XD downloads /NJH /NJS /NDL /NP
```

- `hosting/` is deploy staging only. Never edit it directly — it is overwritten.
- `/XD downloads` protects `hosting/downloads/DictationMic-Windows.zip` (110 MB,
  over GitHub's file limit and not in git). `/MIR` would delete it otherwise.
- robocopy exit codes **0–7 are success**, not failure. In PowerShell:
  `if ($LASTEXITCODE -lt 8) { $global:LASTEXITCODE = 0 } else { throw }`

Then confirm the mirror took:
`diff -rq web hosting` should report only `Only in hosting: downloads`.

## 3. Deploy

```bash
firebase deploy --only hosting
```

Project is `dictationmic-sync` (from `.firebaserc`). Live at
https://dictationmic-sync.web.app

## 4. Verify against the live URL, not the deploy log

"Deploy complete" only means the upload finished. Actually fetch the files and
check the new code is in them:

```bash
curl -s https://dictationmic-sync.web.app/js/ui.js | grep -c "<something you just changed>"
curl -s https://dictationmic-sync.web.app/sw.js  | grep -n "const CACHE"
```

For a whole-file change, `diff` the fetched copy against the local one.

## 5. Commit and push

Commit to `main` (this repo's history is entirely direct-to-main) and
`git push origin main`.

- **Separate commits for unrelated changes**, even when shipping in one go —
  another agent's CSS and a Python fix are two commits, not one.
- Read the diff before writing the message. Describe what was wrong and why the
  change fixes it, in the repo's existing voice: plain sentences, no
  `feat:`/`fix:` prefixes.
- Sanity-check first: `python -m py_compile` for Python, `node --check` on a
  `.mjs` copy for ES modules, balanced braces for CSS.
- `.gitignore` already keeps out the signing keystore, `keystore.properties`,
  the APK, `settings.json`, `gemini.key`, `notes/`, and `hosting/`. Don't
  override it.

## Notes

- **Android is dead.** Steve wants the PWA only — it does email sign-in sync and
  works on iOS. Never build, rebuild, or offer an APK.
- The desktop app (`app.py`, `cloudsync.py`) is not part of a deploy — it ships
  as a rebuilt exe. Changes there only need the commit and push.
- If the working tree has changes from another agent, they go too. Say what they
  were in the summary so he knows what landed.
