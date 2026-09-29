import { Chip } from "../ds";

// Renders the DS Chip through the app's own barrel — composing, not shadowing.
export function CodeRefChip({ path }: { path: string }) {
  return <Chip>{path}</Chip>;
}

export function ScheduledChip({ at }: { at: string }) {
  return <Chip tone="info">{at}</Chip>;
}
