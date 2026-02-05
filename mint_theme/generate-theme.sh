#!/bin/bash
# ============================================================
# Odoo 19 Theme Generator
# ============================================================
# Generate mint_theme.scss with custom colors
#
# Usage:
#   ./generate-theme.sh                    # Use default green theme
#   ./generate-theme.sh --preset blue      # Use blue preset
#   ./generate-theme.sh --primary "#FF5722" --name "orange"
#
# Presets: green (default), blue, orange, purple, charcoal
#
# Environment Variables (alternative to flags):
#   ODOO_BRAND_PRIMARY   - Primary brand color
#   ODOO_BRAND_HOVER     - Hover state color
#   ODOO_BRAND_ACTIVE    - Active/pressed state color
#   ODOO_BRAND_ACCENT    - Accent/highlight color
#   ODOO_BRAND_BG        - Light background color
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_FILE="$SCRIPT_DIR/static/src/scss/mint_theme.scss"

# ============================================================
# COLOR PRESETS
# ============================================================

preset_green() {
  THEME_NAME="Mint Green"
  THEME_PRIMARY="#15803D"
  THEME_HOVER="#166534"
  THEME_ACTIVE="#14532D"
  THEME_ACCENT="#22C55E"
  THEME_BG="#DCFCE7"
}

preset_blue() {
  THEME_NAME="Ocean Blue"
  THEME_PRIMARY="#1D4ED8"
  THEME_HOVER="#1E40AF"
  THEME_ACTIVE="#1E3A8A"
  THEME_ACCENT="#3B82F6"
  THEME_BG="#DBEAFE"
}

preset_orange() {
  THEME_NAME="Coral Orange"
  THEME_PRIMARY="#EA580C"
  THEME_HOVER="#C2410C"
  THEME_ACTIVE="#9A3412"
  THEME_ACCENT="#FB923C"
  THEME_BG="#FFEDD5"
}

preset_purple() {
  THEME_NAME="Berry Purple"
  THEME_PRIMARY="#7C3AED"
  THEME_HOVER="#6D28D9"
  THEME_ACTIVE="#5B21B6"
  THEME_ACCENT="#A78BFA"
  THEME_BG="#EDE9FE"
}

preset_charcoal() {
  THEME_NAME="Charcoal"
  THEME_PRIMARY="#374151"
  THEME_HOVER="#1F2937"
  THEME_ACTIVE="#111827"
  THEME_ACCENT="#6B7280"
  THEME_BG="#F3F4F6"
}

preset_teal() {
  THEME_NAME="Teal"
  THEME_PRIMARY="#0D9488"
  THEME_HOVER="#0F766E"
  THEME_ACTIVE="#115E59"
  THEME_ACCENT="#2DD4BF"
  THEME_BG="#CCFBF1"
}

preset_rose() {
  THEME_NAME="Rose"
  THEME_PRIMARY="#E11D48"
  THEME_HOVER="#BE123C"
  THEME_ACTIVE="#9F1239"
  THEME_ACCENT="#FB7185"
  THEME_BG="#FFE4E6"
}

# ============================================================
# HELPER FUNCTIONS
# ============================================================

hex_to_rgb() {
  local hex="${1#\#}"
  local r=$((16#${hex:0:2}))
  local g=$((16#${hex:2:2}))
  local b=$((16#${hex:4:2}))
  echo "$r, $g, $b"
}

show_help() {
  echo "Odoo 19 Theme Generator"
  echo ""
  echo "Usage: $0 [options]"
  echo ""
  echo "Options:"
  echo "  --preset <name>     Use a color preset (green, blue, orange, purple, charcoal, teal, rose)"
  echo "  --primary <color>   Set primary color (hex, e.g., #15803D)"
  echo "  --hover <color>     Set hover color (hex)"
  echo "  --active <color>    Set active color (hex)"
  echo "  --accent <color>    Set accent color (hex)"
  echo "  --bg <color>        Set background color (hex)"
  echo "  --name <name>       Theme name for comments"
  echo "  --list              List available presets"
  echo "  --help              Show this help message"
  echo ""
  echo "Example:"
  echo "  $0 --preset blue"
  echo "  $0 --primary '#FF5722' --name 'Custom Orange'"
}

list_presets() {
  echo "Available presets:"
  echo ""
  echo "  green    - Mint Green (#15803D) - Default"
  echo "  blue     - Ocean Blue (#1D4ED8)"
  echo "  orange   - Coral Orange (#EA580C)"
  echo "  purple   - Berry Purple (#7C3AED)"
  echo "  charcoal - Charcoal (#374151)"
  echo "  teal     - Teal (#0D9488)"
  echo "  rose     - Rose (#E11D48)"
}

# ============================================================
# PARSE ARGUMENTS
# ============================================================

# Set defaults (green theme)
preset_green

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --preset)
      case "$2" in
        green)   preset_green ;;
        blue)    preset_blue ;;
        orange)  preset_orange ;;
        purple)  preset_purple ;;
        charcoal) preset_charcoal ;;
        teal)    preset_teal ;;
        rose)    preset_rose ;;
        *)
          echo "Error: Unknown preset '$2'"
          list_presets
          exit 1
          ;;
      esac
      shift 2
      ;;
    --primary)
      THEME_PRIMARY="$2"
      shift 2
      ;;
    --hover)
      THEME_HOVER="$2"
      shift 2
      ;;
    --active)
      THEME_ACTIVE="$2"
      shift 2
      ;;
    --accent)
      THEME_ACCENT="$2"
      shift 2
      ;;
    --bg)
      THEME_BG="$2"
      shift 2
      ;;
    --name)
      THEME_NAME="$2"
      shift 2
      ;;
    --list)
      list_presets
      exit 0
      ;;
    --help|-h)
      show_help
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      show_help
      exit 1
      ;;
  esac
