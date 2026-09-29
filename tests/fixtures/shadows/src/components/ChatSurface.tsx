import { Surface } from "@protolabsai/ui/layout";

// An app feature that COMPOSES the DS Surface — not a fork of it.
export function ChatSurface({ children }: { children: React.ReactNode }) {
  return <Surface padding="none">{children}</Surface>;
}

export function CodeSurface({ code }: { code: string }) {
  return (
    <Surface>
      <pre>{code}</pre>
    </Surface>
  );
}
