export type ElicitationChoice = { const: string; title: string };
export type ElicitationField = {
  type: "string" | "number" | "integer" | "boolean" | "array";
  title?: string; description?: string; default?: string | number | boolean | string[];
  enum?: string[]; enumNames?: string[]; oneOf?: ElicitationChoice[];
  items?: { enum?: string[]; anyOf?: ElicitationChoice[]; oneOf?: ElicitationChoice[] };
  minimum?: number; maximum?: number; minLength?: number; maxLength?: number;
  minItems?: number; maxItems?: number; format?: string;
};
export type ElicitationValues = Record<string, string | boolean | string[] | undefined>;

export const elicitationChoices = (field: ElicitationField) => {
  const titled = field.oneOf ?? field.items?.anyOf ?? field.items?.oneOf;
  return titled ?? (field.enum ?? field.items?.enum)?.map((value, index) => ({
    const: value, title: field.enumNames?.[index] ?? value,
  }));
};

export function elicitationDefaults(fields: [string, ElicitationField][], required: string[]): ElicitationValues {
  return Object.fromEntries(fields.flatMap(([name, field]) => {
    if (field.default !== undefined) return [[name, typeof field.default === "number" ? String(field.default) : field.default]];
    if (!required.includes(name)) return [];
    if (field.type === "boolean") return [[name, false]];
    if (field.type === "array") return [[name, []]];
    if (field.type === "string" && !elicitationChoices(field)) return [[name, ""]];
    return [];
  }));
}

// `required` means present. Empty text/selection is valid unless its actual
// field constraints exclude it; numeric input stays absent until it is entered.
export function elicitationContent(fields: [string, ElicitationField][], required: string[], values: ElicitationValues):
  { content: Record<string, string | number | boolean | string[]>; error?: string } {
  const content: Record<string, string | number | boolean | string[]> = {};
  for (const [name, field] of fields) {
    const value = values[name];
    const label = field.title || name;
    const absent = value === undefined || ((field.type === "number" || field.type === "integer") && value === "");
    if (absent) {
      if (required.includes(name)) return { content, error: `请填写${label}` };
      continue;
    }
    let valid: boolean;
    const choices = elicitationChoices(field)?.map(choice => choice.const);
    if (field.type === "number" || field.type === "integer") {
      const numeric = typeof value === "string" ? Number(value) : NaN;
      valid = typeof value === "string" && value.trim().length > 0 && Number.isFinite(numeric)
        && (field.type !== "integer" || Number.isInteger(numeric))
        && (field.minimum === undefined || numeric >= field.minimum)
        && (field.maximum === undefined || numeric <= field.maximum);
      content[name] = numeric;
    } else if (field.type === "boolean") {
      valid = typeof value === "boolean";
      content[name] = value;
    } else if (field.type === "array") {
      valid = Array.isArray(value) && value.every(item => typeof item === "string" && choices?.includes(item))
        && (field.minItems === undefined || value.length >= field.minItems)
        && (field.maxItems === undefined || value.length <= field.maxItems);
      content[name] = value;
    } else {
      const length = typeof value === "string" ? Array.from(value).length : 0;
      valid = typeof value === "string" && (!choices || choices.includes(value))
        && (field.minLength === undefined || length >= field.minLength)
        && (field.maxLength === undefined || length <= field.maxLength);
      content[name] = value;
    }
    if (!valid) return { content, error: `${label}不符合输入要求` };
  }
  return { content };
}