done

# Override with environment variables if set
THEME_PRIMARY="${ODOO_BRAND_PRIMARY:-$THEME_PRIMARY}"
THEME_HOVER="${ODOO_BRAND_HOVER:-$THEME_HOVER}"
THEME_ACTIVE="${ODOO_BRAND_ACTIVE:-$THEME_ACTIVE}"
THEME_ACCENT="${ODOO_BRAND_ACCENT:-$THEME_ACCENT}"
THEME_BG="${ODOO_BRAND_BG:-$THEME_BG}"

# Calculate RGB
THEME_PRIMARY_RGB=$(hex_to_rgb "$THEME_PRIMARY")

# ============================================================
# DISPLAY CONFIGURATION
# ============================================================

echo "============================================================"
echo "Odoo 19 Theme Generator"
echo "============================================================"
echo ""
echo "Theme:    $THEME_NAME"
echo "Primary:  $THEME_PRIMARY"
echo "Hover:    $THEME_HOVER"
echo "Active:   $THEME_ACTIVE"
echo "Accent:   $THEME_ACCENT"
echo "BG:       $THEME_BG"
echo "RGB:      $THEME_PRIMARY_RGB"
echo ""

# ============================================================
# GENERATE SCSS FILE
# ============================================================

cat > "$OUTPUT_FILE" << SCSS
/**
 * ============================================================
 * ODOO 19 CUSTOM THEME - ${THEME_NAME}
 * ============================================================
 * Generated by: generate-theme.sh
 * Generated at: $(date -u +"%Y-%m-%d %H:%M:%S UTC")
 *
 * To regenerate with different colors:
 *   ./generate-theme.sh --preset blue
 *   ./generate-theme.sh --primary "#FF5722"
 */

// ============================================================
// THEME CONFIGURATION
// ============================================================

\$theme-primary: ${THEME_PRIMARY};
\$theme-primary-hover: ${THEME_HOVER};
\$theme-primary-active: ${THEME_ACTIVE};
\$theme-accent: ${THEME_ACCENT};
\$theme-bg-light: ${THEME_BG};
\$theme-text-on-primary: white;


// ============================================================
// CSS CUSTOM PROPERTIES
// ============================================================

