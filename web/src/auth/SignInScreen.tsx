import { useState } from "react";
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

  return (
    <div className="signin">
      <div className="signin__card">
        <div className="signin__head">
          <span className="topbar__mark" aria-hidden="true">
            A
          </span>
          <span className="topbar__name">Apchi</span>
        </div>

        {signIn.isPending && <p className="note">Asking Apchi how to sign in…</p>}

        {signIn.isError && (
          <p className="note note--red" role="alert">
            Apchi could not be reached. {(signIn.error as Error).message}
          </p>
        )}

        {signIn.data?.mode === "password" && <PasswordForm />}

        {signIn.data?.mode === "sso" && (
          <>
            <p className="signin__lede">
              Apchi signs in through your organisation's identity provider.
            </p>
            {/* A real link, not a fetch: the provider answers with a redirect and a
                cross-origin redirect is the browser's job, not the client's. */}
            <a className="btn btn--primary btn--block" href="/api/v1/auth/sso/login">
              Continue with single sign-on
            </a>
          </>
        )}

        {signIn.data?.mode === "none" && (
          <p className="note">
            Apchi is running without authentication, so there is nothing to sign in to. Every
            caller is treated as an Admin.
          </p>
        )}
      </div>
    </div>
  );
}

function PasswordForm() {
  const login = useLogin();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  return (
    <form
      className="signin__form"
      onSubmit={(e) => {
        e.preventDefault();
        login.mutate({ username, password });
      }}
    >
      <label className="field">
        <span className="field__label">Username</span>
        <input
          className="field__input"
          name="username"
          autoComplete="username"
          required
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
      </label>

      <label className="field">
        <span className="field__label">Password</span>
        <input
          className="field__input"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>

      {login.isError && (
        <p className="note note--red" role="alert">
          {(login.error as Error).message}
        </p>
      )}

      <button type="submit" className="btn btn--primary btn--block" disabled={login.isPending}>
        {login.isPending ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
