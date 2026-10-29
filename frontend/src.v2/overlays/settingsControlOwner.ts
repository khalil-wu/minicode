/** Moving to another settings page is not an authored control cancellation. */
export const isSettingsNavigationTarget = (target: EventTarget | null): boolean => target instanceof Element
  && Boolean(target.closest(".settings-center-tabs, .mc-navigation-rail"));
