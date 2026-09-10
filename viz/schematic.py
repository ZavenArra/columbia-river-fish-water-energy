"""
Schematic of the Columbia/Snake system and where the dams sit on it.

Drawn with st.mermaid_chart rather than hand-written SVG or HTML, so it is a
native Streamlit element that follows the app's theme.

The geography, which the diagram has to get right: the Snake joins the Columbia
at Columbia river mile 324, which is UPSTREAM of McNary (RM 292). So a fish
climbing the system passes Bonneville, The Dalles, John Day and McNary, and only
then chooses between continuing up the Columbia (Priest Rapids and above) or
turning up the Snake (Ice Harbor and above). The diagram branches at that point
rather than hanging the Snake off the bottom of the list.
"""

from dams import (COLUMBIA_MAINSTEM, DAM_INFO, RIVER_MILE, SNAKE_MAINSTEM)

# Mermaid needs a light/dark pair; these follow the app's own tokens.
_STYLE = {
    "light": {"node": "#f0efec", "edge": "#52514e", "text": "#0b0b0b",
              "water": "#cde2fb", "sel_fill": "#2a78d6", "sel_text": "#ffffff",
              "off": "#fcfcfb", "off_text": "#8a8a86"},
    "dark": {"node": "#383835", "edge": "#c3c2b7", "text": "#ffffff",
             "water": "#1c5cab", "sel_fill": "#5598e7", "sel_text": "#0b0b0b",
             "off": "#242422", "off_text": "#6f6f6a"},
}


def _node(code: str) -> str:
    name = (DAM_INFO.get(code, {}) or {}).get("name", code)
    mile = RIVER_MILE.get(code)
    label = f"{name}<br/>RM {mile}" if mile else name
    return f'{code}["{label}"]'


def river_schematic(selected: str = None, available=None, mode: str = "light") -> str:
    """
    Mermaid definition for the system, flowing downstream-to-upstream.

    `available` marks which dams the page can actually chart; anything outside
    it is greyed rather than hidden, so the schematic stays the same shape and
    the missing ones are visibly missing.
    """
    css = _STYLE["dark" if mode == "dark" else "light"]
    available = set(available or [])
    lines = [
        "graph LR",
        f'  OCEAN(["Pacific Ocean"])',
    ]

    # Lower Columbia: ocean up to McNary.
    lower = [d for d in COLUMBIA_MAINSTEM if RIVER_MILE.get(d, 0) < 324]
    upper = [d for d in COLUMBIA_MAINSTEM if RIVER_MILE.get(d, 0) > 324]

    prev = "OCEAN"
    for code in lower:
        lines.append(f"  {prev} --> {_node(code)}")
        prev = code
    lines.append(f'  {prev} --> CONF{{"Snake<br/>confluence<br/>RM 324"}}')

    # Upstream of the confluence the system forks.
    prev = "CONF"
    for code in upper:
        lines.append(f"  {prev} --> {_node(code)}")
        prev = code
    lines.append(f"  {prev} --> UPPER([\"Upper Columbia\"])")

    prev = "CONF"
    for code in SNAKE_MAINSTEM:
        lines.append(f"  {prev} --> {_node(code)}")
        prev = code
    lines.append(f"  {prev} --> UPSNAKE([\"Upper Snake\"])")

    all_dams = COLUMBIA_MAINSTEM + SNAKE_MAINSTEM
    lines += [
        f"  classDef dam fill:{css['node']},stroke:{css['edge']},"
        f"color:{css['text']},stroke-width:1px",
        f"  classDef water fill:{css['water']},stroke:{css['edge']},"
        f"color:{css['text']},stroke-width:1px",
        f"  classDef sel fill:{css['sel_fill']},stroke:{css['sel_fill']},"
        f"color:{css['sel_text']},stroke-width:2px",
        f"  classDef missing fill:{css['off']},stroke:{css['off_text']},"
        f"color:{css['off_text']},stroke-width:1px,stroke-dasharray:3 3",
        f"  class {','.join(all_dams)} dam",
        "  class OCEAN,CONF,UPPER,UPSNAKE water",
    ]
    absent = [d for d in all_dams if available and d not in available]
    if absent:
        lines.append(f"  class {','.join(absent)} missing")
    if selected:
        lines.append(f"  class {selected} sel")
    return "\n".join(lines)
