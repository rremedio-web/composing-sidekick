"""MCP server profiles and closure registry for the Composing Sidekick MCP server (registered as "style-sidekick").

Declarative profiles for role-focused MCP server operation:
- compose: Complete composing core loop (default).
- session: Live hearing, inventory, listening, audition/judgment, and closeout.
- reference: Audio reference analyze/find/tag/clip/batch/reindex/apply/beat family.
- debug: Diagnostic, read, probe, status, and offline Mix Adviser tools.
- all: Complete registered surface (all tools).
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable

PROFILE_ENV_VAR = "SIDEKICK_MCP_PROFILE"
DEFAULT_PROFILE = "compose"

VALID_PROFILES: tuple[str, ...] = (
    "compose",
    "session",
    "reference",
    "debug",
    "all",
)

ALL_TOOLS: tuple[str, ...] = (
    "get_style",
    "find_phrases",
    "hear_the_room",
    "inventory_session_audio",
    "capture_loop",
    "listen_to_takes",
    "reset_ears",
    "suggest_structure",
    "develop_idea",
    "arrange_song",
    "audition_leads",
    "rate_candidates",
    "suggest_growth",
    "grow_my_loop",
    "answer_my_loop",
    "develop_my_loop",
    "lay_the_song",
    "audition_in_lanes",
    "promote_lane",
    "three_endings",
    "bury_or_marry",
    "version_courage",
    "one_hour_close",
    "confirm_bounce",
    "declare_finishing_state",
    "sombra_status",
    "no_new_notes",
    "sticky_develop",
    "judge_answer",
    "whats_missing",
    "probe_envelope_surface",
    "phrase_envelopes",
    "assign_groove",
    "read_clip",
    "read_current_clip",
    "create_idea_pack_from_clip",
    "create_idea_pack_from_live_clip",
    "prepare_idea_pack_audition_lanes",
    "score_phrase",
    "critique",
    "continue_clip",
    "finish_my_idea",
    "rate",
    "get_ratings",
    "get_gold_phrases",
    "compose_brief",
    "compose_from_profile",
    "humanize",
    "export_midi",
    "send_to_ableton",
    "write_midi_clip",
    "write_composed_to_jam",
    "clear_rejects",
    "surgery_clip",
    "analyze_mix_pass",
    "propose_mix_moves",
    "prepare_mix_ab",
    "judge_mix_pass",
    "mix_trial_report",
    "analyze_audio_ref",
    "find_audio_refs",
    "tag_audio_ref",
    "clip_audio_ref_moments",
    "analyze_audio_refs_batch",
    "reindex_audio_refs",
    "restore_audio_ref_ownership",
    "apply_audio_ref",
    "beat_from_ref",
    "close_session",
    "session_report",
    "adopt_answer",
    "register_arrangement_candidates",
    "declare_piece_version",
    "record_proposal",
    "record_reaction",
    "record_observation",
    "piece_brief",
)

COMPOSE_CORE_LOOP_CATEGORIES: dict[str, tuple[str, ...]] = {
    "hear_capture": (
        "hear_the_room",
        "inventory_session_audio",
        "capture_loop",
    ),
    "inventory_listen": (
        "listen_to_takes",
        "read_clip",
        "read_current_clip",
        "get_style",
        "find_phrases",
    ),
    "generate_write": (
        "suggest_structure",
        "arrange_song",
        "answer_my_loop",
        "develop_my_loop",
        "lay_the_song",
        "audition_in_lanes",
        "three_endings",
        "export_midi",
        "send_to_ableton",
        "write_midi_clip",
        "write_composed_to_jam",
        "surgery_clip",
        "create_idea_pack_from_clip",
        "create_idea_pack_from_live_clip",
        "prepare_idea_pack_audition_lanes",
        "apply_audio_ref",
        "beat_from_ref",
    ),
    "judge_promote_close": (
        "score_phrase",
        "rate_candidates",
        "judge_answer",
        "clear_rejects",
        "rate",
        "promote_lane",
        "close_session",
        "session_report",
    ),
}

COMPOSE_TOOLS: tuple[str, ...] = (
    # Core loop (hear/capture, inventory/listen, generate/write, judge/promote/close)
    "hear_the_room",
    "inventory_session_audio",
    "capture_loop",
    "listen_to_takes",
    "read_clip",
    "read_current_clip",
    "get_style",
    "find_phrases",
    "suggest_structure",
    "arrange_song",
    "answer_my_loop",
    "develop_my_loop",
    "lay_the_song",
    "audition_in_lanes",
    "three_endings",
    "export_midi",
    "send_to_ableton",
    "write_midi_clip",
    "write_composed_to_jam",
    "surgery_clip",
    "create_idea_pack_from_clip",
    "create_idea_pack_from_live_clip",
    "prepare_idea_pack_audition_lanes",
    "score_phrase",
    "rate_candidates",
    "judge_answer",
    "clear_rejects",
    "rate",
    "promote_lane",
    "close_session",
    "session_report",
    "adopt_answer",
    "register_arrangement_candidates",
    # Composing workflow extensions
    "develop_idea",
    "suggest_growth",
    "grow_my_loop",
    "audition_leads",
    "reset_ears",
    "sombra_status",
    "sticky_develop",
    "bury_or_marry",
    "one_hour_close",
    "confirm_bounce",
    "declare_finishing_state",
    "no_new_notes",
    "whats_missing",
    "critique",
    "continue_clip",
    "finish_my_idea",
    "compose_brief",
    "compose_from_profile",
    "humanize",
    "assign_groove",
    "get_ratings",
    "get_gold_phrases",
    # Reference retrieval & write-back tools
    "find_audio_refs",
    "apply_audio_ref",
    "beat_from_ref",
    # Piece continuity (piece identity, proposals, his reactions, observations, the brief)
    "declare_piece_version",
    "record_proposal",
    "record_reaction",
    "record_observation",
    "piece_brief",
)

SESSION_TOOLS: tuple[str, ...] = (
    # Live hearing / inventory / listening
    "hear_the_room",
    "inventory_session_audio",
    "capture_loop",
    "listen_to_takes",
    "reset_ears",
    # Clip inspection
    "read_clip",
    "read_current_clip",
    "write_midi_clip",
    "surgery_clip",
    # Audition / judgment
    "audition_leads",
    "rate_candidates",
    "judge_answer",
    "clear_rejects",
    "rate",
    "score_phrase",
    "create_idea_pack_from_clip",
    "create_idea_pack_from_live_clip",
    "prepare_idea_pack_audition_lanes",
    "audition_in_lanes",
    "promote_lane",
    # Status / closeout
    "sombra_status",
    "sticky_develop",
    "no_new_notes",
    "version_courage",
    "one_hour_close",
    "confirm_bounce",
    "declare_finishing_state",
    "close_session",
    "session_report",
    "adopt_answer",
    "register_arrangement_candidates",
    "get_ratings",
    # Piece continuity
    "declare_piece_version",
    "record_proposal",
    "record_reaction",
    "record_observation",
    "piece_brief",
)

REFERENCE_TOOLS: tuple[str, ...] = (
    # Complete audio-ref family
    "analyze_audio_ref",
    "find_audio_refs",
    "tag_audio_ref",
    "clip_audio_ref_moments",
    "analyze_audio_refs_batch",
    "reindex_audio_refs",
    "restore_audio_ref_ownership",
    "apply_audio_ref",
    "beat_from_ref",
    # Dependencies required by reference instructions/recipes
    "hear_the_room",
    "export_midi",
    "send_to_ableton",
    "get_style",
    "judge_answer",
    "clear_rejects",
    "rate",
    "close_session",
    "session_report",
)

DEBUG_TOOLS: tuple[str, ...] = (
    # Diagnostics / read / probe / status
    "sombra_status",
    "probe_envelope_surface",
    "phrase_envelopes",
    "read_clip",
    "read_current_clip",
    "get_style",
    "get_ratings",
    "score_phrase",
    "critique",
    "session_report",
    "inventory_session_audio",
    "listen_to_takes",
    "reset_ears",
    "hear_the_room",
    # Offline Mix Adviser diagnostics
    "analyze_mix_pass",
    "propose_mix_moves",
    "prepare_mix_ab",
    "judge_mix_pass",
    "mix_trial_report",
)

PROFILE_TOOL_REGISTRY: dict[str, tuple[str, ...]] = {
    "compose": COMPOSE_TOOLS,
    "session": SESSION_TOOLS,
    "reference": REFERENCE_TOOLS,
    "debug": DEBUG_TOOLS,
    "all": ALL_TOOLS,
}

PROFILE_INSTRUCTIONS: dict[str, str] = {
    "compose": (
        "Ricardo's composing sidekick (compose profile). You (Claude) compose; these tools steer "
        "with his real style profile, phrase library, arrangement templates, and scoring. "
        "Use get_style + find_phrases + suggest_structure to plan, score_phrase to self-check, "
        "export_midi writes .mid files and send_to_ableton builds a pure Producer Pal handoff. "
        "Use write_midi_clip for verified SIDEKICK session writes and surgery_clip for bounded one-bar edits. "
        "Use write_composed_to_jam for gear 5/6 material that passed validation — it stamps the ledger. "
        "Use answer_my_loop (steered with find_audio_refs), develop_my_loop, lay_the_song, three_endings, "
        "apply_audio_ref, beat_from_ref for generation. "
        "Create idea packs with create_idea_pack_from_clip or create_idea_pack_from_live_clip and "
        "prepare_idea_pack_audition_lanes. Use audition_in_lanes then promote_lane for verified Live take-lane KEEP. "
        "Use rate_candidates, judge_answer, rate, close_session, and "
        "session_report for judgment and closeout; use adopt_answer to file verified JAM-to-song survival. "
        "Use capture_loop only for SIDEKICK EARS. "
        "Call listen_to_takes with inventory_session_audio. "
        "Before writing to SIDEKICK JAM or picking phrases while Live is up, call hear_the_room then "
        "pass use_ears=True on serving tools; default stays OFF until Booth promotion. "
        "If moment.unheard lists audio clips in the current playing context, acknowledge them and ask "
        "what they are doing before structure advice — do not infer musical content from the name. "
        "Open a piece with piece_brief (read-only); declare_piece_version binds a Set to a piece only on "
        "his confirmation; record_proposal files the question/gesture/reference job/options, record_reaction "
        "his words verbatim (never a verdict), record_observation Set changes seen on a read."
    ),
    "session": (
        "Ricardo's composing sidekick (session profile). Focus on Live session hearing, take inventory, "
        "listening, clip inspection, auditioning, judgment, and closeout. "
        "Use hear_the_room to listen to the room and reset_ears to reset. "
        "Use capture_loop only for SIDEKICK EARS. Call inventory_session_audio to discover session takes, "
        "then listen_to_takes to evaluate audio clips. "
        "Inspect clips with read_clip and read_current_clip; use write_midi_clip and surgery_clip only on exact SIDEKICK tracks. "
        "Audition and evaluate with rate_candidates, judge_answer, rate, score_phrase, and audition_leads. "
        "Prepare symbolic lanes with create_idea_pack_from_clip, create_idea_pack_from_live_clip, and prepare_idea_pack_audition_lanes. "
        "Use audition_in_lanes then promote_lane for Producer Pal 2.2 Live take-lane KEEP. "
        "Track ratings with get_ratings. Track status with sombra_status, sticky_develop, and no_new_notes. "
        "Close sessions with version_courage, one_hour_close, close_session, and session_report."
        " Use adopt_answer for verified, no-mutation adoption evidence."
    ),
    "reference": (
        "Ricardo's composing sidekick (reference profile). Focus on external audio reference analysis, "
        "retrieval, clipping, tagging, and application. "
        "Use analyze_audio_ref (YOU choose focus=full|groove|harmony|melody), "
        "find_audio_refs while composing, apply_audio_ref to push a ref_id into a jam handoff "
        "(write=True -> SIDEKICK JAM), beat_from_ref to align rhythm, "
        "tag_audio_ref after KEEP/DROP, clip_audio_ref_moments for jam-sized cuts, "
        "analyze_audio_refs_batch for multiple URLs, and reindex_audio_refs to refresh. "
        "Before writing to SIDEKICK JAM call hear_the_room. "
        "Send MIDI with export_midi or send_to_ableton, align with get_style, and judge with "
        "judge_answer, rate, close_session, and session_report."
    ),
    "debug": (
        "Ricardo's composing sidekick (debug profile). Diagnostic, probe, inspection, and offline analysis tools. "
        "Check status with sombra_status, hear_the_room, and session_report. Probe envelopes with probe_envelope_surface and phrase_envelopes. "
        "Inspect clips with read_clip and read_current_clip. "
        "Inspect catalog and ratings with get_style, get_ratings, score_phrase, and critique. "
        "Manage session ears with inventory_session_audio, listen_to_takes, and reset_ears. "
        "Offline Mix Adviser diagnostics: analyze_mix_pass, propose_mix_moves, prepare_mix_ab, judge_mix_pass, mix_trial_report "
        "(listener remains opt-in and never applies mixer changes)."
    ),
    "all": (
        "Ricardo's composing sidekick (all profile). Full suite of composition, session, audio reference, "
        "diagnostic, and mix adviser tools. "
        "Use get_style, find_phrases, suggest_structure, arrange_song, answer_my_loop, develop_my_loop, "
        "lay_the_song, three_endings, export_midi, send_to_ableton, write_midi_clip, surgery_clip. "
        "Inspect and listen with hear_the_room, inventory_session_audio, listen_to_takes, reset_ears, "
        "read_clip, read_current_clip. "
        "Idea packs and audition with create_idea_pack_from_clip, create_idea_pack_from_live_clip, "
        "prepare_idea_pack_audition_lanes, audition_in_lanes, promote_lane, audition_leads, suggest_growth, grow_my_loop, develop_idea, "
        "sticky_develop, no_new_notes, bury_or_marry, version_courage, one_hour_close, whats_missing, "
        "probe_envelope_surface, phrase_envelopes, assign_groove, critique, continue_clip, finish_my_idea, "
        "compose_brief, compose_from_profile, humanize. "
        "Judge with score_phrase, rate_candidates, judge_answer, rate, get_ratings, get_gold_phrases, "
        "close_session, session_report. "
        "Use adopt_answer for exact-ID adoption evidence without moving Live. "
        "Audio refs: analyze_audio_ref, find_audio_refs, tag_audio_ref, clip_audio_ref_moments, "
        "analyze_audio_refs_batch, reindex_audio_refs, apply_audio_ref, beat_from_ref. "
        "Offline Mix Adviser: analyze_mix_pass, propose_mix_moves, prepare_mix_ab, judge_mix_pass, mix_trial_report."
    ),
}

# Declarative recipe/dependency rules:
# Each rule has:
# - id: unique identifier
# - source_tools: set of trigger tools
# - required: list of requirements (each requirement is a tuple of alternative tool names; at least one must be present)
# - description: human-readable explanation
RECIPE_RULES: list[dict[str, Any]] = [
    {
        "id": "composed_write_needs_contract_and_judgment",
        "source_tools": ("write_composed_to_jam",),
        "required": [("answer_my_loop",), ("judge_answer", "rate"), ("close_session",)],
        "description": "write_composed_to_jam needs a gear 5/6 contract from answer_my_loop "
                       "and a way to judge and sweep what it lands",
    },
    {
        "id": "listen_needs_inventory",
        "source_tools": ("listen_to_takes",),
        "required": [("inventory_session_audio",)],
        "description": "listen_to_takes requires inventory_session_audio",
    },
    {
        "id": "ears_aware_needs_hearing",
        "source_tools": (
            "find_phrases",
            "answer_my_loop",
            "develop_my_loop",
            "grow_my_loop",
            "suggest_growth",
            "audition_leads",
        ),
        "required": [("hear_the_room",)],
        "description": "ears-aware composition/retrieval requires hear_the_room",
    },
    {
        "id": "lanes_need_creation_and_judgment",
        "source_tools": ("prepare_idea_pack_audition_lanes",),
        "required": [
            ("create_idea_pack_from_clip", "create_idea_pack_from_live_clip"),
            ("rate_candidates", "judge_answer", "rate"),
        ],
        "description": "idea-pack lane preparation requires an idea-pack creation tool plus judgment",
    },
    {
        "id": "live_lane_audition_needs_promotion",
        "source_tools": ("audition_in_lanes",),
        "required": [("promote_lane",)],
        "description": "Live take-lane audition requires verified promotion",
    },
    {
        "id": "ref_writes_need_hearing_judgment_closeout",
        "source_tools": ("apply_audio_ref", "beat_from_ref"),
        "required": [
            ("hear_the_room",),
            ("tag_audio_ref", "judge_answer", "rate"),
            ("close_session", "session_report"),
        ],
        "description": "reference apply/beat writes require hearing and judgment/closeout tools",
    },
]


def validate_profile_name(name: str) -> str:
    """Validate profile name and return normalized string or raise ValueError."""
    if not isinstance(name, str):
        raise ValueError(
            f"Invalid profile type: expected string, got {type(name).__name__}"
        )
    clean = name.strip()
    if clean not in VALID_PROFILES:
        valid_list = ", ".join(VALID_PROFILES)
        raise ValueError(
            f"Unknown MCP profile '{clean}'. Valid profiles are: {valid_list}"
        )
    return clean


def get_active_profile_name() -> str:
    """Get the active MCP profile name from environment or default."""
    raw = os.environ.get(PROFILE_ENV_VAR, DEFAULT_PROFILE)
    return validate_profile_name(raw)


def get_profile_tools(profile: str) -> frozenset[str]:
    """Get the registered tool set for a given profile name."""
    clean = validate_profile_name(profile)
    return frozenset(PROFILE_TOOL_REGISTRY[clean])


def get_profile_instructions(profile: str) -> str:
    """Get the profile-aware instruction string for a given profile name."""
    clean = validate_profile_name(profile)
    return PROFILE_INSTRUCTIONS[clean]


def check_instruction_closure(profile: str) -> list[str]:
    """Verify that every tool mentioned in instructions exists in the profile."""
    clean = validate_profile_name(profile)
    tools = get_profile_tools(clean)
    instructions = get_profile_instructions(clean)
    violations: list[str] = []

    for tool_name in ALL_TOOLS:
        pattern = rf"\b{re.escape(tool_name)}\b"
        if re.search(pattern, instructions):
            if tool_name not in tools:
                violations.append(
                    f"Profile '{clean}' instructions mention tool '{tool_name}', "
                    f"but '{tool_name}' is not in profile tools."
                )
    return violations


def check_recipe_closure(profile: str) -> list[str]:
    """Verify declarative recipe/dependency rules for the given profile."""
    clean = validate_profile_name(profile)
    tools = get_profile_tools(clean)
    violations: list[str] = []

    for rule in RECIPE_RULES:
        active_triggers = [t for t in rule["source_tools"] if t in tools]
        if not active_triggers:
            continue

        for alt_group in rule["required"]:
            if not any(req in tools for req in alt_group):
                group_str = " | ".join(alt_group)
                violations.append(
                    f"Profile '{clean}' activates {active_triggers} but is missing "
                    f"required dependency ({group_str}) for rule '{rule['id']}'."
                )
    return violations


def check_tool_description_closure(
    profile: str,
    tool_descriptions: dict[str, str],
) -> list[str]:
    """Check that tool descriptions for active tools in profile do not reference inactive tools."""
    clean = validate_profile_name(profile)
    tools = get_profile_tools(clean)
    violations: list[str] = []

    for tool_name in tools:
        doc = tool_descriptions.get(tool_name) or ""
        for other in ALL_TOOLS:
            if other != tool_name:
                pattern = rf"\b{re.escape(other)}\b"
                if re.search(pattern, doc):
                    if other not in tools:
                        violations.append(
                            f"Tool '{tool_name}' in profile '{clean}' description references "
                            f"tool '{other}', but '{other}' is not in profile tools."
                        )
    return violations


def validate_profile_closure(
    profile: str,
    tool_descriptions: dict[str, str] | None = None,
) -> list[str]:
    """Run all closure checks (recipes, profile instructions, and tool descriptions)."""
    clean = validate_profile_name(profile)
    violations = check_recipe_closure(clean) + check_instruction_closure(clean)
    if tool_descriptions is not None:
        violations.extend(check_tool_description_closure(clean, tool_descriptions))
    return violations


def validate_all_profiles(tool_descriptions: dict[str, str] | None = None) -> None:
    """Validate naming, subset relationships, recipe closure, instruction closure, and description closure."""
    all_tools_set = get_profile_tools("all")
    if set(ALL_TOOLS) != all_tools_set:
        raise ValueError(
            f"'all' profile tools mismatch: {set(ALL_TOOLS) ^ all_tools_set}"
        )

    for profile in VALID_PROFILES:
        tools = get_profile_tools(profile)
        if not tools:
            raise ValueError(f"Profile '{profile}' has empty tool set")

        if not tools.issubset(all_tools_set):
            raise ValueError(
                f"Profile '{profile}' contains unknown tools: {tools - all_tools_set}"
            )

        if profile != "all" and len(tools) >= len(all_tools_set):
            raise ValueError(
                f"Profile '{profile}' must be strictly smaller than 'all' "
                f"({len(tools)} >= {len(all_tools_set)})"
            )

        violations = validate_profile_closure(profile, tool_descriptions=tool_descriptions)
        if violations:
            raise ValueError(
                f"Closure violations in profile '{profile}': {'; '.join(violations)}"
            )


def validate_registered_tools(registered_tools: Iterable[str]) -> None:
    """Validate that the server's registered tools match ALL_TOOLS exactly and without duplicates before pruning."""
    registered_list = list(registered_tools)

    # Enforce sequence uniqueness before set equality
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in registered_list:
        if name in seen:
            if name not in duplicates:
                duplicates.append(name)
        else:
            seen.add(name)
    if duplicates:
        raise ValueError(f"Duplicate tools registered on server: {sorted(duplicates)}")

    registered = set(registered_list)
    declared = set(ALL_TOOLS)
    missing = sorted(declared - registered)
    extra = sorted(registered - declared)
    if missing or extra:
        errs = []
        if extra:
            errs.append(f"extra/unclassified tools registered on server: {extra}")
        if missing:
            errs.append(f"declared tools missing from server registration: {missing}")
        raise ValueError(f"Server tool registration drift detected: {'; '.join(errs)}")


