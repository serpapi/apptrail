import { beginLoading, isLoading } from "./loading.js";

const form = document.querySelector("#auth-form");
const error = document.querySelector("#auth-error");
const submit = document.querySelector("#auth-submit");
let setup = false;
async function load() {
  try {
    const response = await fetch("/api/auth/status", {
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!response.ok)
      throw new Error(
        "Unable to open AppTrail. Check the server address and try again.",
      );
    const state = await response.json();
    if (state.authenticated) {
      location.replace("/");
      return;
    }
    setup = state.setup_required;
    document.querySelector("#auth-title").textContent = setup
      ? "Create your account"
      : "Sign in to AppTrail";
    document.querySelector("#auth-description").textContent = setup
      ? "Set up the owner account for this AppTrail workspace."
      : "Access your apps and search history.";
    document.title =
      (setup ? "Create your account" : "Sign in") + " · AppTrail";
    for (const id of ["setup-fields", "confirm-field", "password-help"])
      document.getElementById(id).hidden = !setup;
    document.querySelector("#auth-recovery").hidden = setup;
    form.elements.setup_token.required = setup;
    form.elements.confirm_password.required = setup;
    form.elements.username.pattern = setup
      ? "[A-Za-z0-9][A-Za-z0-9_.-]{2,63}"
      : ".+";
    form.elements.username.title = setup
      ? "3–64 letters, numbers, dots, underscores or hyphens."
      : "";
    form.elements.password.autocomplete = setup
      ? "new-password"
      : "current-password";
    form.elements.password.minLength = setup ? 8 : 1;
    if (setup) {
      form.elements.password.pattern = "(?=.*[0-9])(?=.*[\\p{P}\\p{S}]).*";
      form.elements.password.title =
        "Use 8 to 128 characters, including a number and a special character.";
    } else {
      form.elements.password.removeAttribute("pattern");
      form.elements.password.removeAttribute("title");
    }
    submit.textContent = setup ? "Create account" : "Sign in";
    form.hidden = false;
  } catch (e) {
    error.textContent = e.message;
    form.hidden = false;
    submit.disabled = true;
  }
  document.querySelector("#auth-loading").hidden = true;
}
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (isLoading(form) || submit.disabled) return;
  error.textContent = "";
  const data = new FormData(form);
  if (setup && data.get("password") !== data.get("confirm_password")) {
    error.textContent = "Passwords do not match.";
    return;
  }
  const finishLoading = beginLoading(submit, form);
  submit.textContent = setup ? "Creating account…" : "Signing in…";
  try {
    const body = {
      username: data.get("username"),
      password: data.get("password"),
    };
    if (setup) body.setup_token = data.get("setup_token").trim();
    const response = await fetch("/api/auth/" + (setup ? "setup" : "login"), {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-AppTrail-Request": "1",
      },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 409) await load();
      throw new Error(result.detail || "Unable to sign in. Try again.");
    }
    form.reset();
    location.replace("/");
  } catch (e) {
    error.textContent = e.message;
  } finally {
    finishLoading();
    submit.textContent = setup ? "Create account" : "Sign in";
  }
});
await load();
