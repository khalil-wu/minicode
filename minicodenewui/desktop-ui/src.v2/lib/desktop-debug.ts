// The debug menu is a build-flavor gate (dev/internal), separate from provider
// capabilities and the native devtools permission: the Vite dev build is the
// internal build; packaged/release builds are prod.
export const allowDebugMenu = (): boolean => import.meta.env.DEV;
