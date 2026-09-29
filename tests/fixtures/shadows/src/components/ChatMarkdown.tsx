import { Markdown } from "@protolabsai/ui/markdown";

export function ChatMarkdown({ text }: { text: string }) {
  return <Markdown source={text} />;
}
