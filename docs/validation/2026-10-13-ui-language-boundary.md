# UI language boundary and validation

The delivered language setting supports Simplified Chinese (`zh-CN`), English
(`en-US`), and automatic detection (`localeOverride: null`). This is a real
desktop preference and updates the rendered UI and native menus. It does not
reload the page or use locale keys to remount the workbench.

## Reference contract

The installed Codex reference was `26.1007.2314.0`, read from its `app.asar`.
Read-only extracts are in
`output/playwright/codex-rebuild-20261012/locale-source/`.

- `general-settings-161845fad443.js`: the UI language setting writes
  `localeOverride`; automatic detection writes `null`; English uses `en-US`.
- `main-BklS_2Y2.js`: `locale-info` exposes Electron's app and system locales.
  Native localization prefers the override and otherwise uses `app.getLocale()`.
- `app-shared-737655e1fb23.js`: the preference's default is `null`.

MiniCode exposes only the two supplied language catalogs. Automatic detection
uses the desktop locale, or `navigator.language` in the browser. `zh`, `zh-CN`,
`zh-SG`, and `zh-Hans` resolve to Simplified Chinese; other detected locales use
English. The setting keeps the preference as `null` while detection is enabled.

## Implementation

`frontend/src.v2/i18n/locale.ts` owns detection. `i18n/index.tsx` supplies typed,
explicit catalogs and a React language context. Resources are split by the
existing area ownership into core, desktop, and root catalogs; these are
compiled into one catalog. There is no DOM text replacement, machine guessing,
or catch-and-return-source translation fallback.

`App.tsx` installs the desktop language provider. `AppLanguageBridge` shares the
selected locale with the three imperative dialogs and rendered Markdown widgets,
which already own separate React roots. Those dialogs keep their input state while their default labels
change. The Markdown editor changes its existing CodeMirror compartments for
placeholder, accessibility labels, and checkbox labels; it keeps its editor
instance, document, native undo, and per-document sessions.

Font CSS and the existing font-size values were not changed by this language
work. Chinese spacing around mixed text/numeric content was retained; model
counts and Diff expansion counts use complete templates.

## Covered areas

| Area | Connected UI |
| --- | --- |
| App and shell | Loading labels, titlebar, navigation, both sidebars, sessions, chat menus, workspace slots, welcome state, usage captions, panel loading, file tree/actions, bottom dock |
| Composer | Main input, model and effort menus, permissions, project selector, attachments, context references, add menu, queue, input history, skill/plugin picker chrome |
| Settings | Navigation and search; General/Appearance/browser/plugin/desktop areas owned by the desktop agent; Provider, Advanced, Personalization, Archived, Shortcuts, Voice, GitHub, feature flags, MCP tools, Git, migration, sandbox, project environment, scheduler, skills, connectors |
| File workspace | Editor actions/language labels, symbols, terminal toolbar/session labels, Git and delivery, artifact preview/PDF, preview service management, Diff and review, side chat, problems, project search/replace, Markdown editing |
| Chat | History/loading/search, context-card headings, quotes, user/assistant action buttons, generated-artifact controls, proposed-plan buttons, notices, inline Diff, approval/plan/question controls |
| Other chrome | Command palette, quick open, keyboard help, imperative dialogs, UI error surfaces, edit-review toolbar, Diff-editor loading |

Projectless tasks keep an empty project selection even when they have a real
`executionCwd`. Reveal/copy/open actions use that real directory. Workspace
grouping still uses project/worktree identity.

## Content preserved

User and model message bodies, generated-plan instructions, actual names,
attachment/file names, paths, source code, command arguments, raw terminal/tool
output, backend/provider diagnostics, MCP tool names/descriptions, and plugin
metadata remain their original content. Terminal status strings used internally
for output and lifecycle comparison also retain their authored values.

This is coverage of the listed UI regions, not a claim that every string in the
repository is translated. Unconverted secondary modules, static text supplied by
other runtime catalogs, date/relative-time helpers, and backend-authored data
labels require their own explicit UI-key contracts before being translated.

## Validation

- Initial core batch: 11 files, 276 cases after the added projectless case;
  all passed after the first six concrete integration/test issues were fixed.
- Final language batch: 31 files, 461 cases. 459 passed on the first run;
  the two failures were corrected and all 30 Diff cases plus all 20 Markdown
  cases passed on the targeted rerun.
- The language chain has five passing cases covering preference precedence,
  live desktop events, source-content preservation, stable composer elements,
  catalog parity, and the imperative dialog's independent root/draft.
- `tsc --noEmit` passed at the final implementation checkpoint.
- An additional SettingsCenter run passed 66/67 cases. The remaining fixture
  selected both visible and retained-hidden “高级” summaries; the desktop-area
  owner is fixing its selector. It was recorded as a failure, not a pass.

Production build and real Electron task/visual checks are coordinated by the
root task after this checkpoint. Language switching in a real isolated Electron
profile must include both directions, the native preference event, the unchanged
draft/attachments/message content, and `document.documentElement.lang`.

The normally running user's application/profile was not read, reloaded,
terminated, or used for language QA.
