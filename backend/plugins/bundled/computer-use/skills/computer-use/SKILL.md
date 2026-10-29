---
name: computer-use
description: Work with running Windows applications using the independent MiniCode computer runtime.
---

Use the configured `cua_repl` tool. Its `cua` object is supplied by MiniCode; do not import an OpenAI native package or launch a helper yourself.

Start with `await cua.getState()` and select an exact returned running window with `await cua.getApp({window_id})`. Follow the documentation returned by that runtime. Take a screenshot of the selected window before deciding coordinates. Perform one input action, then observe that same window again. Keep each screenshot and input associated with its actual selected target and turn.

This implementation supports running Windows windows, screenshots, clicks, text, key chords and scrolling. It does not provide a browser API, accessibility tree, app launch or a macOS/Linux adapter. Do not invent unsupported capabilities or substitute whole-desktop capture for an unavailable window image.

Respect the user's instruction about which applications may be controlled. When the user prohibits computer control, do not call computer tools. Stop immediately when the user interrupts. Do not automate authentication, password managers or security permission dialogs. Keep unsubmitted user content intact and do not execute terminal commands through application UI.
