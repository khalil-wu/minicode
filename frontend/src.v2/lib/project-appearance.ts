import { BarChart3, Book, Brain, BriefcaseBusiness, DollarSign, Dumbbell, Earth, Flower2, FlaskConical, Folder, FunctionSquare,
  Globe, GraduationCap, Heart, HeartPulse, Logs, Music, Palette, PawPrint, Pencil, PencilLine, Plane, Popcorn, Scale,
  Settings2, Sprout, Stethoscope, Terminal, Weight, Wrench } from "./icons";

// Current Codex desktop LIa/HIa/aka/nDa project marker catalogue.
export const PROJECT_ICONS = { "bar-chart": BarChart3, book: Book, brain: Brain, "currency-dollar": DollarSign, customize: Settings2,
  "desk-globe": Earth, dumbbell: Dumbbell, edit: Pencil, flask: FlaskConical, folder: Folder, function: FunctionSquare, globe: Globe,
  "graduation-cap": GraduationCap, health: HeartPulse, heart: Heart, kettlebell: Weight, logs: Logs, lotus: Flower2, music: Music,
  palette: Palette, paw: PawPrint, plane: Plane, plant: Sprout, popcorn: Popcorn, scale: Scale, stethoscope: Stethoscope,
  suitcase: BriefcaseBusiness, terminal: Terminal, wrench: Wrench, writing: PencilLine } as const;
export const PROJECT_COLORS = { black: ["currentColor", "currentColor"], blue: ["#0285ff", "#339cff"], green: ["#04b84c", "#40c977"],
  orange: ["#fb6a22", "#ff8549"], pink: ["#ff66ad", "#ff8cc1"], purple: ["#924ff7", "#ad7bf9"], red: ["#fa423e", "#ff6764"], yellow: ["#ffc300", "#ffd240"] } as const;
export type ProjectAppearance = { color: keyof typeof PROJECT_COLORS; marker: { kind: "icon"; icon: keyof typeof PROJECT_ICONS } | { kind: "emoji"; emoji: string } };
export const DEFAULT_PROJECT_APPEARANCE: ProjectAppearance = { color: "black", marker: { kind: "icon", icon: "folder" } };
