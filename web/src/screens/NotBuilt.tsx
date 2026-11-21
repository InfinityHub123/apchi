import { PageHead } from "../ui/States";

/** A route whose screen has not landed yet. Removed once the last one has. */
export function NotBuilt({ title }: { title: string }) {
  return (
    <div className="page">
      <PageHead title={title} sub="This screen is not built yet." />
    </div>
  );
}
