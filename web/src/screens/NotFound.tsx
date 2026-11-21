import { Link } from "react-router-dom";
import { PageHead } from "../ui/States";

/** A path no screen lives at. */
export function NotFound() {
  return (
    <div className="page">
      <PageHead title="Not found" sub={<>There is no screen here. <Link to="/">Back to the Overview</Link>.</>} />
    </div>
  );
}