:root {
  --o-brand-odoo: #{\$theme-primary} !important;
  --o-brand-primary: #{\$theme-primary} !important;
  --o-enterprise-color: #{\$theme-primary} !important;
  --primary: #{\$theme-primary} !important;
  --bs-primary: #{\$theme-primary} !important;
  --bs-primary-rgb: ${THEME_PRIMARY_RGB} !important;
  --o-main-color: #{\$theme-primary} !important;

  --o-navbar-bg: #{\$theme-primary} !important;
  --o-navbar-color: #{\$theme-text-on-primary} !important;
  --o-navbar-brand-color: #{\$theme-text-on-primary} !important;
  --o-navbar-badge-bg: #{\$theme-primary-hover} !important;
  --o-navbar-entry-active-bg: #{\$theme-primary-active} !important;
  --o-navbar-entry-bg-hover: #{\$theme-primary-active} !important;
  --o-navbar-entry-bg-active: #{\$theme-primary-active} !important;

  --NavBar-entry-backgroundColor: #{\$theme-primary} !important;
  --NavBar-entry-backgroundColor--hover: #{\$theme-primary-active} !important;
  --NavBar-entry-backgroundColor--active: #{\$theme-primary-active} !important;
  --NavBar-background: #{\$theme-primary} !important;
  --NavBar-entry-bg: #{\$theme-primary} !important;
  --NavBar-entry-bg--hover: #{\$theme-primary-active} !important;
  --NavBar-entry-bg--active: #{\$theme-primary-active} !important;
  --NavBar-menuSections-bg: #{\$theme-primary} !important;
  --NavBar-menuSections-entry-bg: #{\$theme-primary} !important;
  --NavBar-menuSections-entry-bg--hover: #{\$theme-primary-active} !important;
  --NavBar-menuSections-entry-bg--active: #{\$theme-primary-active} !important;
  --o-menu-sections-link-bg-active: #{\$theme-primary-active} !important;

  --AppMenu-section-bg: #{\$theme-primary} !important;
  --AppMenu-section-bg-hover: #{\$theme-primary-active} !important;
  --o-webclient-color-accent: #{\$theme-primary} !important;

  --o-action-color: #{\$theme-primary} !important;
  --o-action-color-hover: #{\$theme-primary-hover} !important;
  --o-form-lightsecondary: #{\$theme-bg-light} !important;

  --btn-primary-bg: #{\$theme-primary} !important;
  --btn-primary-border-color: #{\$theme-primary} !important;
  --btn-primary-hover-bg: #{\$theme-primary-hover} !important;
  --btn-primary-hover-border-color: #{\$theme-primary-hover} !important;
  --btn-primary-active-bg: #{\$theme-primary-active} !important;
  --btn-primary-active-border-color: #{\$theme-primary-active} !important;

  --o-color-1: #{\$theme-primary} !important;
  --o-color-2: #{\$theme-primary-hover} !important;
  --o-color-3: #{\$theme-primary-active} !important;
  --o-color-4: #{\$theme-primary} !important;
  --o-color-5: #{\$theme-accent} !important;

  --o-cc1-bg: #{\$theme-primary} !important;
  --o-cc2-bg: #{\$theme-primary} !important;
  --o-cc3-bg: #{\$theme-primary} !important;
  --o-cc4-bg: #{\$theme-primary} !important;
  --o-cc5-bg: #{\$theme-primary} !important;
  --o-cc1-btn-primary: #{\$theme-primary} !important;
  --o-cc1-btn-primary-border: #{\$theme-primary} !important;
  --o-cc1-link: #{\$theme-primary} !important;
  --o-cc2-btn-primary: #{\$theme-primary} !important;
  --o-cc2-btn-primary-border: #{\$theme-primary} !important;
  --o-cc2-link: #{\$theme-primary} !important;
  --o-cc3-btn-primary: #{\$theme-primary} !important;
  --o-cc3-btn-primary-border: #{\$theme-primary} !important;
  --o-cc3-link: #{\$theme-primary} !important;
  --o-cc4-btn-primary: #{\$theme-primary} !important;
  --o-cc4-btn-primary-border: #{\$theme-primary} !important;
  --o-cc4-link: #{\$theme-accent} !important;
  --o-cc5-btn-primary: #{\$theme-primary} !important;
  --o-cc5-btn-primary-border: #{\$theme-primary} !important;
  --o-cc5-link: #{\$theme-accent} !important;
}

html, body, .o_web_client, .o_action_manager, .o_main_navbar {
  --o-brand-odoo: #{\$theme-primary} !important;
  --o-brand-primary: #{\$theme-primary} !important;
  --NavBar-entry-backgroundColor: #{\$theme-primary} !important;
  --NavBar-entry-backgroundColor--hover: #{\$theme-primary-active} !important;
  --NavBar-entry-backgroundColor--active: #{\$theme-primary-active} !important;
}


// ============================================================
// NAVBAR
// ============================================================

.o_main_navbar {
  background-color: \$theme-primary !important;

  .o_menu_toggle,
  .o_menu_brand,
  .o_menu_sections,
  .o_menu_systray {
    background-color: transparent !important;
  }

  .o_menu_toggle:hover,
  .o_menu_toggle:focus,
  .o_menu_brand:hover {
    background-color: \$theme-primary-active !important;
  }
}

.o_navbar_apps_menu,
.o_navbar_apps_menu button,
.o_navbar_apps_menu .o-dropdown,
.o_navbar_apps_menu .dropdown-toggle {
  background: transparent !important;

  &:hover, &:focus, &.show, &[aria-expanded="true"] {
    background: \$theme-primary-active !important;
  }
}


