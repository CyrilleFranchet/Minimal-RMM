/** Light/dark theme — default dark; preference in localStorage (rmm_theme). */
(function () {
  const STORAGE_KEY = "rmm_theme";
  const DEFAULT_THEME = "dark";

  function normalizeTheme(value) {
    return value === "light" ? "light" : "dark";
  }

  function currentTheme() {
    return normalizeTheme(document.documentElement.getAttribute("data-theme"));
  }

  const ICON_SUN = "\u2600";
  const ICON_MOON = "\u263E";

  function updateToggleButtons(theme) {
    const toLight = theme === "dark";
    const label = toLight ? "Switch to light mode" : "Switch to dark mode";
    const icon = toLight ? ICON_SUN : ICON_MOON;
    document.querySelectorAll(".theme-toggle-btn").forEach((btn) => {
      btn.textContent = icon;
      btn.setAttribute("aria-label", label);
      btn.title = label;
    });
  }

  function applyTheme(theme) {
    const t = normalizeTheme(theme);
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem(STORAGE_KEY, t);
    updateToggleButtons(t);
  }

  function initThemeToggle() {
    applyTheme(localStorage.getItem(STORAGE_KEY) || DEFAULT_THEME);
    document.querySelectorAll(".theme-toggle-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        applyTheme(currentTheme() === "dark" ? "light" : "dark");
      });
    });
  }

  document.addEventListener("DOMContentLoaded", initThemeToggle);
})();
