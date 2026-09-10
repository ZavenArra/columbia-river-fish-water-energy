"""
Columbia/Snake River Dam Dashboard.

Two pages:
  Weekly overview  -- each dam's measures against ISO week, one line per year
  Monthly profile  -- one dam, one year, monthly totals as grouped bars

Reads data/dams.db and nothing else. Connections are opened read-only
(mode=ro) so a bug here can never corrupt the store; the one exception is the
"Refresh recent data" button, which shells out to etl/refresh.py rather than
writing anything itself.

Run with:  streamlit run app.py
"""

import subprocess
import sys
from pathlib import Path

import streamlit as st

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from viz.data import DB_PATH  # noqa: E402

REFRESH_SCRIPT = REPO / "etl" / "refresh.py"


def refresh_control():
    """Shared sidebar control, shown on every page."""
    st.sidebar.divider()

    # st.rerun() wipes anything written before it, so a success message shown
    # inline would flash and vanish after a 75-second wait. Hand it to the next
    # run through session state instead.
    result = st.session_state.pop("_refresh_result", None)
    if result:
        (st.sidebar.success if result[0] == "ok" else st.sidebar.error)(result[1])

    if st.sidebar.button("Refresh recent data", width="stretch"):
        with st.spinner("Fetching the last 60 days from DART, USACE and CWMS…"):
            proc = subprocess.run(
                [sys.executable, str(REFRESH_SCRIPT), "--days", "60"],
                capture_output=True, text=True, cwd=str(REPO), timeout=900,
            )
        # Clear every cached reader so the new rows are visible immediately.
        st.cache_data.clear()
        if proc.returncode == 0:
            st.session_state["_refresh_result"] = (
                "ok", "Refreshed — the last 60 days are up to date.")
            st.rerun()
        else:
            st.sidebar.error("Refresh failed — the database was not changed.")
            with st.sidebar.expander("Error detail"):
                st.code((proc.stderr or proc.stdout or "")[-2000:])
    st.sidebar.caption("Runs `etl/refresh.py --days 60`, then clears the caches. "
                       "Takes roughly 75 seconds.")


def weekly_page():
    from viz import weekly
    weekly.render()
    refresh_control()


def monthly_page():
    from viz import monthly
    monthly.render()
    refresh_control()


def main():
    st.set_page_config(page_title="Columbia/Snake Dam Dashboard", layout="wide")

    if not DB_PATH.exists():
        st.title("Columbia/Snake River Dam Dashboard")
        st.error(f"No database at `{DB_PATH}`.")
        st.markdown("Run the backfill first:\n\n"
                    "```bash\npython etl/backfill.py --start-year 2023 "
                    "--end-year 2024\n```")
        return

    nav = st.navigation([
        st.Page(weekly_page, title="Weekly overview", icon="📈", default=True),
        st.Page(monthly_page, title="Monthly profile", icon="📊"),
    ])
    nav.run()


if __name__ == "__main__":
    main()