// ============================================================
// NAVBAR MENU SECTIONS
// ============================================================

.o_menu_sections {
  background-color: \$theme-primary !important;

  > button,
  > a,
  > .dropdown,
  > .o-dropdown,
  button.o-dropdown,
  button.dropdown-toggle,
  button[data-menu-xmlid],
  button[data-hotkey],
  a.o-dropdown-item,
  a.dropdown-item,
  a.o_nav_entry,
  a[data-menu-xmlid],
  [data-menu-xmlid],
  [data-hotkey],
  [data-section] {
    background: \$theme-primary !important;
    color: \$theme-text-on-primary !important;

    &:hover, &:focus, &.show, &.active, &[aria-expanded="true"] {
      background: \$theme-primary-active !important;
      color: \$theme-text-on-primary !important;
    }

    span {
      background: transparent !important;
      color: \$theme-text-on-primary !important;
    }
  }
}

.o_main_navbar .o_menu_sections *,
.o_web_client .o_main_navbar .o_menu_sections *,
nav.o_main_navbar .o_menu_sections * {
  background: \$theme-primary !important;
  color: \$theme-text-on-primary !important;

  &:hover, &:focus, &.show, &.active, &[aria-expanded="true"] {
    background: \$theme-primary-active !important;
  }
}

html body .o_web_client .o_main_navbar .o_menu_sections button,
html body .o_web_client .o_main_navbar .o_menu_sections a {
  background: \$theme-primary !important;
  color: \$theme-text-on-primary !important;

  &:hover, &:focus, &.show, &[aria-expanded="true"] {
    background: \$theme-primary-active !important;
  }
}


// ============================================================
// SYSTRAY
// ============================================================

.o_main_navbar .o_menu_systray {
  .o-dropdown,
  .dropdown-toggle,
  > div {
    &:hover, &.show, &:focus {
      background-color: \$theme-primary-active !important;
    }
  }

  .o_MessagingMenu,
  .o_ActivityMenu {
    &.o-dropdown.show,
    &:hover {
      background-color: \$theme-primary-active !important;
    }
  }
}


// ============================================================
// DROPDOWNS
// ============================================================

.o_main_navbar .dropdown-menu {
  .dropdown-item:hover,
  .dropdown-item:focus,
  .dropdown-item.active {
    background-color: \$theme-primary !important;
    color: \$theme-text-on-primary !important;
  }
}

.o_menu_sections .dropdown-menu {
  background-color: white !important;

  .dropdown-item {
    color: #333 !important;

    &:hover, &:focus, &.active {
      background-color: \$theme-primary !important;
      color: \$theme-text-on-primary !important;
    }
  }
}


// ============================================================
// BUTTONS
// ============================================================

.o_web_client {
  .btn-primary,
  .btn-primary.disabled,
  .btn-primary:disabled {
    background-color: \$theme-primary !important;
    border-color: \$theme-primary !important;
    color: \$theme-text-on-primary !important;

    &:hover, &:focus {
      background-color: \$theme-primary-hover !important;
      border-color: \$theme-primary-hover !important;
    }

    &:active, &.active {
      background-color: \$theme-primary-active !important;
      border-color: \$theme-primary-active !important;
    }
  }

  .btn-outline-primary {
    color: \$theme-primary !important;
    border-color: \$theme-primary !important;

    &:hover, &:focus {
      background-color: \$theme-primary !important;
      border-color: \$theme-primary !important;
      color: \$theme-text-on-primary !important;
    }
  }

  .btn-link {
    color: \$theme-primary !important;
    &:hover { color: \$theme-primary-hover !important; }
  }
}


// ============================================================
// LINKS
// ============================================================

.o_web_client a:not(.btn):not(.nav-link):not(.dropdown-item) {
  color: \$theme-primary !important;
  &:hover, &:focus { color: \$theme-primary-hover !important; }
}


// ============================================================
// FORM ELEMENTS
// ============================================================

.o_web_client {
  .nav-tabs .nav-link.active,
  .nav-tabs .nav-link:hover {
    border-bottom-color: \$theme-primary !important;
    color: \$theme-primary !important;
  }

  .form-check-input:checked,
  .o_checkbox input:checked + span {
    background-color: \$theme-primary !important;
    border-color: \$theme-primary !important;
  }

  .form-check-input[type="radio"]:checked {
    border-color: \$theme-primary !important;
    background-color: \$theme-primary !important;
  }

  .form-control:focus,
  .form-select:focus {
    border-color: \$theme-primary !important;
    box-shadow: 0 0 0 0.2rem rgba(\$theme-primary, 0.25) !important;
  }

  .form-switch .form-check-input:checked {
    background-color: \$theme-primary !important;
    border-color: \$theme-primary !important;
  }
}


