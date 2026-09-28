let pending = 0;
let showTimer;
const indicator = document.createElement("div");
indicator.className = "network-loading";
indicator.hidden = true;
indicator.setAttribute("role", "status");
indicator.setAttribute("aria-live", "polite");
indicator.innerHTML = '<span class="loading-spinner" aria-hidden="true"></span>Loading…';

export function placeLoadingIndicator() {
  const parent = document.querySelector("dialog[open]") || document.body;
  if (indicator.parentElement !== parent) parent.append(indicator);
}

export function isLoading(element) {
  return element?.getAttribute("aria-busy") === "true";
}

export function beginLoading(...elements) {
  const controls = elements.filter(Boolean).map((element) => {
    const previous = {
      element,
      busy: element.getAttribute("aria-busy"),
      disabled: element.disabled,
    };
    element.setAttribute("aria-busy", "true");
    if (element.matches("button")) {
      element.disabled = true;
      element.classList.add("is-loading");
    }
    return previous;
  });
  placeLoadingIndicator();
  if (pending++ === 0)
    showTimer = setTimeout(() => {
      indicator.hidden = false;
    }, 150);
  let finished = false;
  return () => {
    if (finished) return;
    finished = true;
    for (const { element, busy, disabled } of controls) {
      if (busy === null) element.removeAttribute("aria-busy");
      else element.setAttribute("aria-busy", busy);
      if (element.matches("button")) {
        element.disabled = disabled;
        element.classList.remove("is-loading");
      }
    }
    if (--pending === 0) {
      clearTimeout(showTimer);
      indicator.hidden = true;
    }
    placeLoadingIndicator();
  };
}
