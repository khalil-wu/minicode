"use strict";

const NATIVE_MENU_KEYS = new Set(["file", "edit", "view", "help"]);

function popupApplicationMenu(menu, key, window) {
  return new Promise((resolve) => {
    menu.getMenuItemById(`minicode-menu-${key}`).submenu.popup({
      window,
      callback() {
        if (!window.isDestroyed()) window.focus();
        resolve();
      },
    });
  });
}

module.exports = { NATIVE_MENU_KEYS, popupApplicationMenu };
