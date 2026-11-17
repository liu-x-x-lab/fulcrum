# Fulcrum UI guidelines

This is the visual and interaction contract for tenant and platform administration,
authentication and public pages. Use it with [the shared development contract](../../AGETNS-DEV.md).
The current design is a compact Soft UI interface: light gray surroundings, white
surfaces, indigo actions, rounded corners and restrained shadows.

**Avoid unnecessary secondary labels and descriptions. Let the layout do the explaining.**

Use grouping, alignment, spacing and hierarchy to communicate relationships.
Remove redundant subtitles, badges, English kickers and helper text. Keep essential
field labels, status/error feedback and warnings for consequential actions.

## Shared implementation

| Need | Existing component |
| --- | --- |
| Tenant / platform page | [base.html](../../app/templates/base.html) / [platform_base.html](../../app/templates/platform_base.html) |
| Authentication | [auth_base.html](../../app/templates/auth_base.html), [_auth_fields.html](../../app/templates/_auth_fields.html) |
| Plugin settings | [_plugin_config_form.html](../../app/templates/_plugin_config_form.html) |
| Group configuration | [_config_card.html](../../app/templates/_config_card.html), [_chatroom_plugin_card.html](../../app/templates/_chatroom_plugin_card.html) |
| Toasts and dialogs | [toast.js](../../app/static/js/toast.js) (`appToast`), [dialog.js](../../app/static/js/dialog.js) (`appDialog`, `data-confirm-*`) |
| Switches, selects and pending actions | [switches.js](../../app/static/js/switches.js), [select.js](../../app/static/js/select.js), [action-feedback.js](../../app/static/js/action-feedback.js) |
| Tokens and component styles | [style.css](../../app/static/css/style.css), [fonts.css](../../app/static/css/fonts.css) |

Use existing `btn`, `ui-switch`, configuration, table, status and modal classes.
Add necessary styles beside their component rules, scoped to that component.
Do not append permanent override layers, reintroduce old style aliases, add a
second navigation system or install another UI/chart framework. Browser-native
`alert`, `confirm` and `prompt` are replaced by the shared dialog controls.
A settings-only plugin uses the generated form rather than a separate page.

## Visual values

Reference CSS variables instead of copying literal values into new styles.

| Role | Current tokens / values |
| --- | --- |
| Page / main surface | `--bg` `#f5f6fa` / `--surface` `#ffffff` |
| Inputs / secondary surfaces | `--surface-soft` `#f8fafc` / `--surface-muted` `#f1f5f9` |
| Heading / body / supporting text | `--text` `#1f2937` / `--text-soft` `#475569` / `--muted` `#64748b` |
| Brand / primary action / selection | `--primary` `#6366f1` / `--primary-strong` `#5154c9` / `--primary-soft` `#eeefff` |
| Borders | `--border` `#e8ecf3` / `--border-strong` `#cdd5e4` |
| Status | `--success` `#18745e`, `--info` `#496ba8`, `--warning` `#a66416`, `--danger` `#bd4260`, with their soft backgrounds |
| Surface / control / compact radii | `--radius-lg` 20px / `--radius-md` 16px / `--radius-sm` 10px |
| Elevation / focus | `--shadow-card`, `--shadow-soft`, `--focus` |
| Admin content width | `--content-width` 1600px; desktop side padding 24px |

Use the self-hosted Albert Sans / Noto Sans SC fonts. Chinese body weight is 400;
headings use 600–650. Prefer tabular numerals for comparable numbers. Do not fetch
remote fonts or shrink body text to create density.

| Text | Desktop | Mobile |
| --- | --- | --- |
| Admin page title | 26px | 24px |
| Section title | 18px | 17–18px |
| Key metric | 30px | 26px |
| Body | 14px, line-height 1.6 | 14–15px, line-height 1.6 |
| Field/table label | 13px | 13–14px |
| Supporting information | 12px | 12px |
| Login title | 30px | 26px |
| Marketing title | 64px | 44–48px |

