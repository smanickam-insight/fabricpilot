"""Shared inspection chrome for operational and capacity evidence."""

from contextlib import contextmanager

import streamlit as st


@contextmanager
def inspection_panel(title, path, severity, source, close_key, on_close):
    with st.container(key="details"):
        with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
            st.caption("RESOURCE INSPECTION")
            st.button("", icon=":material/close:", help="Close details (Esc)", key=close_key, on_click=on_close)
        st.subheader(title)
        st.badge(severity, color="red" if severity in ("High", "Critical") else "orange" if severity in ("Medium", "Warning") else "blue")
        st.caption(path)
        st.caption(source)
        yield st.tabs(["Root cause & recommendation", "Telemetry & logs"])


def inspection_footer():
    return st.container(key="drawer_footer")