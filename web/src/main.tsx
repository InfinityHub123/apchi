import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "./tokens.css";
import "./components.css";

// A failed read is shown, not retried into silence: a screen that renders empty while
// reporting success is the failure this codebase refuses everywhere else too.
const queries = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 5_000 } },
});

const root = document.getElementById("root");
if (root === null) throw new Error("index.html has no #root");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queries}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