Use the stronger primary color for white-text actions. Status and chart legends
need text as well as color. Target at least 4.5:1 contrast for ordinary text.
Keep data surfaces still; interactive controls may rise at most 2px on hover,
with active/focus/disabled states and no layout shift. Respect reduced motion.

## Layout and content

- Prefer 16–20px section gaps and 18–20px surface padding. Form fields use
  14–16px gaps and 6px between label and input. Public sections may use about
  64px spacing; public content width is 1200px and the auth shell about 1040px.
- Keep the 64px top bar, one page `h1`, nearby primary action and about 20px
  before the content. Secondary navigation wraps naturally without horizontal
  scrolling. Platform and tenant navigation retain their distinct responsibilities.
- Prefer compact record lists/tables for business data. Put filters near results,
  wrapping on desktop and collapsing on mobile while showing active filters.
  Use add/edit dialogs for users, tenants and plans rather than large inline forms.
- Plugin catalogs use three columns on desktop, two on tablet and one on mobile.
  Show the full useful description, version and configuration entry; retain
  actionable warnings. Long configuration fields take a row; short values fit
  their content. Each label and field share one grid cell.
- Use a single raised surface per section. Grid items must shrink and stretch
  naturally; avoid nested raised cards, fixed-height clipping and oversized empty
  states. Typical chart height is about 210px. Preserve exact readings in an
  accessible detail view when simplifying a chart.
- Tables, logs and code may scroll within their own containers. Preserve QR and
  payment-code aspect ratios, white backgrounds and content. Decoration must
  not interfere with scanning, input or primary actions.

## Forms, dialogs and permissions

Preserve field names, form actions, validation, autofill and CSRF. Placeholders
are examples, not labels. Escape untrusted values or use `textContent` in browser
code. Server checks enforce tenant membership, role, plugin availability and
resource ownership; hidden controls are not authorization.

Show pending, success and failure states. Prevent double submission, preserve
input on failure, and use shared autosave/recovery behavior. Report a saved state
only after server confirmation. Show default versus group-specific configuration
and use the shared reset action. Secret fields show configured/not-configured
state without exposing the stored value.

Dialogs have a reachable close control, bounded height, internal scrolling,
Escape handling and focus restoration to a visible trigger. When opened from a
collapsed record menu, restore focus to its summary instead of a hidden button.
Custom selects must not resize or scroll other fields when opened. Preserve
accessible names and keyboard operation for icon-only controls.

## Authentication and responsive behavior

- Tenant login, platform login and registration share the visual shell but keep
  distinct account systems and request paths. Preserve all current required
  fields, CAPTCHA, password visibility and validation behavior.
- Authentication errors stay beside the form. Use one form column, a compact
  brand area and a home link; do not invent customer claims or operational metrics.
- Under 820px, collapse primary navigation and stack paired surfaces. On phones,
  reduce branding so login controls remain reachable in the first viewport;
  registration may scroll naturally.
- Under 560px, short metrics can remain in two columns; long labels/content
  metrics may use one. Touch targets must reach 44px. Desktop buttons are usually
  40px and compact filters 36px.
- Keep the skip link, one `h1`, visible labels/focus, meaningful image alternatives
  and announced feedback. Reduced-motion users receive a stable interface.

## Acceptance

Check structure, visual consistency, density, states and responsive behavior for
the changed surface. Use 1440×900, 1024×768 and 390×844 when layout changes. The
root must satisfy `scrollWidth <= clientWidth + 1`; wide content scrolls locally.
Exercise the relevant normal, empty, error, permission, long-content and dialog
states, including actual keyboard/click paths when interactions change.

Select lightweight checks using [the testing guide](../../tests/README.md).
Distinguish source/template checks, browser interaction and production/device
acceptance. Keep execution records, screenshots and raw output in ignored local
directories. Publish only concise results and deliberately selected sanitized
product illustrations; do not maintain a historical page-by-page progress log here.
