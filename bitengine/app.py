"""BitEngine dashboard — a Streamlit front end over the real engine.

    pip install streamlit
    streamlit run app.py

Every number on this page comes from `webui.py`, which runs the actual L2
controller and L3 container and verifies the round trip before returning. There
is no mock path and no estimated figure: if a pack cannot be decoded back to the
input's SHA-256, the page says so instead of showing a saving.

This file is deliberately thin. All logic lives in `webui.py` so it can be
tested without Streamlit installed — see `tests/test_webui.py`.
"""

from __future__ import annotations

import html

import pandas as pd
import streamlit as st

import l1
import l2
import l3
import webui

st.set_page_config(page_title="BitEngine", page_icon="◧", layout="wide")

BLOCK_SIZES = {
    "4 KB": 4096,
    "8 KB": 8192,
    "16 KB": 16384,
    "32 KB": 32768,
    "64 KB": 65536,
    "256 KB": 262144,
    "1 MB": 1048576,
}


def block_map(blocks: list[webui.BlockRow]) -> None:
    """A strip of the container, one segment per run of same-codec blocks."""
    segments = webui.block_map_segments(blocks)
    if not segments:
        return
    palette = webui.codec_palette()
    total = sum(count for _, count in segments)
    pieces = []
    for codec, count in segments:
        width = count / total * 100
        colour = palette.get(codec, "#718096")
        title = html.escape(f"{codec} x{count}")
        pieces.append(
            f'<div title="{title}" style="width:{width}%;background:{colour};height:100%"></div>'
        )
    st.markdown(
        '<div style="display:flex;height:34px;width:100%;border-radius:4px;overflow:hidden;'
        'border:1px solid rgba(128,128,128,.35)">' + "".join(pieces) + "</div>",
        unsafe_allow_html=True,
    )
    legend = " &nbsp; ".join(
        f'<span style="color:{palette.get(name, "#718096")}">&#9632;</span> {html.escape(name)}'
        for name in sorted({codec for codec, _ in segments})
    )
    st.markdown(f'<div style="font-size:0.85em;opacity:.8">{legend}</div>', unsafe_allow_html=True)
    if len(blocks) > sum(c for _, c in segments):
        st.caption(f"Showing the first {sum(c for _, c in segments):,} of {len(blocks):,} blocks.")


