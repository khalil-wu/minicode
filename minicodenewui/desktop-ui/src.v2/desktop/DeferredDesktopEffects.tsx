import { useDesktopBehaviorPreferences } from "../hooks/useDesktopBehaviorPreferences";
import { useDesktopPets } from "../hooks/useDesktopPets";
import { useNativeDictation } from "../hooks/useNativeDictation";
import { useWorkspaceGit } from "../hooks/useWorkspaceGit";

/** Keep non-critical desktop observers out of the initial workbench module. */
export function DeferredDesktopEffects() {
  useDesktopBehaviorPreferences();
  useDesktopPets();
  useNativeDictation();
  useWorkspaceGit();
  return null;
}
