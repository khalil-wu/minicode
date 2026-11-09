/** Occurrence ordering leaves unknown/new records in their real base order. */
export const orderSidebarItems = <T,>(items: T[], identifiers: string[], id: (item: T) => string): T[] => {
  const positions = new Map(identifiers.map((identifier, index) => [identifier, index]));
  return [...items].sort((left, right) => (positions.get(id(left)) ?? identifiers.length) - (positions.get(id(right)) ?? identifiers.length));
};