def show_pack_result(outcome: webui.PackOutcome, source_name: str) -> None:
    if not outcome.verified:
        st.error(
            "The container did not decode back to the uploaded bytes. "
            "This is a bug — do not trust the figures below or use the file."
        )

    columns = st.columns(5)
    columns[0].metric("Original", webui.format_bytes(outcome.original_bytes))
    columns[1].metric("Container", webui.format_bytes(outcome.container_bytes))
    columns[2].metric(
        "Saved",
        webui.format_pct(outcome.saving_pct),
        delta=webui.format_bytes(outcome.saved_bytes),
    )
    columns[3].metric("Encode", f"{outcome.encode_mbs:.0f} MB/s")
    columns[4].metric("Decode", f"{outcome.decode_mbs:.0f} MB/s")

    if outcome.verified:
        st.success(f"Round trip verified — SHA-256 {outcome.manifest.sha256.hex()[:32]}…")

    st.download_button(
        "Download compressed file (.bite)",
        data=outcome.container,
        file_name=outcome.filename(source_name),
        mime="application/octet-stream",
        type="primary",
    )

    st.divider()
    left, right = st.columns([2, 1])

    with left:
        st.subheader("Blocks")
        st.caption(
            f"{outcome.manifest.block_count:,} blocks of "
            f"{webui.format_bytes(outcome.goal.block_bytes)} under “{outcome.goal.name}”."
        )
        block_map(outcome.blocks)

        if outcome.blocks:
            frame = pd.DataFrame(
                {
                    "block": [b.index for b in outcome.blocks],
                    "stored bytes": [b.payload_bytes for b in outcome.blocks],
                }
            ).set_index("block")
            st.bar_chart(frame, height=220)

    with right:
        st.subheader("Codecs")
        counts = outcome.codec_counts
        if counts:
            st.bar_chart(pd.DataFrame({"blocks": counts}), height=220)
        st.caption(
            "identical costs 1 byte, raw costs the block plus 1. Which codec each block "
            "landed in is why the saving is what it is."
        )

    st.divider()
    st.subheader("Change against saving")
    a, b = st.columns(2)
    a.metric("Bytes that differ from the base", webui.format_pct(outcome.change_pct))
    b.metric("Actually saved on disk", webui.format_pct(outcome.saving_pct))
    st.caption(
        "These are different quantities. The first is a property of your data and assumes the "
        "delta is free; the second is what the disk sees after the representation is paid for, "
        "and is always the smaller claim."
    )

    if outcome.baselines:
        st.divider()
        st.subheader("Compared with the alternative")
        rows = [
            {
                "method": "BitEngine",
                "size": webui.format_bytes(outcome.container_bytes),
                "saving": webui.format_pct(outcome.saving_pct),
                "encode": f"{outcome.encode_mbs:.0f} MB/s",
                "decode": f"{outcome.decode_mbs:.0f} MB/s",
            }
        ]
        for baseline in outcome.baselines:
            rows.append(
                {
                    "method": baseline.name,
                    "size": webui.format_bytes(baseline.encoded_bytes),
                    "saving": webui.format_pct(baseline.saving_pct),
                    "encode": f"{baseline.encode_mbs:.0f} MB/s",
                    "decode": f"{baseline.decode_mbs:.0f} MB/s",
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        best = min(outcome.baselines, key=lambda b: b.encoded_bytes)
        if best.encoded_bytes < outcome.container_bytes:
            st.warning(
                f"**{best.name} produced a smaller file than BitEngine on these inputs.** "
                "It is shown because the project's own benchmarks "
                "(`reports/head_to_head_zstd.txt`) reach the same conclusion, and a dashboard "
                "that hid it would be misleading."
            )
    elif webui.zstandard is None:
        st.info("Install `zstandard` to see BitEngine compared against `zstd --patch-from`.")


# ---------------------------------------------------------------------------
# sidebar
# ---------------------------------------------------------------------------

st.title("BitEngine")
st.caption("Bit-level delta optimisation — L1 codecs, L2 goal matching, L3 container.")

with st.sidebar:
    st.header("Engine")
    auto_goal = st.toggle("Choose the goal by probing", value=True, help=(
        "Encodes a bounded sample under every applicable goal and block size, then picks the "
        "one that measured best. Block size alone can move the result by 80 points."
    ))
    manual_block = st.selectbox(
        "Block size", ["automatic", *BLOCK_SIZES], index=0,
        help="Overrides the probe. The best value is a property of the data, not of the format.",
    )
    keyframes = st.number_input(
        "Keyframe interval (blocks)", min_value=0, value=0, step=8,
        help=(
            "0 disables. Storing every Nth block standalone bounds random-access cost to N "
            "blocks, and costs saving in exchange."
        ),
    )
    fast_decode = st.toggle("Fast-decode policy", value=False, help=(
        "Bars the bitmap codec, whose decode is sequential in the number of changed positions. "
        "Smaller saving, more predictable decode rate."
    ))
    st.divider()
    st.caption(
        f"Uploads are capped at {webui.format_bytes(webui.MAX_UPLOAD_BYTES)} because the browser "
        "holds them in memory. The CLI streams and has no such limit."
    )

pack_tab, unpack_tab, inspect_tab = st.tabs(["Pack", "Unpack", "Inspect"])


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------

with pack_tab:
    st.markdown(
        "Upload the file you want to store. Add the **previous version** as a reference and the "
        "engine stores only what changed — that is the case it is built for."
    )
    left, right = st.columns(2)
    target_upload = left.file_uploader("Target — the file to store", key="pack_target")
    reference_upload = right.file_uploader(
        "Reference — the previous version (optional)", key="pack_reference"
    )

    if target_upload is not None:
        target = target_upload.getvalue()
        reference = reference_upload.getvalue() if reference_upload is not None else None

        try:
            chosen, reports = (None, [])
            if auto_goal:
                with st.spinner("Probing goals and block sizes on a sample…"):
                    chosen, reports = webui.choose_goal(target, reference)

            options = webui.goal_choices(reference is not None)
            if not options:
                st.error("No goal applies to this combination of files.")
                st.stop()

            names = [goal.name for goal in options]
            default = names.index(chosen.name) if chosen and chosen.name in names else 0
            picked = st.selectbox("Goal", names, index=default)
            base_goal = next(goal for goal in options if goal.name == picked)

            if auto_goal and chosen and chosen.name == picked:
                base_goal = chosen  # keep the probed block size

            block_bytes = None if manual_block == "automatic" else BLOCK_SIZES[manual_block]
            goal = webui.build_goal(
                base_goal,
                block_bytes=block_bytes,
                keyframe_interval=int(keyframes),
                fast_decode=fast_decode,
            )
            st.caption(
                f"{goal.summary}  \n"
                f"base **{goal.base}** · block **{webui.format_bytes(goal.block_bytes)}** · "
                f"keyframes **{goal.keyframe_interval or 'none'}**"
            )

            if reports:
                with st.expander("What the probe measured", expanded=False):
                    measured = [r for r in reports if r.measured]
                    if measured:
                        st.dataframe(
                            pd.DataFrame(
                                [
                                    {
                                        "goal": r.goal.name,
                                        "block": webui.format_bytes(r.goal.block_bytes),
                                        "saving": webui.format_pct(r.saving_pct),
                                        "blocks": r.blocks,
                                    }
                                    for r in measured[:15]
                                ]
                            ),
                            hide_index=True,
                            width="stretch",
                        )
                    else:
                        st.info(
                            "The sample was smaller than two blocks at every candidate size, "
                            "so nothing could be measured. Pick a goal and block size by hand."
                        )

            if st.button("Pack", type="primary"):
                with st.spinner("Encoding, then decoding it back to verify…"):
                    outcome = webui.pack(target, reference, goal)
                show_pack_result(outcome, target_upload.name)

        except (ValueError, KeyError, l3.ContainerError, l1.CorruptBlock) as exc:
            st.error(str(exc))


# ---------------------------------------------------------------------------
# unpack
# ---------------------------------------------------------------------------

with unpack_tab:
    st.markdown(
        "Restore a `.bite` container. If it was packed against a reference, supply the same "
        "reference file — the container stores the difference, not the whole thing."
    )
    left, right = st.columns(2)
    container_upload = left.file_uploader("Container (.bite)", key="unpack_container")
    unpack_reference = right.file_uploader("Reference (if one was used)", key="unpack_reference")

    if container_upload is not None:
        try:
            preview = webui.inspect_container(
                container_upload.getvalue(),
                unpack_reference.getvalue() if unpack_reference is not None else None,
            )
            needs = preview.manifest.goal.needs_reference
            if needs and unpack_reference is None:
                st.warning(
                    "This container was packed against a reference. Upload it above, or the "
                    "restore will fail."
                )

            if st.button("Unpack", type="primary"):
                with st.spinner("Decoding and verifying…"):
                    result = webui.unpack(
                        container_upload.getvalue(),
                        unpack_reference.getvalue() if unpack_reference is not None else None,
                    )
                columns = st.columns(3)
                columns[0].metric("Restored", webui.format_bytes(result.restored_bytes))
                columns[1].metric("Decode", f"{result.decode_mbs:.0f} MB/s")
                columns[2].metric("Verify", "PASS" if result.verified else "FAIL")

                if result.verified:
                    st.success("SHA-256 matches the hash recorded when the container was written.")
                    st.download_button(
                        "Download restored file",
                        data=result.data,
                        file_name=container_upload.name.removesuffix(".bite") or "restored.bin",
                        mime="application/octet-stream",
                        type="primary",
                    )
                else:
                    st.error(
                        "The restored bytes do not match the recorded hash. The container is "
                        "corrupt or the wrong reference was supplied. The file is not offered."
                    )

        except (ValueError, l3.ContainerError, l1.CorruptBlock) as exc:
            st.error(str(exc))


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------

with inspect_tab:
    st.markdown("Read a container's manifest and block layout without decoding it.")
    inspect_upload = st.file_uploader("Container (.bite)", key="inspect_container")

    if inspect_upload is not None:
        try:
            report = webui.inspect_container(inspect_upload.getvalue())
            manifest = report.manifest
            goal = manifest.goal

            columns = st.columns(4)
            columns[0].metric("Original", webui.format_bytes(manifest.total_bytes))
            columns[1].metric("Container", webui.format_bytes(manifest.container_bytes))
            columns[2].metric("Saving", webui.format_pct(manifest.saving_pct))
            columns[3].metric("Blocks", f"{manifest.block_count:,}")

            st.write(
                f"**Goal** `{goal.name}` · **base** `{goal.base}` · stride {goal.stride_blocks} · "
                f"block {webui.format_bytes(goal.block_bytes)} · "
                f"keyframes {goal.keyframe_interval or 'none'}"
            )
            st.code(manifest.sha256.hex(), language=None)

            st.subheader("Blocks")
            block_map(report.blocks)
            if report.codec_counts:
                st.bar_chart(pd.DataFrame({"blocks": report.codec_counts}), height=200)

            st.metric("Blocks to reach the last one", report.deepest_chain)
            if not report.random_access_is_bounded:
                st.warning(
                    f"Reaching the final block costs {report.deepest_chain} decodes, because each "
                    "block is stored against its predecessor. Repack with a keyframe interval to "
                    "bound this."
                )

        except (ValueError, l3.ContainerError, l1.CorruptBlock) as exc:
            st.error(str(exc))

st.divider()
st.caption(
    "BitEngine stores the difference between two versions of a thing. On a single file with no "
    "reference it will find little or nothing, which is the expected result and not a fault — see "
    "`reports/` for the measured numbers, including where the engine loses."
)
