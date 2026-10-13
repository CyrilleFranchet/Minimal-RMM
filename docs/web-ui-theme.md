# Web UI — light / dark theme

The operator console at `/ui/` supports **dark** (default) and **light** themes via CSS custom properties on `html[data-theme]`.

## Toggle

- **Login screen** — sun / moon toggle in the card header (sun while dark → switch to light).
- **Connected** — same control in the app header (before **AI**).

Click toggles between themes (☀ in dark mode, ☽ in light mode). Preference is mirrored in **`localStorage`** key `rmm_theme` (`dark` | `light`) and persisted in the authenticated server or workspace SQLite state. Default when unset: **`dark`**.

## Implementation

| File | Role |
|------|------|
| `web/style.css` | `:root` / `[data-theme="dark"]` and `[data-theme="light"]` variable palettes |
| `web/theme.js` | Apply theme, sync toggle buttons, persist preference |
| `web/index.html` | Inline head script applies saved theme before paint (avoids flash) |

Semantic tokens (`--bg`, `--surface`, `--text`, `--overlay`, status backgrounds, etc.) keep components theme-aware without per-component overrides.

## Notes

- The browser mirror is read before the page renders to avoid a flash of the
  wrong theme. After authentication, SQLite is authoritative and updates the
  mirror; the API token remains a separate login credential.
- Native form controls follow `color-scheme` on `html` for scrollbars and inputs where supported.
