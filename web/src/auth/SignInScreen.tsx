import { useState } from "react";
import { Logo } from "../ui/Icon";
import { useLogin, useSignIn } from "./useAuth";

/**
 * The screen a caller with no credential sees.
 *
 * What it offers comes from `/auth/sign-in` rather than from probing: `/auth/me` answers 401
 * in both `password` and `sso`, so without that endpoint this would have to branch on the
 * prose of a refusal.
 *
 * Apchi answers one message for every way a password login can fail -- unknown username,
 * wrong password, disabled account -- because telling them apart is how somebody enumerates
 * the platform staff who have accounts. So this screen shows what the API said and never
 * tries to be more specific.
 */
export function SignInScreen() {
  const signIn = useSignIn(true);
  const mode = signIn.data?.mode;

  return (
    <div className="signin">
      <section className="card signin__card">
        <div className="signin__body">
          <span style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <Logo size={20} />
            <span style={{ fontSize: 18, fontWeight: 600 }}>apchi</span>
          </span>
          <div style={{ fontSize: 15, fontWeight: 600 }}>Sign in</div>

          {signIn.isPending && <p className="note note--flush">Asking Apchi how to sign in…</p>}

          {signIn.isError && (
            <p className="note note--flush note--red" role="alert">
              Apchi could not be reached. {(signIn.error as Error).message}
            </p>
          )}

          {mode === "password" && <PasswordForm />}

          {mode === "sso" && (
            <>
              {/* A real link, not a fetch: the provider answers with a redirect, and a
                  cross-origin redirect is the browser's job, not the client's. */}
              <a className="btn pri" style={{ justifyContent: "center", height: 34 }} href="/api/v1/auth/sso/login">
                Continue with your organisation's sign-in
              </a>
              <span className="sm muted">
                You will come back here once your identity provider has confirmed who you are.
                Your role comes from it, not from Apchi.
              </span>
            </>
          )}

          {mode === "none" && (
            <p className="note note--flush">
              Apchi is running without authentication, so there is nothing to sign in to.
              Every caller is treated as an Admin.
            </p>
          )}
        </div>
        {mode !== undefined && (
          <div className="cft">
            <span className="sm muted">
              {mode === "password"
                ? "Password mode · the session is a cookie this page cannot read"
                : mode === "sso"
                  ? "SSO mode"
                  : "No sign-in"}
            </span>
          </div>
        )}
      </section>
    </div>
  );
}

function PasswordForm() {
  const login = useLogin();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  return (
    <form
      style={{ display: "flex", flexDirection: "column", gap: 16 }}
      onSubmit={(e) => {
        e.preventDefault();
        login.mutate({ username, password });
      }}
    >
      <label className="fl">
        Username
        <input
          className="inp"
          name="username"
          autoComplete="username"
          required
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
      </label>
      <label className="fl">
        Password
        <input
          className="inp"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <button
        type="submit"
        className="btn pri"
        style={{ justifyContent: "center", height: 34 }}
        disabled={login.isPending}
      >
        {login.isPending ? "Signing in…" : "Sign in"}
      </button>
      {login.isError && (
        <div className="note note--flush note--red" role="alert">
          {(login.error as Error).message}
        </div>
      )}
    </form>
  );
}