def get_server_tool_descriptions(mcp_server: Any) -> dict[str, str]:
    """Extract tool descriptions/docstrings from a FastMCP server instance or dict."""
    if isinstance(mcp_server, dict):
        return mcp_server
    if hasattr(mcp_server, "_tool_manager"):
        return {
            t.name: getattr(t.fn, "__doc__", "") or t.description or ""
            for t in mcp_server._tool_manager.list_tools()
        }
    raise TypeError(f"Unsupported server type: {type(mcp_server).__name__}")


def validate_server_registration(mcp_server: Any) -> None:
    """Validate that the FastMCP server instance registers exactly ALL_TOOLS before pruning."""
    if hasattr(mcp_server, "_tool_manager"):
        registered = [t.name for t in mcp_server._tool_manager.list_tools()]
    elif isinstance(mcp_server, (list, set, tuple)):
        registered = list(mcp_server)
    else:
        raise TypeError(f"Unsupported server type: {type(mcp_server).__name__}")
    validate_registered_tools(registered)


def validate_server_description_closure(mcp_server_or_desc_dict: Any) -> None:
    """Validate tool description closure across all profiles using server tool descriptions."""
    desc_dict = get_server_tool_descriptions(mcp_server_or_desc_dict)
    validate_all_profiles(tool_descriptions=desc_dict)


def prune_tools_for_profile(mcp_server: Any, profile: str | None = None) -> list[str]:
    """Prune tools from a FastMCP server instance that are not in the target profile.

    Returns the list of tool names that were pruned.
    """
    if profile is None:
        target_profile = get_active_profile_name()
    else:
        target_profile = validate_profile_name(profile)

    allowed = get_profile_tools(target_profile)
    registered_tools = [t.name for t in mcp_server._tool_manager.list_tools()]
    pruned: list[str] = []

    for tool_name in registered_tools:
        if tool_name not in allowed:
            mcp_server.remove_tool(tool_name)
            pruned.append(tool_name)

    return pruned
