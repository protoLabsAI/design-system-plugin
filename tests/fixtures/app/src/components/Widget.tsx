import { Box } from "@mui/material";
import { Button as DSButton } from "@protolabsai/ui/primitives";

// A comment mentioning #1234 and <button> is not code.
export function StatusDot({ ok }: { ok: boolean }) {
  return <span style={{ color: ok ? "#9b87f2" : "var(--pl-color-fg)", fontSize: 12, padding: 8 }} />;
}

export function Toolbar() {
  return (
    <div>
      <button onClick={() => {}}>raw</button>
      <input type="hidden" name="x" />
      <DSButton>ok</DSButton>
      <a href="#abc">anchor, not a color</a>
      <Box />
    </div>
  );
}
