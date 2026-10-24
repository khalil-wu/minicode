# Codex desktop logical-pixel layout — 2026-10-24

This batch uses installed package **26.1016.8622.0 / app 26.1016.62342**. Static resources were read from the same ASAR recorded by `output/full-parity-audit-20261023/installed-assets.json`; extracted references and token scopes are in `output/layout-metrics-20261024/`. Application modules were parsed, never executed. Implementation workers used those resources; Root also observed the current desktop General and Home in an independent reference window and verified the current production MiniCode build. User zoom, font-family and saved sidebar preferences were preserved.

## Scope and measurements

All numbers below are CSS logical pixels at the existing root rem basis, independent of each window's zoom. Root confirmed the current Source zoom factor 1.3 and its actual Home composer around 958 screen pixels, agreeing with a **736 logical-pixel usable axis**. Mini's font family and authored text-size/zoom/sidebar-width settings remain unchanged.

| Surface | Installed Source consumer / scope | Previous Mini | This batch |
|---|---|---|---|
| Transcript/composer constrained axis | `Gbi/Wbi`, app-initial:55087; shared CSS Electron body 48rem outer width, border-box, 16px inline padding each side → **768 outer / 736 usable**. thread-scroll-layout footer uses the same wrapper. | ordinary 820px usable; inline-summary branch already736 | ordinary and inline both736; narrow widths subtract only32px once |
| Main toolbar | ordinary `--height-toolbar:46px`; the observed desktop application-menu branch uses scoped `--app-shell-header-height:52px` | min52 + 8px/20px padding | current desktop strip52px, ordinary46px token retained, 8px inline inset |
| Application menu chrome | `ApplicationMenuTopBar_etoz9` height36; Windows page surface adds8px top inset | one44px menu; no page inset | 36px menu +8px workspace inset; net workspace top remains44 |
| Page frame | `PageSurfaceLayout_etoz9`: trailing/bottom4px; base radius12px | no trailing/bottom gutter or whole-frame radius | 4px trailing/bottom,12px radius |
| Pane toolbar | shared `--height-toolbar-pane:40px`; `r$i(size=pane)` | no common pane token |40px semantic token established; consumer-specific browser/file inner toolbars await Root's matching live measurement rather than assuming every header is a pane |
| Header variants | normal46; `Layout[data-app-shell-application-menu-header]` switches to sidebar navigation header52 | right header always52 | desktop application-menu shell now sets the same data attribute and scoped header-height52 for main/right shared strip. Ordinary fallback46 and pane40 remain distinct |
| App rail | `MZc` size=xl40, icon lg20; rail px2 each side →56 total; gap2=8 | rail56, button44, gap10, icon20 | rail56, button40, gap8, icon20;12.5px scoped radius |
| Left sidebar width | JS `bZt/ib`: default340, clamp240–520; `vJa` retains saved preference | same default/clamp | unchanged, including Root-observed saved width around240 |
| Sidebar mode / rows | shared `.sidebar-navigation`: mode32, row30, row radius10. `ConversationSidebar_1uxfp` separately overrides rows36 behind its product/layout consumer. | rows34, mode inherited, header36 plus14 top padding | base rows30/mode32; header52 with internal extra top padding removed; no guessed36px feature activation |
| Shared tabs | `OQi` h-8=32; selected capture rounded-md; `T$i` gap-1=4 |30px tab, internal gap7 |32px tab, internal gap8, strip gap4; selected surface10px radius |
| Resize target | `ZCi` w-4=16, centered/outside hit area; visible divider1 | left6/right9 |16px targets centered on unchanged boundary |
| Transcript scroll | `thread-scroll-layout`: top inset32, transcript pb-8=32, px-toolbar16; Electron stable both-edge scrollbar gutter | inline24, bottom24, no source top inset, automatic gutter | inline16, block32, stable both-edge gutter |
| Codex transcript turn stack | local-conversation-thread:1890 normal Codex gap-3=12; Aeon uses a separate gap0 branch | global turn gap28 |12px normal turn gap |
| Turn items / grouped activity | `conversation-gap`: item16/grouped4; local-conversation-turn inserts actual item gaps | several extra user margins; process-stack gap2 | user extra margins removed, process/answer item inset16, grouped stack gap4 |
| Assistant prose | MarkdownRoot_1fgd0 line height = current font size × leading-relaxed1.625 |1.62 |1.625; font family and selected font-size scale unchanged |
| User bubble | user-message CSS width70%, py2.5=10/inline16, radius22 |72–76%,12/16, radius20 |70%,10/16,radius22 |
| Composer input/footer | shared Electron line20/min44; ComposerLayout empty-attachment inset8/8/6; input inline12; footer bottom8 and controls28 | textarea40, extra frame4/4/8, footer34, send32, attach28/34 | textarea44/line20, frame0/4/4, root14/8/8, footer/send/attach28; input's total inline inset12 |
| Composer surface | default composer-radius22; no layout border; stroke through elevation | radius24, border1, generic shadow | radius22, elevation stroke, no sizing border; Source-style bottom16 preserved |
| Home hero | `app-primary` DUt/rUt/uUt:56px terminal artwork,24px gap,28px normal heading with1.2 line height |36px framed monogram/22px artwork,12px gap,22–26px semibold heading | exact static terminal paths/viewBox,56px mark without a frame,24px gap,28px normal heading; current font-family and text-scale preference retained |

