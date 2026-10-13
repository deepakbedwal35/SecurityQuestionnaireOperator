"""
Reusable Streamlit rendering components.
Clean UI helpers without business logic or database queries.
"""
import os
import streamlit as st
from app.viewmodels import (
    GoalCardVM,
    PlanStepVM,
    StatusBadgeVM,
    TimelineRowVM,
)


def render_status_badge(badge: StatusBadgeVM) -> None:
    """Renders a clean styled badge for a run's status."""
    color_map = {
        "green": ("#dcfce7", "#166534"),
        "blue": ("#dbeafe", "#1e40af"),
        "orange": ("#ffedd5", "#9a3412"),
        "red": ("#fee2e2", "#991b1b"),
        "gray": ("#f3f4f6", "#374151"),
    }
    bg, fg = color_map.get(badge.color, ("#f3f4f6", "#374151"))
    st.markdown(
        f'<span style="background-color: {bg}; color: {fg}; padding: 4px 10px; border-radius: 9999px; font-weight: 600; font-size: 0.85rem;">'
        f'{badge.icon} {badge.label}'
        f'</span>',
        unsafe_allow_html=True,
    )


def render_goal_card(goal_vm: GoalCardVM) -> None:
    """Renders the understood goal summary card."""
    with st.container(border=True):
        st.subheader("🎯 Understood Goal", anchor=False)
        col1, col2 = st.columns([1, 1])
        with col1:
            st.markdown(f"**Customer:** `{goal_vm.customer}`")
            st.markdown(f"**Chosen File:** `{goal_vm.chosen_file}`")
            st.markdown(f"**Deadline:** `{goal_vm.deadline}`")
        with col2:
            st.markdown(f"**Selection Rationale:** {goal_vm.selection_rationale}")
            if goal_vm.constraints:
                st.markdown(f"**Constraints:** {', '.join(goal_vm.constraints)}")

        if goal_vm.low_confidence:
            st.warning("⚠️ Low confidence in file selection. Evidence was evaluated across multiple candidates.")
        if goal_vm.needs_clarification:
            st.error("🛑 Ambiguity could not be safely resolved without human intervention.")


def render_plan_checklist(steps: list[PlanStepVM]) -> None:
    """Renders the agent's plan checklist with status and postconditions."""
    with st.container(border=True):
        st.subheader("📋 Plan Checklist", anchor=False)
        for s in steps:
            col_icon, col_desc, col_post = st.columns([0.08, 0.62, 0.30])
            with col_icon:
                if s.is_completed:
                    st.markdown("✅")
                elif s.is_in_progress:
                    st.markdown("🔄")
                elif s.is_failed:
                    st.markdown("❌")
                else:
                    st.markdown("⚪")
            with col_desc:
                st.markdown(f"**{s.description}**  \n<small style='color: gray;'>Tool: `{s.tool_hint}`</small>", unsafe_allow_html=True)
            with col_post:
                st.markdown(f"<small>Postcondition: <code>{s.postcondition_kind}</code></small>", unsafe_allow_html=True)


def render_timeline(rows: list[TimelineRowVM]) -> None:
    """Renders the execution event timeline."""
    with st.container(border=True):
        st.subheader("⏱️ Execution Timeline", anchor=False)
        if not rows:
            st.info("No events recorded yet.")
            return

        for r in rows:
            with st.container():
                cols = st.columns([0.12, 0.18, 0.70])
                with cols[0]:
                    st.markdown(f"<small style='color: gray;'>{r.time_str}</small>", unsafe_allow_html=True)
                with cols[1]:
                    badge_style = "color: #b91c1c; font-weight: bold;" if r.is_error else ("color: #c2410c; font-weight: bold;" if r.is_replan else "color: #374151;")
                    st.markdown(f"<small style='{badge_style}'>#{r.seq} {r.event_type}</small>", unsafe_allow_html=True)
                with cols[2]:
                    st.markdown(f"<small>{r.summary}</small>", unsafe_allow_html=True)


def render_evidence_gallery(rows: list[TimelineRowVM], artifacts: dict) -> None:
    """Generic expandable evidence gallery rendering screenshots, xlsx outputs, and JSON files."""
    media_items = []
    # Collect from timeline artifact paths
    for r in rows:
        if r.artifact_path and os.path.exists(r.artifact_path):
            media_items.append((r.artifact_path, f"Event #{r.seq} ({r.event_type})"))

    # Collect from state artifacts
    for name, path in artifacts.items():
        if os.path.exists(path) and (path, name) not in media_items:
            media_items.append((path, f"Artifact: {name}"))

    if not media_items:
        return

    with st.expander("🖼️ Evidence & Artifact Gallery", expanded=False):
        for path, label in media_items:
            ext = os.path.splitext(path)[1].lower()
            if ext in (".png", ".jpg", ".jpeg", ".webp"):
                st.image(path, caption=label, use_container_width=True)
            else:
                st.markdown(f"- **{label}:** `{path}` ({os.path.getsize(path)} bytes)")

