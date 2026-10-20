# Desktop and agent alignment checkpoint — 2026-10-21

This checkpoint collects the local harness, conversation ownership, and desktop alignment work since the preceding GitHub delivery. The desktop reference is Codex 26.1013.4894.0 and the public source reference is commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`.

## Implemented scope

- Agent execution and projection: code-execution ownership, nested tool records, asynchronous delegation and follow-up, goals, cancellation, stream persistence, and conversation-switch recovery. Rejecting one queued follow-up seals its optimistic reply while preserving another live assistant and its work state in the same active, background, or side conversation. Replay fixtures exercise captured event sequences with anonymized local profile paths; a replay is not a new live provider run.
- Desktop shell: common navigation rail, nonmodal settings, keyboard and focus behavior, project sources and stable identity, real managed workspaces for projects without source folders, import, local environments, storage, and worktree settings.
- Browser: live downloads participate in updater restart checks, concurrent save paths do not collide, actual filenames and native zoom/download updates reach the UI, and autofill stays with its original frame, document, and form. Number shortcuts and configured external-browser clicks reach the native-page consumer.
- Plugins and Skills: readable inactive-plugin inventory stays separate from execution admission, discovery and counts agree, ordinary installed Skills share the full detail/actions, and declared plugin metadata and links reach their consumers. MCP external links follow the installed Source host schema.
- Local dictation: one recorder controller, original-target ownership, saved audio before transcription, twenty-record history and ID-based retry/download/delete, dictionary entries, and the Windows keyboard/session/input production path. Voice service requests and preferences belong to the local app backend even from a remote window; remote model profiles keep their original host. Provider navigation has an acknowledged pending intent independent of conversation navigation.
- Existing special fonts, workspace drafts, attachments, unsaved editor contents, and stable conversation/project identities are preserved. Local runtime data and audit outputs are excluded from the submission.

## Verification before submission

The final combined `tsc -b && vite build --base=./` passed. Protocol synchronization and tracked-diff whitespace checks passed. The protocol checker does not recursively prove managed-project payload mapping; REST, WebSocket, store, and execution-root consumers were also inspected directly.

Feature verification was performed in batches, with passing cases and targeted repairs recorded separately. Selected group evidence includes browser frontend/native 33/62, shell frontend/native 122/13, projects/shortcuts/message links frontend/backend 105/8, plugins frontend/backend 52/24, and final dictation frontend/backend 48/9. Native dictation local-ownership checks passed 58 cases with three additional provider-navigation lifecycle cases. These groups overlap and must not be summed into a repository-wide total. Twelve Windows symlink-privilege cases were skipped, not passed. Original failures and name-filtered cases remain distinguished in the local audit receipts.

This submission is also checked against the repository's complete GitHub Actions workflow; its run attached to the submitted commit is the authority for CI results. Local Windows checks do not substitute for Linux native builds, wheel cold installation, or the isolated cloud Electron suites.

## Completion boundaries

The confirmed implementation batch is complete; comprehensive desktop equivalence is not claimed. Remaining differences include seven browser content-setting consumers, quiet/expanded notification-request presentation, Source-private password bubbles and suggestions, and the complete native mixed file/directory selection contract. Native-page modifier clicks currently cover main-frame HTML anchors/areas, not iframe, SVG, or script navigation.

Fresh physical UI/focus/motion acceptance, live production DownloadItem pause/resume/completion, real Windows microphone/hotkeys/SendInput, OS clipboard/default-application behavior, and actual SSH/WSL voice acceptance remain separate from fixture and compilation results. Opening the latest build confirms startup, not those acceptance cases. Account login, authorization, and account-dependent official services are outside this checkpoint.

Source download resume requires a live DownloadItem; live downloads block updater restart with `download.active`. Automatic recreation across app restarts is not an established Source parity requirement and is not listed as a confirmed missing feature.
