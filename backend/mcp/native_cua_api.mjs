// MiniCode's computer API. Native operations run on the captured receiving host.
export function createCuaApi(request, nodeRepl) {
  const documentation = `MiniCode native Windows computer API (independent implementation).
await cua.getState() lists running visible windows; it does not capture the desktop.
let app = await cua.getApp({window_id: '<id from getState>'}) selects and requests access to that exact app/window.
await app.getState() returns title and physical window bounds.
await app.screenshot() emits the target window PNG and returns its dimensions.
await app.click({x,y,button:'left',click_count:1}); await app.move({x,y});
await app.type({text:'...'}); await app.keypress({keys:['CTRL','L']});
await app.scroll({x,y,delta:-120}); await app.focus();
Coordinates use physical pixels relative to the screenshot. Input requires Windows to grant foreground to that same target.
Selected targets belong to their original turn. Select again after turn completion or reset.
Only running-window control is implemented. No browser API, accessibility tree, app launch, audio, or macOS/Linux adapter is exposed.
PrintWindow can fail or return application-rendered blank/protected content; desktop capture is never substituted. Use nodeRepl.write for extra text.`;
  return Object.freeze({
    async getState() {
      const windows = await request('list_windows', {});
      nodeRepl.write({ documentation, windows });
      return windows;
    },
    async getApp({ window_id }) {
      const selected = await request('select_window', { window_id });
      const invoke = (method, args = {}) => request(method, { ...args, target: selected.target });
      nodeRepl.write({ app: selected.name, window_id: selected.window_id, title: selected.title,
        bounds: selected.bounds, documentation });
      return Object.freeze({
        window_id: selected.window_id, app_id: selected.app_id,
        async getState() { const state = await invoke('get_state'); nodeRepl.write(state); return state; },
        async screenshot() {
          const image = await invoke('screenshot');
          await nodeRepl.emitImage(image);
          return { width: image.width, height: image.height };
        },
        click: args => invoke('click', args), move: args => invoke('move', args),
        type: args => invoke('type_text', args), keypress: args => invoke('keypress', args),
        scroll: args => invoke('scroll', args), focus: () => invoke('focus'),
      });
    },
  });
}
