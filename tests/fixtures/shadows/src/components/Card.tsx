// THE real shadow: a local Card, named exactly like the DS one, re-implementing its markup
// instead of importing it.
export function Card({ children }: { children: React.ReactNode }) {
  return <div className="pl-card local-card">{children}</div>;
}
