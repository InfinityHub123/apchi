import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { useIdentity } from "./auth/useAuth";
import { SignInScreen } from "./auth/SignInScreen";
import { Shell } from "./shell/Shell";
import { routes } from "./routes";

const router = createBrowserRouter([{ path: "/", element: <Shell />, children: routes }]);

/**
 * The console is not rendered at all for a caller Apchi does not know. Hiding controls while
 * still fetching would show an Operator a shell full of 403s and call it an interface.
 */
export default function App() {
  const identity = useIdentity();

  if (identity.isPending) {
    return (
      <div className="signin">
        <p className="note note--flush">Asking Apchi who you are…</p>
      </div>
    );
  }
  if (identity.isError) return <SignInScreen />;
  return <RouterProvider router={router} />;
}
