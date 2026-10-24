// Source JU.allowDebugMenu is a build-flavor gate (dev/internal), separate
// from provider capabilities and the native devtools permission. MiniCode's
// existing Vite dev build is its internal build; packaged/release builds are prod.
export const allowDebugMenu = (): boolean => import.meta.env.DEV;