## Implementation owners

- Shell size/axis/frame rules: `shell/shell.css`; header base consumes the same small-toolbar token in `styles/components.css`.
- Existing final shell/chat/composer rules were edited in place in `styles/ui-polish.css`; no new override stylesheet was added. Settings/dialog rows in this shared file belong to the concurrent settings agent.
- Actual inline-summary axis: `chat/ChatContextCard.css`; message gutter behavior: `chat/MessageList.tsx`; turn/user geometry: `chat/cells/cells.css` and `agent-loop/styles/agent-loop.css`.
- Composer frame/footer: `composer/composer.css`, `composer/FooterRow.tsx`; right resizer: `shell/SidebarRight.tsx`.

## DOM rulers for Root's current build

Measure bounding rectangles and computed styles, dividing screen coordinates by the actual window zoom where needed; do not change either saved zoom to make sizes appear equal.

| Ruler | Mini selector | Expected logical measurement |
|---|---|---|
| Menu / page origin | `.header-bar`, `.mc-desktop-workspace` | menu36; workspace y44; trailing/bottom4 |
| Main / right toolbar | `.mc-workbench-toolbar`, `.mc-sidebar-right-header` | desktop application-menu branch52; ordinary fallback46 |
| Sidebar / mode / rows | `.mc-sidebar-heading`, `.mc-sidebar-mode-toggle`, `.mc-sidebar-action`, `.session-row-hover` |52/32/30/30 |
| Rail / glyphs | `.mc-navigation-rail`, `.mc-navigation-rail button`, `.mc-navigation-rail button > svg` |56/40/20 |
| Tabs / splitters | `.mc-sidebar-right-tab-frame`, `.mc-sidebar-left-resize-handle`, `.mc-sidebar-right-resize-handle` |32 high /16 target width |
| Reading / input axes | `.message-list-content`, `.composer-container` in the same `.chat-pane-main` |736 maximum each, aligned centers; inline-summary branch must remain736 |
| Scroll bounds | `.message-list-scroll` | inline16, block32; stable both-edge gutter |
| User bubble | `.agent-loop-user-cell .user-cell-wrap`, `.user-cell-bubble` |70% max,10px block padding,16px inline,22px radius |
| Composer internals | `.composer-textarea-frame`, `.composer-textarea`, `.composer-footer`, `.composer-send-btn`, `.composer-attach-btn` |0/4/4 frame padding; input min44/line20; controls28 |

## Full-suite file-workspace regressions addressed in the same batch

The existing Markdown/browser-preview tests still asserted the retired shared `previewArtifact` or mocked the old directory API. The actual path is Markdown/file entry → `openWorkspaceFileTab` with fixed owner → mounted `FileViewerPanel` → owner-bound `readFileWorkspacePreview/Original`. Fixtures now assert that path, exact browser API parameters, independent file content, owner isolation, unchanged main draft/editor, and retired-reader cancellation.

