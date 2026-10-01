import { beginLoading, isLoading } from "./loading.js";

const form = document.querySelector("#auth-form");
const error = document.querySelector("#auth-error");
const submit = document.querySelector("#auth-submit");
const restoreForm = document.querySelector("#setup-restore-form");
const restoreOption = document.querySelector("#setup-restore-option");
const restoreError = document.querySelector("#setup-restore-error");
const restoreSubmit = document.querySelector("#setup-restore-submit");
let setup = false;
function showRestore(open) {
  if (!setup || isLoading(form) || isLoading(restoreForm)) return;
  const from = open ? form : restoreForm;
  const to = open ? restoreForm : form;
  to.elements.setup_token.value = from.elements.setup_token.value;
  form.hidden = open;
  restoreOption.hidden = open;
  restoreForm.hidden = !open;
  document.querySelector("#auth-title").textContent = open
    ? "Restore your data"
    : "Create your account";
  document.querySelector("#auth-description").textContent = open
    ? "Recover your AppTrail workspace from a backup after an update or move."
    : "Set up the owner account for this AppTrail workspace.";
  document.title = (open ? "Restore your data" : "Create your account") + " · AppTrail";
  if (open) restoreForm.elements.setup_token.focus();
  else document.querySelector("#show-restore").focus();
}
document.querySelector("#show-restore").addEventListener("click", () => showRestore(true));
document.querySelector("#cancel-restore").addEventListener("click", () => showRestore(false));
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
    restoreOption.hidden = !setup;
    restoreForm.hidden = true;
    document.querySelector("#auth-title").textContent = setup
      ? "Create your account"
      : "Sign in to AppTrail";
    document.querySelector("#auth-description").textContent = setup
      ? "Set up the owner account for this AppTrail workspace."
      : new URLSearchParams(location.search).get("restored") === "1"
        ? "Backup restored. Sign in with the username and password saved in that backup."
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
    restoreOption.hidden = true;
    restoreForm.hidden = true;
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
restoreForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!setup || isLoading(restoreForm) || restoreSubmit.disabled) return;
  restoreError.textContent = "";
  const file = restoreForm.elements.backup.files[0];
  if (!file || !file.size || file.size > 2 * 1024 * 1024 * 1024) {
    restoreError.textContent = "Choose a SQLite backup file of up to 2 GB.";
    return;
  }
  if (!restoreForm.elements.confirm.checked) {
    restoreError.textContent = "Confirm that the backup will overwrite current server data.";
    return;
  }
  const setupToken = restoreForm.elements.setup_token.value.trim();
  const finishLoading = beginLoading(restoreSubmit, restoreForm);
  const controls = [...restoreForm.querySelectorAll("input, button")].filter((el) => !el.disabled);
  controls.forEach((el) => { el.disabled = true; });
  const status = document.querySelector("#setup-restore-status");
  status.textContent = "Uploading, validating and restoring your backup. Keep this page open.";
  restoreSubmit.textContent = "Restoring…";
  try {
    const response = await fetch("/api/auth/restore", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/vnd.sqlite3",
        "X-AppTrail-Request": "1",
        "X-AppTrail-Setup-Token": setupToken,
        "X-AppTrail-Confirm-Restore": "overwrite",
      },
      body: file,
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 409) {
        await load();
        error.textContent = result.detail;
        return;
      }
      throw new Error(result.detail || "Unable to restore the backup. Try again.");
    }
    localStorage.removeItem("apptrail-app");
    location.replace("/login?restored=1");
  } catch (e) {
    restoreError.textContent = e.message;
  } finally {
    status.textContent = "";
    controls.forEach((el) => { el.disabled = false; });
    finishLoading();
    restoreSubmit.textContent = "Restore backup";
  }
});
await load();
