"""The design tokens, and the few components Streamlit has no shape for.

WHY THIS FILE EXISTS AT ALL. Most of the look is `.streamlit/config.toml`,
which Streamlit applies to every widget it draws. What it cannot express is a
composed piece — the summary panel above the views, with an eyebrow line, a
sentence and a row of levels — so those are written here as small pieces of
HTML against the same tokens, rather than as ad-hoc colours at the call site.

THE TOKENS ARE THE HOSTED PRODUCT'S. Two products with one name should not be
readable as two tools, so the values below are the ones app.gammagrid.io uses
in its own stylesheet: the same palette, the same three radii, the same type
scale of four sizes plus a hero size for a number that IS the content of its
tile. Changing one of them here without changing it there is how two products
start looking like two products.

WHY VALUES AND NOT A STYLESHEET FILE. Streamlit has no static asset path that
survives a container rebuild without extra plumbing, and the total is under a
hundred lines. Constants also mean a call site can use a colour in a Plotly
figure and in CSS and get the same one.
"""

from __future__ import annotations

# --- palette ---------------------------------------------------------------
# The brand pair carries meaning everywhere in this product: green is damping,
# purple is amplification. Nothing else may be drawn in them.
PRIMARY = "#B833E0"
PRIMARY_SOFT = "#DA8EF2"
ACCENT = "#22C55E"
ACCENT_SOFT = "#6EDE9A"

BACKGROUND = "#0A0C0B"
SURFACE = "#111614"
# A third level, for something raised above its own panel — a sticky table
# header, a tile inside a card. Never a second page background.
SURFACE_2 = "#182220"
BORDER = "#2A332E"
GRID = "#1C2320"

TEXT = "#EDEFEC"
MUTED = "#96A39B"
FAINT = "#5C6A62"

ERROR = "#F08080"
WARNING = "#E8C46A"

# --- shape -----------------------------------------------------------------
RADIUS_PANEL = "8px"
RADIUS_BOX = "6px"
RADIUS_CONTROL = "4px"
SHADOW_PANEL = "0 6px 18px rgba(0, 0, 0, 0.28)"

# --- type ------------------------------------------------------------------
TEXT_MICRO = "11px"   # an eyebrow label, an uppercase meta line
TEXT_BASE = "13px"    # the working size
TEXT_LEAD = "16px"    # a word meant to stand out a little
TEXT_STRONG = "18px"  # a view's heading; a number above body weight
TEXT_HERO = "28px"    # a number that is the point of the tile it sits in

FONT_UI = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)
FONT_MONO = "ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace"


def stylesheet() -> str:
    """The one <style> block the page injects, in tokens rather than literals.

    KEPT DELIBERATELY SHORT. Every rule here is one Streamlit does not have a
    setting for; anything that can be said in config.toml is said there
    instead, because a theme setting survives a Streamlit upgrade and a
    selector into its DOM does not.
    """
    return f"""
<style>
  /* Headings follow the chrome, not the data: Streamlit's own heading font
     setting does not reach the h2/h3 a view writes through st.subheader. */
  h1, h2, h3, h4, [data-testid="stHeading"] {{
    font-family: {FONT_UI} !important;
    letter-spacing: 0.01em;
  }}

  /* The view switcher reads as a row of pills, the shape the hosted product
     uses: a capsule per view, the active one picked out in the brand purple. */
  [data-testid="stSegmentedControl"] button {{
    border-radius: 999px !important;
    padding: 4px 14px !important;
  }}

  /* Numbers read as numbers. Tabular figures line up in a column and stop a
     price from jittering as it changes, which a proportional face does. */
  [data-testid="stMetricValue"],
  [data-testid="stDataFrame"],
  .gg-mono {{
    font-family: {FONT_MONO};
    font-variant-numeric: tabular-nums;
  }}

  /* The summary panel above the views. */
  .gg-weather {{
    display: flex;
    align-items: center;
    gap: 18px;
    flex-wrap: wrap;
    border: 1px solid {BORDER};
    border-radius: {RADIUS_PANEL};
    background: {SURFACE};
    box-shadow: {SHADOW_PANEL};
    padding: 14px 18px;
    margin-bottom: 0.6rem;
  }}
  .gg-weather-words {{ flex: 1 1 260px; min-width: 0; }}
  .gg-weather-label {{
    font-size: {TEXT_LEAD};
    font-weight: 700;
    color: {TEXT};
    letter-spacing: 0.02em;
  }}
  .gg-weather-scope {{
    font-weight: 400;
    color: {FAINT};
    font-size: {TEXT_MICRO};
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }}
  .gg-weather-sentence {{
    color: {MUTED};
    font-size: {TEXT_BASE};
    margin-top: 2px;
  }}
  .gg-levels {{
    display: flex;
    gap: 22px;
    flex-wrap: wrap;
    margin: 0;
    font-variant-numeric: tabular-nums;
  }}
  .gg-levels dt {{
    font-size: {TEXT_MICRO};
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: {FAINT};
  }}
  .gg-levels dd {{
    margin: 0;
    font-family: {FONT_MONO};
    font-size: {TEXT_STRONG};
    font-weight: 700;
    color: {TEXT};
  }}
  .gg-green {{ color: {ACCENT_SOFT}; }}
  .gg-purple {{ color: {PRIMARY_SOFT}; }}

  /* The status line beside the ticker: a dot, then when this screen is from. */
  .gg-status {{
    color: {MUTED};
    font-size: {TEXT_BASE};
    text-align: right;
    padding-top: 6px;
  }}
  .gg-status b {{ color: {TEXT}; font-weight: 600; }}
  .gg-dot {{
    display: inline-block;
    width: 7px; height: 7px;
    border-radius: 50%;
    margin-right: 7px;
    vertical-align: middle;
  }}

  /* The ladder of changes: a table, but not a dataframe — the chips carry
     meaning in colour and a grid of cells would bury that. */
  .gg-ladder {{
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
    font-size: {TEXT_BASE};
  }}
  .gg-ladder th:first-child, .gg-ladder td:first-child {{ width: 34%; }}
  .gg-ladder th:nth-child(2), .gg-ladder td:nth-child(2),
  .gg-ladder th:nth-child(3), .gg-ladder td:nth-child(3) {{ width: 18%; }}
  .gg-ladder th {{
    text-align: left;
    font-size: {TEXT_MICRO};
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: {FAINT};
    font-weight: 600;
    padding: 0 12px 6px 0;
  }}
  .gg-ladder td {{
    padding: 7px 12px 7px 0;
    border-top: 1px solid {GRID};
    vertical-align: baseline;
  }}
  .gg-ladder .gg-num {{
    font-family: {FONT_MONO};
    font-variant-numeric: tabular-nums;
  }}
  .gg-ladder .gg-before {{ color: {MUTED}; }}
  .gg-ladder .gg-note {{
    display: block;
    color: {FAINT};
    font-size: {TEXT_MICRO};
  }}
</style>
"""