// ============================================================
// LIST & KANBAN
// ============================================================

.o_web_client {
  .o_kanban_record:hover { border-color: \$theme-primary !important; }

  .o_list_view .o_data_row.o_data_row_selected,
  .o_list_view .o_data_row:focus-within {
    background-color: \$theme-bg-light !important;
  }

  .o_kanban_header .o_kanban_title:hover { color: \$theme-primary !important; }
}


// ============================================================
// STATUSBAR
// ============================================================

.o_web_client .o_statusbar_status {
  button.btn-primary,
  button.o_arrow_button.btn-primary {
    background-color: \$theme-primary !important;
    border-color: \$theme-primary !important;
    &:hover { background-color: \$theme-primary-hover !important; }
  }

  .o_arrow_button.o_arrow_button_current {
    background-color: \$theme-primary !important;
    &::after { border-left-color: \$theme-primary !important; }
  }
}


// ============================================================
// BADGES & PROGRESS
// ============================================================

.badge-primary, .badge.bg-primary, .bg-primary {
  background-color: \$theme-primary !important;
}

.badge.text-bg-primary {
  background-color: \$theme-primary !important;
  color: \$theme-text-on-primary !important;
}

.progress-bar, .o_progressbar .o_progressbar_complete {
  background-color: \$theme-primary !important;
}


// ============================================================
// CALENDAR
// ============================================================

.o_calendar_renderer {
  .fc-event, .fc-event-dot {
    background-color: \$theme-primary !important;
    border-color: \$theme-primary-hover !important;
  }

  .fc-button-primary {
    background-color: \$theme-primary !important;
    border-color: \$theme-primary !important;
    &:hover, &:focus { background-color: \$theme-primary-hover !important; }
  }
}


// ============================================================
// CHATTER & MODALS
// ============================================================

.o_Chatter {
  .btn-primary {
    background-color: \$theme-primary !important;
    border-color: \$theme-primary !important;
  }
  .o_Chatter_sendButton { background-color: \$theme-primary !important; }
}

.modal {
  .btn-primary {
    background-color: \$theme-primary !important;
    border-color: \$theme-primary !important;
    &:hover, &:focus {
      background-color: \$theme-primary-hover !important;
      border-color: \$theme-primary-hover !important;
    }
  }
  .modal-header .btn-close:focus {
    box-shadow: 0 0 0 0.2rem rgba(\$theme-primary, 0.25) !important;
  }
}


// ============================================================
// CONTROL PANEL & BREADCRUMBS
// ============================================================

.o_control_panel {
  .btn-primary {
    background-color: \$theme-primary !important;
    border-color: \$theme-primary !important;
  }
  .o_searchview_input:focus { border-color: \$theme-primary !important; }
}

.breadcrumb-item a {
  color: \$theme-primary !important;
  &:hover { color: \$theme-primary-hover !important; }
}


// ============================================================
// HOME MENU
// ============================================================

.o_home_menu {
  .o_app:hover, .o_app:focus {
    background-color: rgba(\$theme-primary, 0.15) !important;
  }
  .o_menuitem.o_focused, .o_menuitem:hover {
    background-color: rgba(\$theme-primary, 0.15) !important;
  }
}

.o_apps .o_app:hover {
  background-color: rgba(\$theme-primary, 0.1) !important;
}


// ============================================================
// MISC
// ============================================================

.o_loading_indicator { background-color: \$theme-primary !important; }
::selection { background-color: rgba(\$theme-primary, 0.3) !important; }
::-webkit-scrollbar-thumb:hover { background-color: \$theme-primary !important; }
*:focus-visible { outline-color: \$theme-primary !important; }

[style*="71639e"],
[style*="background-color: rgb(113, 99, 158)"],
[style*="background: rgb(113, 99, 158)"] {
  background: \$theme-primary !important;
  background-color: \$theme-primary !important;
}
SCSS

echo "Generated: $OUTPUT_FILE"
echo ""
echo "Next steps:"
echo "  1. Rebuild Docker image: docker build -t odoo-custom . --build-arg CACHEBUST=\$(date +%s)"
echo "  2. Deploy to Railway: railway up"
echo "  3. Clear browser cache and refresh"
echo ""
echo "============================================================"
