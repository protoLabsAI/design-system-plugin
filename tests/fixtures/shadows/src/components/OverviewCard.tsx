import { Card, Stat } from "@protolabsai/ui/primitives";

export function OverviewCard({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <Stat label={label} value={value} />
    </Card>
  );
}

export const NodeRuntimeCard = ({ node }: { node: string }) => <Card className="node-runtime">{node}</Card>;
