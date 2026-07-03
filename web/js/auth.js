// Firebase email/password auth via the Identity Toolkit REST API — same
// scheme as cloudsync.py on the laptop. The refresh token lives in
// localStorage; ID tokens are minted from it on demand.

import { FIREBASE } from "../config.js";

const KEY = "dictmic-auth";
const SIGNIN = `https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=${FIREBASE.apiKey}`;
const SIGNUP = `https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=${FIREBASE.apiKey}`;
const REFRESH = `https://securetoken.googleapis.com/v1/token?key=${FIREBASE.apiKey}`;
const OOB = `https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key=${FIREBASE.apiKey}`;

let session = null;   // {refreshToken, idToken, exp, uid, email}

function load() {
  if (session) return session;
  try { session = JSON.parse(localStorage.getItem(KEY)) || null; }
  catch { session = null; }
  return session;
}

function save(s) {
  session = s;
  localStorage.setItem(KEY, JSON.stringify(s));
}

export function signedIn() { return !!load()?.refreshToken; }
export function uid() { return load()?.uid || ""; }
export function email() { return load()?.email || ""; }

export function signOut() {
  session = null;
  localStorage.removeItem(KEY);
}

export async function signIn(email, password) {
  let res = await fetch(SIGNIN, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, returnSecureToken: true }),
  });
  if (!res.ok) {
    const err = (await res.json()).error?.message || "";
    // first ever sign-in creates the account (same as the laptop dialog)
    if (err.startsWith("EMAIL_NOT_FOUND") || err.startsWith("INVALID_LOGIN_CREDENTIALS")) {
      res = await fetch(SIGNUP, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, returnSecureToken: true }),
      });
      if (!res.ok) {
        const err2 = (await res.json()).error?.message || err;
        throw new Error(friendly(err2.startsWith("EMAIL_EXISTS")
          ? "WRONG_PASSWORD" : err2));
      }
    } else {
      throw new Error(friendly(err));
    }
  }
  const data = await res.json();
  save({
    refreshToken: data.refreshToken, idToken: data.idToken,
    exp: Date.now() + (Number(data.expiresIn || 3600) - 300) * 1000,
    uid: data.localId, email,
  });
  return session;
}

// Firebase emails the reset link itself — nothing to store or serve.
export async function sendPasswordReset(addr) {
  const res = await fetch(OOB, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ requestType: "PASSWORD_RESET", email: addr }),
  });
  if (!res.ok) {
    const err = (await res.json()).error?.message || "";
    if (err.startsWith("EMAIL_NOT_FOUND"))
      throw new Error("No account with that email yet — signing in creates one");
    if (err.includes("INVALID_EMAIL"))
      throw new Error("That email doesn't look right");
    if (err.includes("TOO_MANY_ATTEMPTS"))
      throw new Error("Too many tries — wait a minute");
    throw new Error("Couldn't send the reset email");
  }
}

function friendly(err) {
  if (err.includes("WEAK_PASSWORD")) return "Password needs at least 6 characters";
  if (err.includes("INVALID_EMAIL")) return "That email doesn't look right";
  if (err.includes("WRONG_PASSWORD")) return "Wrong password for that account";
  if (err.includes("TOO_MANY_ATTEMPTS")) return "Too many tries — wait a minute";
  return "Sign-in failed" + (err ? ` (${err})` : "");
}

export async function idToken() {
  const s = load();
  if (!s?.refreshToken) throw new Error("signed-out");
  if (s.idToken && Date.now() < s.exp) return s.idToken;
  const res = await fetch(REFRESH, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token", refresh_token: s.refreshToken,
    }),
  });
  if (!res.ok) { signOut(); throw new Error("signed-out"); }
  const data = await res.json();
  save({
    ...s,
    refreshToken: data.refresh_token || s.refreshToken,
    idToken: data.id_token,
    exp: Date.now() + (Number(data.expires_in || 3600) - 300) * 1000,
    uid: data.user_id || s.uid,
  });
  return session.idToken;
}
