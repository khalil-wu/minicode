# Project symbol catalogue

The Unicode emoji strings and search keywords in `project-emojis.json` were read as literal data from the installed Codex desktop `26.1013.4894.0` asset `webview/assets/browser-3b4e0de870ed.js`. The extraction merges its base `tt.emojis` catalogue and the source's `rt` additions, following the source `at` registration. The resulting 1,914 entries retain Unicode code points, English search keywords, and category identifiers.

Extraction: `output/codex-alignment-20261018/settings-source/extract-project-emojis.cjs`. It parses JavaScript syntax and accepts literal data only; it does not evaluate application modules. The desktop reference asset remains in the local audit directory. No license for unrelated desktop application code is asserted here.
