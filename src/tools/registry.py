"""The tool catalogue: name -> function, description and JSON schema.

The agent (Claude tool calling) and the MCP server both expose exactly these tools.
Descriptions are written as prompts: when to use, when not to, and example arguments.
Read tools never write. Action tools (last four) only write Draft, Question and Watch nodes.
Strict tool use allows at most 16 optional (nullable) parameters across all tools together
(tests/test_read_tools.py checks it), so read_thread and read_kep take no as_of here.
"""
from src.tools import act, read

_AS_OF = {"type": "string", "description": "Optional point in time: a date (2024-06-01) or a release (v1.29). "
                                            "Omit for the current state."}

TOOLS = {
    "trace_decision": {
        "fn": read.trace_decision,
        "description": (
            "Use for WHY / STATUS / IS-IT-STILL-VALID questions about one decision (a KEP or a feature): its status "
            "now or at a past time, status history with source PRs, what replaced it, who approved it, and the "
            "comments that state the reason. Returns found=false when no KEP matches: then say no such decision "
            "exists. Do not use for 'who knows' or 'what does it affect'. "
            "Examples: {\"kep_or_query\": \"KEP-3063\"}, {\"kep_or_query\": \"sidecar containers\", \"as_of\": \"v1.29\"}"),
        "schema": {"type": "object", "properties": {
            "kep_or_query": {"type": "string", "description": "A KEP id (KEP-753) or the feature's name"},
            "as_of": _AS_OF}, "required": ["kep_or_query"], "additionalProperties": False},
    },
    "who_owns": {
        "fn": read.who_owns,
        "description": (
            "Use for OWNERSHIP questions: which team owns a component, code path, KEP or feature gate, its current "
            "leads, and the people who approve that code. Examples: {\"target\": \"kubelet\"}, "
            "{\"target\": \"pkg/scheduler\"}, {\"target\": \"KEP-753\"}. Do not use for expertise ranking (who_knows)."),
        "schema": {"type": "object", "properties": {
            "target": {"type": "string", "description": "Component, code path, KEP id, feature gate or team"},
            "as_of": _AS_OF}, "required": ["target"], "additionalProperties": False},
    },
    "who_knows": {
        "fn": read.who_knows,
        "description": (
            "Use for EXPERTISE questions: who knows most about a topic, or who to ask. Scores people by authoring, "
            "approving and reviewing the related KEPs, approving their PRs and discussing them. "
            "Example: {\"topic\": \"swap memory on nodes\"}. Do not use for formal ownership (who_owns)."),
        "schema": {"type": "object", "properties": {
            "topic": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
            "required": ["topic"], "additionalProperties": False},
    },
    "impact_of": {
        "fn": read.impact_of,
        "description": (
            "Use for IMPACT questions: what a KEP affects (feature gates, components, code areas and their "
            "approvers, participating teams) and related or replacing KEPs up to `depth` hops; or which KEPs affect "
            "a component. Examples: {\"entity\": \"KEP-753\"}, {\"entity\": \"kube-scheduler\", \"depth\": 2}."),
        "schema": {"type": "object", "properties": {
            "entity": {"type": "string"}, "depth": {"type": "integer", "minimum": 1, "maximum": 3}},
            "required": ["entity"], "additionalProperties": False},
    },
    "timeline": {
        "fn": read.timeline,
        "description": (
            "Use for HISTORY questions about one KEP: every recorded change (created, status, stage, release "
            "targets, approvers) with its source PR, oldest first, and its release targets at `as_of`. "
            "Example: {\"kep\": \"KEP-753\"}."),
        "schema": {"type": "object", "properties": {"kep": {"type": "string"}, "as_of": _AS_OF},
                   "required": ["kep"], "additionalProperties": False},
    },
    "what_changed": {
        "fn": read.what_changed,
        "description": (
            "Use for WHAT-CHANGED / WHAT'S-NEW questions: changes since a date or release, optionally for one team "
            "or KEP (status changes, new KEPs, leadership changes, approver changes), newest first. "
            "Examples: {\"since\": \"2026-09-01\", \"team\": \"sig-node\"}, {\"since\": \"v1.36\"}."),
        "schema": {"type": "object", "properties": {
            "since": {"type": "string", "description": "Date (YYYY-MM-DD) or release (v1.36)"},
            "team": {"type": "string", "description": "Team id such as sig-node"},
            "kep": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "required": ["since"], "additionalProperties": False},
    },
    "connect": {
        "fn": read.connect,
        "description": (
            "Use for HOW-ARE-THESE-CONNECTED questions between two people, teams, KEPs or components: the shortest "
            "path through ownership, authorship, review and decision links (at most 4 hops). Past relationships "
            "carry an 'until' date. Example: {\"a\": \"pohly\", \"b\": \"KEP-753\"}."),
        "schema": {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                   "required": ["a", "b"], "additionalProperties": False},
    },
    "influence": {
        "fn": read.influence,
        "description": (
            "Use for KEY-PEOPLE questions: most influential reviewers (metric 'pagerank') or people who bridge "
            "groups (metric 'betweenness') on the reviewer graph, optionally within one team. "
            "Examples: {\"metric\": \"pagerank\"}, {\"team\": \"sig-scheduling\", \"metric\": \"betweenness\"}."),
        "schema": {"type": "object", "properties": {
            "team": {"type": "string"}, "metric": {"type": "string", "enum": ["pagerank", "betweenness"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25}}, "additionalProperties": False},
    },
    "search": {
        "fn": read.search,
        "description": (
            "Use for WHAT WAS SAID about a topic: finds passages in KEP design docs and in issue/PR discussions "
            "(with author, date and link), when the question is about content, arguments, concerns, examples or "
            "details rather than status, owners or experts. For WHY a KEP was built, decided or dropped, or its "
            "status, use trace_decision first (it has the checked rationale). Pass kep to look only inside one "
            "KEP's doc and threads. "
            "Returns found=false when nothing written matches: then say it is not in the graph. Quote only what a "
            "passage says. Examples: {\"query\": \"why sidecars need restartPolicy Always\"}, "
            "{\"query\": \"graceful termination order\", \"kep\": \"KEP-753\"}, "
            "{\"query\": \"memory limit decrease\", \"kind\": \"discussion\"}"),
        "schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for, in plain words"},
            "kep": {"type": "string", "description": "Optional KEP id or name to search only within"},
            "kind": {"type": "string", "enum": ["doc", "discussion"], "description": "Optional: only design docs or only discussions"},
            "as_of": _AS_OF}, "required": ["query"], "additionalProperties": False},
    },
    "read_thread": {
        "fn": read.read_thread,
        "description": (
            "Use for questions about ONE ISSUE OR PULL REQUEST in kubernetes/enhancements: what it is, who opened "
            "it, its state, linked KEPs, reviewers' verdicts and what the discussion said (first and last comments). "
            "A KEP id reads its tracking issue. Examples: {\"ref\": \"#4255\"}, "
            "{\"ref\": \"https://github.com/kubernetes/enhancements/pull/4183\"}, {\"ref\": \"KEP-3063\"}"),
        "schema": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "#number, issue/PR URL, or KEP id"}},
            "required": ["ref"], "additionalProperties": False},
    },
    "read_kep": {
        "fn": read.read_kep,
        "description": (
            "Use for WHAT A KEP'S DESIGN DOCUMENT SAYS: its goals, non-goals, proposal, risks, alternatives, test "
            "plan or graduation criteria. Without section it lists the sections; with section it returns that "
            "section's text. Not for why it was decided, status or history: use trace_decision. "
            "Examples: {\"kep\": \"KEP-753\", "
            "\"section\": \"non-goals\"}, {\"kep\": \"in-place pod resize\", \"section\": \"alternatives\"}"),
        "schema": {"type": "object", "properties": {
            "kep": {"type": "string", "description": "A KEP id or the feature's name"},
            "section": {"type": "string", "description": "Optional section name, e.g. goals, non-goals, risks"}},
            "required": ["kep"], "additionalProperties": False},
    },
    "resolve": {
        "fn": read.resolve,
        "description": (
            "Use only when unsure which KEP, team, person or component a name refers to: returns ranked "
            "candidates and how each matched. Example: {\"text\": \"DRA\"}."),
        "schema": {"type": "object", "properties": {"text": {"type": "string"}},
                   "required": ["text"], "additionalProperties": False},
    },
    "recommend_reviewers": {
        "fn": act.recommend_reviewers,
        "description": (
            "ACTION. Use when the user wants REVIEWERS for a change to a KEP, component or code path: ranks people "
            "by current approver roles on the affected code and KEP plus expertise, excluding the KEP's authors, "
            "each with reasons. Example: {\"kep_or_path\": \"KEP-4381\"}, {\"kep_or_path\": \"pkg/scheduler\"}."),
        "schema": {"type": "object", "properties": {
            "kep_or_path": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
            "required": ["kep_or_path"], "additionalProperties": False},
    },
    "draft_decision_brief": {
        "fn": act.draft_decision_brief,
        "description": (
            "ACTION. Use when the user asks to WRITE, DRAFT or SUMMARIZE a brief/memo/update about one decision for "
            "others: builds a markdown brief (status, why with quotes, impact, who to tell, sources) and saves it "
            "as a draft. Show the returned markdown. Example: {\"kep\": \"KEP-4381\"}."),
        "schema": {"type": "object", "properties": {"kep": {"type": "string"}},
                   "required": ["kep"], "additionalProperties": False},
    },
    "route_question": {
        "fn": act.route_question,
        "description": (
            "ACTION. Use when the user wants to know WHO TO ASK or wants a question passed on: picks experts and "
            "the owning team's tech lead for the topic and records the question routed to them (nothing is sent). "
            "Example: {\"question\": \"Can DRA drivers support autoscaling?\", \"topic\": \"dynamic resource allocation\"}."),
        "schema": {"type": "object", "properties": {"question": {"type": "string"}, "topic": {"type": "string"}},
                   "required": ["question", "topic"], "additionalProperties": False},
    },
    "watch": {
        "fn": act.watch,
        "description": (
            "ACTION. Use when the user wants to FOLLOW, WATCH or BE NOTIFIED about a KEP, team, component or person. "
            "Changes then appear in the live feed. Example: {\"entity\": \"KEP-2699\"}."),
        "schema": {"type": "object", "properties": {"entity": {"type": "string"}},
                   "required": ["entity"], "additionalProperties": False},
    },
}


def call_tool(name: str, args: dict, graph_name: str | None = None) -> dict:
    """Run a tool by name with validated arguments (unknown names or arguments raise ValueError)."""
    if name not in TOOLS:
        raise ValueError(f"unknown tool {name}")
    allowed = set(TOOLS[name]["schema"]["properties"])
    extra = set(args) - allowed
    if extra:
        raise ValueError(f"unexpected arguments for {name}: {sorted(extra)}")
    for key, value in args.items():
        if isinstance(value, str) and len(value) > 300:
            raise ValueError(f"argument {key} is too long")
    return TOOLS[name]["fn"](**args, graph_name=graph_name)