One actual issue was found: native media's reader omitted connection state from its effect. It now aborts on disconnect and reads again with the reconnected session while retaining the fixed file owner. This is a product fix, not an assertion relaxation.

Root owns unified validation. Suggested focused files: `chat/conversationBrowserPreview.chain.test.ts`, `panels/FilesPanel.test.tsx`, `chat/messages/MarkdownRenderer.test.tsx`, `panels/FileViewerPanel.test.tsx`, plus existing shell/chat/composer/layout suites as Root selects. No test/build/GUI pass is claimed here. Root can append current-build DOM/screenshot results and unresolved active-variant differences below.

Root's current unobstructed Source Home measurement confirms page surface y≈44 logical, main/left header52 and Home usable input736. The same application-menu-header scope was applied after that observation. The existing `audit-ui-alignment.mjs` capture now records the DOM rulers above, settings card/row/nav/toggle/button/menu boxes, computed font/padding/gap/border/radius/min/max dimensions, CSS zoom and native Electron zoom/bounds in its existing receipt. Its draft section also records read-only native-input/store/cache/editor traces without waiting between fill and navigation, so pending PM commit can be distinguished from snapshot or hydration overwrite. No second audit system, manual draft repair or removed assertion was introduced. Root reported 395 focused frontend checks and 7 desktop checks passing before this final scoped-header/instrumentation update; its diagnostic production audit is pending.

## Draft assertion diagnosis from the production diagnostic audit

Root's rebuilt, non-HMR production audit reproduced `'' != KEEP_A_DRAFT` at line405. The following excerpt of that run's read-only draft trace disproves a lost-draft/cache hypothesis. `output/playwright/ui-alignment-current/` is a rolling audit directory; later run receipts replace its current contents.

- Row2 /9282.8ms: native input has committed `KEEP_A_DRAFT` to store and the rich document before creation starts.
- Row6 /10756.6ms: owner A's workbench cache contains `KEEP_A_DRAFT`.
- Row17 /11709.1ms: owner B's workbench cache contains `KEEP_B_DRAFT`; A's cache remains correct.
- Row19 /11711.3ms: returning owner A has restored `KEEP_A_DRAFT` in store/rich/cache while the editor is still empty and hidden.
- Row23 /11916.2ms: the visible editor also contains `KEEP_A_DRAFT`. No draft value was lost or replaced.

The audit had waited for the backend owner/pending flag and a retry cell, then read `innerText()` from a composer that was still hidden during UI restoration. The existing script now separately waits for that actual composer to become visible before each original A/B draft assertion (return, reload, and return to B). It does not wait for the expected draft text, add sleeps, mutate draft state, remove assertions, or patch the production draft/store/hydration chain. The original fill→create/switch timing is unchanged. Minimum follow-up: Root reruns the same `audit-ui-alignment.mjs`; no production rebuild is needed for this final script-only fix because the52px/layout build was already used for the diagnostic run.

## Follow-up owner calibration: footer and titlebar

The diagnostic01 idle footer measured31.1px despite28px send/attach controls. `FooterRow`'s shared model/permission trigger style still added5px vertical padding around inherited19.5px text plus two0.8px snapped transparent borders:19.5+10+1.6=31.1. Source's `_Button_1ivww_2` uses `height:var(--button-size);padding:0 var(--button-gutter);line-height:1`, and its md dictionary maps to `--control-size-sm:1.75rem` (**28px**, not the32px control-size-md token). The trigger's actual owner style now uses0px vertical padding/line-height1; redundant inline display/alignment/gap were removed so existing model/permission CSS owns their layout. Internal button gap and leading-control gap use Source's4px tokens. Voice/dictation controls already use28px and zero block padding; no fixed footer height, max-height or overflow hiding was added.

