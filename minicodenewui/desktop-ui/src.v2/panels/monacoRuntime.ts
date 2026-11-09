// Monaco 0.56 snapshots singleton registrations at the first standalone use.
// Colorization can happen before the editor and its language workers load.
// Register the native services used by internal/common/workers.js now; keep
// the actual editor contributions and language providers on their lazy paths.
import "monaco-editor/platform/actionWidget/browser/actionWidget.js";
import "monaco-editor/editor/common/services/treeViewsDndService.js";
import "monaco-editor/editor/contrib/codelens/browser/codeLensCache.js";
import "monaco-editor/editor/contrib/documentSymbols/browser/outlineModel.js";
import "monaco-editor/editor/contrib/gotoError/browser/markerNavigationService.js";
import "monaco-editor/editor/contrib/gotoSymbol/browser/symbolNavigation.js";
import "monaco-editor/editor/contrib/inlayHints/browser/inlayHintsController.js";
import "monaco-editor/editor/contrib/peekView/browser/peekView.js";
import "monaco-editor/editor/contrib/suggest/browser/suggestMemory.js";

export * from "monaco-editor/editor/editor.api.js";
