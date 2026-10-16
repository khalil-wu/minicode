/** An atomic move keeps the iframe's document, JS heap and message ports alive. */
export function moveAppContainer(host: HTMLElement, destination: HTMLElement) {
  if (host.parentElement === destination) return;
  (destination as HTMLElement & { moveBefore(node: Node, child: Node | null): void }).moveBefore(host, null);
}