Source `jqa/Pqa/j0i/kqa` uses6px navigation group gaps, md uniform28px buttons and the explicit sm16px icon override. Mini navigation is now28/6/16, including its existing sidebar trigger. The broad header-button min-height30 rule was removed because it incorrectly constrained menu and navigation roles together. Source `w1i` uses inner menu start4/end8, gap2; its labels use px10/py4, border1, text-base14, normal weight and leading-none. The same existing Mini owner rules now express those dimensions; the existing font family and user text scale remain in force. The Windows topbar's Source safe-start inset is6px.

The existing audit rulers now include the actual footer trigger/group/voice descendants and titlebar navigation/menu, so Root can verify which child determines the row height. The Home mark ruler targets the new `.workbench-empty-mark > svg`. These follow-up changes were not built or tested by this agent; Root's ongoing session50218 still uses its previously frozen build.

The icon-only attachment button was also removed from the text-pill padding selector: its previous28px box still inherited8px inline padding per side, unlike Source uniform controls. Its actual component now owns28px dimensions/zero padding, keeping the16px glyph free to use its real size.

## Current-build verification by Root

The production build and TypeScript check passed after all layout, footer, titlebar, parent-mailbox and Import-navigation edits. Bundle and UI budgets passed. The targeted chrome/MessageList/FooterRow batch passed124 cases; the Import/settings/language batch passed123. Earlier changed file-workspace, selected-token, terminal and side-chat regressions passed as part of395 focused cases. These batches overlap and are not added into a unique project test count. Backend and real-provider task results have their own acceptance reports.

Current production-renderer measurements at native zoom1 and DPR1.25:

| Ruler | Measured logical pixels |
|---|---|
| Application menu / current desktop strip |36 /52 |
| Composer maximum width / total height |736 /98 |
| Footer / attachment and send controls |28 /28 |
| Source-style Home heading |28px, weight400, line-height33.6px |
| Settings card / default switch |728 /32×20 |

The font-family remained `Anthropic Sans` with the existing CJK fallbacks. The independent reference window retained its original zoom1.3; sizes were compared in logical pixels, without scaling MiniCode to conceal different layout rules.

Production-renderer audit reruns retained the original behavior assertions. Three later audit assumptions were corrected without changing the corresponding product behavior: the full46px configuration-heading box may be adjacent to its toolbar while its text still has a positive gap; a1000px-tall compact settings page needs a real smaller viewport to exercise parent scrolling; and a rotating12px glyph must be measured before its paint transform. Failed receipts are retained as `output/layout-metrics-20261024/ui-audit-*-failure-receipt.json`; current evidence remains in the rolling UI audit directory. Static captures fast-forward finite animations and do not certify natural-motion completion.

## Full Linux renderer acceptance and follow-up

GitHub run40087735727 for2af69cd9 completed its production desktop UI/reconnect job successfully. Downloaded evidence is in `output/full-parity-audit-20261023/ci-ui-2af69cd9/`:34 checks,110 captures,0 renderer errors. It includes all17 visible settings pages at DPR1/1.25/1.5, the two theme-menu levels, actual configuration writes/editor repair, import progress/completion/file save/real filesystem failure, profile/help/chat submenus and automation forms, update/pause/delete confirmation. This does not claim hidden settings gates or every button in those pages were exercised.

The17 settings baselines at three DPRs and two custom-contrast captures were inspected at original resolution (53 images); no definite title clipping, control collision or horizontal overflow was found. That review found the Personalization Learn more link still gray; its actual owner now uses Source `text-info` in both normal and hover states. The follow-up production build and bundle/UI budgets passed. Remaining static captures have a separate review record; finite-animation screenshots still do not certify natural motion.

The full frontend job exposed two additional outdated fixtures (an explicit empty recipient receipt and a jsdom Blob passed into Node Response). Desktop unit tests passed418 with9 skipped; its cold-unload oracle then waited for an obsolete renderer Ctrl+R shortcut and missed the required Popout VM dependency. The corrected targeted cases passed, including two real reloads, native close commit and cold-process2MiB attachment recovery. A fresh CI head is required for these follow-ups. The separate Windows Node companion startup investigation remains unresolved and is not converted to a pass by the Linux renderer result.
