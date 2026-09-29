import { Grid } from "@protolabsai/ui/layout";

export function MetricGrid({ items }: { items: string[] }) {
  return <Grid cols={3}>{items.map((i) => <div key={i}>{i}</div>)}</Grid>;
}

// Named like a Grid, but it's a definition list — no DS import, no pl-grid markup. Not a fork.
export function KeyValueGrid({ rows }: { rows: [string, string][] }) {
  return (
    <dl className="kv">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}
