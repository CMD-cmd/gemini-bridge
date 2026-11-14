"""Stdio adapter for explicit tasks and guarded per-task delegation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import importlib
import specialist_budget
import input_limits
import model_selection
import cli_updates
import progress
import viewer
# Existing stdio launchers reload this adapter even if their original module
# list predates the checker. Keep helper fixes live without stopping transport.
importlib.reload(specialist_budget)
importlib.reload(input_limits)
importlib.reload(model_selection)
importlib.reload(cli_updates)
importlib.reload(progress)
importlib.reload(viewer)
from bridge import ROOT, VERSION, load_profile
from client import request as service_request
from jobs import KINDS
from task_mode import PURPOSES
LIMITS = input_limits.load_limits(ROOT)


def protocol_trace(request, phase):
    """Private transport diagnostics: no arguments, prompts, tokens or results."""
    try:
        directory=ROOT/'.runtime'/'mcp'
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        params=request.get('params') or {}
        row={'at':time.time(),'pid':os.getpid(),'phase':phase,'request_id':request.get('id'),
             'method':request.get('method'),'tool':params.get('name') if isinstance(params,dict) else None,
             'adapter_version':VERSION}
        fd=os.open(str(directory/('stdio-'+str(os.getpid())+'.jsonl')),os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        with os.fdopen(fd,'a') as log:log.write(json.dumps(row)+'\n')
    except (OSError,ValueError,TypeError):
        pass  # Diagnostics must never change a protocol result.

SERVER_INSTRUCTIONS = (
    "Gemini generation is off unless the user explicitly permits Gemini or invokes "
    "$gemini-bridge in this chat. Account subscription and another chat's consent do not enable it. "
    "After chat opt-in, both tiers are available without local call-count or fixed expiry limits; "
    "record the direct permission in bridge_task_mode.authorization and material scope, then "
    "bridge_submit(dispatch_mode=auto, task_id, thread_id). Codex chooses model/effort from the "
    "live catalog and current Skill/policy; fast-model tasks prefer Medium/High. "
    "A separate model-specific quote is unnecessary after chat opt-in. Manual is not an authorization bypass. "
    "Read-only status, review and native panels do not invoke Gemini or require new generation consent. "
    "Review deliverables before bridge_review. Keep existing membership authentication; no API fallback. "
    "Credits follow the account setting: do not require or override useG1Credits=false. "
    "If MCP disconnects, use the same guarded local client without duplicate "
    "generation; do not terminate host-owned stdio processes. Native directory tasks use a workspace "
    "grant in bridge_task_mode, then bridge_submit(workspace={}) to use that grant. They may read/search "
    "authorized roots, write delivery files and run approved literal commands; code_repository creates "
    "an isolated worktree from the current checkout. Existing scopes never gain this capability implicitly. "
    "Use optional bridge_doctor for zero-generation CLI inspection and bridge_plan for an unreserved preview. "
    "bridge_summary reads compact evidence. continue_job_id resumes the verified latest provider conversation "
    "with a new execution ID and current scope, same model/effort, and preserved historical material consent. "
    "After inspecting a confirmed transient failure without a delivery, Codex may use bridge_retry with "
    "this chat's thread_id, up to three total attempts. Running/unknown results, cancellations, identity/permission "
    "errors, existing deliveries and started workspace effects are not automatic retry grounds. Show a native "
    "panel for each new attempt; preserve model, effort, material and current scope. "
    "Before actual Bridge use, run the installed Skill daily CLI check script. "
    "bridge_status and legacy-card polling only prepare pending notices; they never consume them. "
    "Relay cli_update.notice only when notify=true."
)

WORKSPACE_SCHEMA = {"type":"object", "properties":{
    "read_roots":{"type":"array","items":{"type":"string"}},
    "write_roots":{"type":"array","items":{"type":"string"}},
    "project_tools":{"type":"array","items":{"type":"string","enum":["file_manifest","table_profile","pubmed_search","public_page","document_extract","literature_search"]},"description":"Selected read-only task MCP tools. Submit with transport=acp; only this task receives these tools, without global MCP inheritance."},
    "subagents":{"type":"object","properties":{"max_children":{"type":"integer","minimum":1,"maximum":4},"roles":{"type":"array","items":{"type":"string","enum":["reader","builder","reviewer"]}},"capacity_mode":{"type":"string","enum":["lifetime","concurrent"],"default":"lifetime"}},"required":["max_children"],"additionalProperties":False,"description":"Authorize one level of reader/builder/reviewer children inheriting the parent model. Builders write isolated delivery directories; parent integrates and runs commands. Legacy scopes remain reader-only."},
    "network_hosts":{"type":"array","maxItems":20,"items":{"type":"string"},"description":"Exact authorized public HTTPS hosts for read-only research; empty by default."},
    "preview":{"type":"boolean","default":False,"description":"Authorize the reserved bridge-preview HTML renderer and protected desktop/mobile screenshots; old grants remain disabled."},
    "commands":{"type":"array","items":{"type":"string"},"description":"Approved literal argv command prefixes; no shell combinations. Commands run in the task filesystem/network sandbox."},
    "code_repository":{"type":"string","description":"Repository root to copy the current checkout into an isolated worktree; source checkout is preserved."},
    "protected_paths":{"type":"array","items":{"type":"string"}}}, "additionalProperties":False}

TOOLS = [
    {"name": "bridge_models", "description": "List models reported by the installed official Antigravity CLI.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "bridge_run_text", "description": "Legacy unscoped generation entry. Disabled when explicit chat opt-in is required; use bridge_task_mode and bridge_submit(dispatch_mode=auto) with this chat's permission and material scope instead.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string", "minLength": 1, "maxLength": LIMITS["MAX_PROMPT_CHARS"]},
         "model": {"type": "string", "minLength": 1},
         "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max", "fixed"]}},
         "required": ["prompt"], "additionalProperties": False}},
    {"name": "bridge_report", "description": "Read the compact report for an existing local bridge job.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}},
                     "required": ["job_id"], "additionalProperties": False}}
]

TOOLS += [
    {"name": "bridge_submit", "description": "Submit a task after explicit Gemini opt-in in this chat; use an enabled task scope, dispatch_mode=auto and task_id/thread_id. Both tiers share the chat permission and unlimited local count defaults. Runtime model/material gates still apply; manual cannot bypass chat consent. Returns immediately; show bridge_native_panel for each new job without resubmitting generation.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string", "minLength": 1, "maxLength": LIMITS["MAX_PROMPT_CHARS"]},
         "task_purpose": {"type":"string","enum":list(PURPOSES),"description":"After explicit Gemini chat opt-in, choose model/effort by live catalog and task capability. General writing/frontend/complex reading may use high tier; light Flash tasks prefer Medium/High. No local call limits or purpose-specific short-input budgets."},
         "selection_reason": {"type":"string","minLength":1,"maxLength":500,"description":"Codex rationale for chosen live model and effort after reading Skill; purpose effort is a recommendation, not a mandatory binding."},
         "task_complexity": {"type":"string","enum":["routine","standard","complex"],"description":"Optional workload level; omitted auto calls classify from task kind and material size. Explicit model+effort take precedence."},
         "kind": {"type": "string", "enum": list(KINDS)},
         "image_count": {"type":"integer", "minimum":1, "maximum":8, "description":"Requested distinct images for kind=image; default 3."},
         "image_retry_limit": {"type":"integer", "minimum":0, "maximum":1, "description":"Retries of an explicitly failed image attempt; default 1; successful images are never regenerated."},
         "title": {"type": "string", "maxLength": 100},
         "request_id": {"type": "string", "description": "Stable caller ID: reuse only for recovery of the same submission, never for a different task."},
         "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 1800, "description": "Soft generation budget from CLI launch, excluding queue/preflight. New stream progress can extend the same call within the configured tier/effort hard cap (ordinary low600s, high/xhigh at most1800s). Unknown silence is not evidence of cloud activity. Short status/watch windows never cancel generation."},
         "files": {"type": "array", "items": {"type": "string"}, "maxItems": LIMITS["MAX_FILES"]},
         "acceptance": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
         "model": {"type": "string"}, "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max", "fixed"]},
         "dispatch_mode": {"type": "string", "enum": ["manual", "auto"]},
         "workspace":dict(WORKSPACE_SCHEMA,description="Native workspace execution. Empty object uses this task's explicit directory grant; omitted preserves the existing material handoff."),
         "task_id": {"type": "string"}, "thread_id": {"type": "string"},
         "output_contract": {"type": "object", "properties": {
             "format": {"type": "string", "enum": ["text", "json"]},
             "max_chars": {"type": "integer", "minimum": 1, "maximum": 64000},
             "strict": {"type": "boolean", "description": "Default false: presentation issues are advisory for Codex review; true enforces a machine contract."}},
             "additionalProperties": False}},
         "required": ["prompt"], "additionalProperties": False}},
    {"name": "bridge_status", "description": "Read one queued task's status and compact result; raw events remain on disk.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False}},
    {"name": "bridge_cancel", "description": "Cancel a queued or running task. Running cancellation terminates the CLI process group; confirm terminal status afterwards.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False}},
    {"name": "bridge_review", "description": "Record a content-level review: adopt sound parts, revise locally or request explanation/correction for issues, and review again. Supports accepted, accepted_with_changes, partially_accepted, revision_requested and rejected; preserves review history separately from execution status. Minor style issues alone need not reject useful work.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}, "verdict": {"type": "string", "enum": ["accepted", "accepted_with_changes", "partially_accepted", "revision_requested", "rejected"]}, "note": {"type": "string"}, **{key:{"type":"array","items":{"type":"string"},"description":description} for key,description in (("accepted_items","Sound parts adopted and retained"),("issues","Specific unresolved questions or corrections"),("changes","Codex revisions or explained changes in the adopted version"))}}, "required": ["job_id", "verdict", "note"], "additionalProperties": False}},
    {"name": "bridge_jobs", "description": "List recent queue records without returning their prompts or raw logs.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}
]

TOOLS += [{"name": "bridge_task_mode", "description": "Record, inspect, disable, or complete a task. Generation defaults off in every chat. Enable only with this chat's explicit user permission to use Gemini or direct gemini-bridge invocation; authorization stores that direct quote. Opt-in allows both tiers without another Argon-specific permission, no local count cap or fixed expiry. Account subscription alone or another chat's permission cannot enable generation. Status and closing existing scopes remain read-only with respect to Gemini.",
          "inputSchema": {"type": "object", "properties": {
              "action": {"type": "string", "enum": ["enable", "status", "disable", "complete"]},
              "thread_id": {"type": "string"}, "task_id": {"type": "string"},
              "scope": {"type": "string", "maxLength": 500},
              "authorization": {"type": "string", "maxLength": 500,"description":"Direct user permission from this chat, e.g. 本聊天可以使用 Gemini, or the actual gemini-bridge invocation. May reuse still-valid permission from earlier in this chat; never infer it from subscription, another chat, examples or quoted material."},
              "allowed_kinds": {"type": "array", "items": {"type": "string", "enum": list(KINDS)}, "maxItems": 7},
              "allowed_purposes": {"type":"array","items":{"type":"string","enum":list(PURPOSES)},"maxItems":len(PURPOSES)},
              "allowed_model_tiers": {"type":"array","items":{"type":"string","enum":["low","high"]},"maxItems":2,"description":"After chat opt-in Ultra defaults to both tiers; explicit low-only scopes remain low-only."},
              "high_tier_authorization": {"type":"string","maxLength":500,"description":"Optional compatibility quote from this chat. Generic permission to use Gemini in authorization suffices for both tiers; no separate Argon invocation is required."},
              "allowed_files": {"type": "array", "items": {"type": "string"}, "maxItems": LIMITS["MAX_FILES"]},
              "material_class": {"type": "string", "enum": ["public_or_synthetic", "user_approved_non_sensitive"]},
              "workspace":dict(WORKSPACE_SCHEMA,description="Explicit directory/command grant for this scope. Omitted scopes cannot use native workspace execution."),
              "max_jobs": {"type": "integer", "minimum": 0, "maximum": 10, "description":"After chat opt-in default 0 means unlimited for both tiers; explicit positive count caps this scope."}},
          "required": ["action", "thread_id"], "additionalProperties": False}}]


def compact_report(report):
    result = dict(report)
    response = result.get("response", "")
    result["response_truncated"] = len(response) > 6000
    result["response"] = response[:6000]
    result["full_report_path"] = str(ROOT / "runs" / result["job_id"] / "report.json")
    return result


# Reuse submission schema so optional previews accept exactly the deployed fields.
next(t for t in TOOLS if t['name']=='bridge_submit')['inputSchema']['properties']['transport']={'type':'string','enum':['auto','headless','acp'],'description':'Auto chooses verified ACP for materials and ordinary workspaces; image generation and native children keep headless. Explicit choices and continuation transport are preserved.'}
next(t for t in TOOLS if t['name']=='bridge_submit')['inputSchema']['properties']['continue_job_id']={
    'type':'string','description':'Continue the verified provider conversation from the latest job in this chat. New job/request ID and current scope required; raw provider IDs are never accepted.'}
TOOLS += [
 {'name':'bridge_retry','description':'Retry a confirmed transient failure after inspecting it, within three total attempts. Requires the same authorized chat and active scope. A repeated request for the same parent returns its existing child; no hidden daemon retries.',
  'inputSchema':{'type':'object','properties':{'job_id':{'type':'string'},'thread_id':{'type':'string'}},'required':['job_id','thread_id'],'additionalProperties':False}},
 {'name':'bridge_doctor','description':'Zero-model installed CLI/help/version and local transport checks; declarations are distinguished from actual local probes.',
  'inputSchema':{'type':'object','properties':{'refresh':{'type':'boolean'}},'additionalProperties':False}},
 {'name':'bridge_plan','description':'Optional submission preview using current authorization/model policy; no queue, job reservation or generation. Not an approval requirement.',
  'inputSchema':next(t for t in TOOLS if t['name']=='bridge_submit')['inputSchema']},
 {'name':'bridge_summary','description':'Read a compact human-facing summary of an existing job, files and verified checks; no generation.',
  'inputSchema':{'type':'object','properties':{'job_id':{'type':'string'}},'required':['job_id'],'additionalProperties':False}},
]


TOOLS.append({'name':'bridge_steer','description':'Send an idempotent text instruction to the same actively running ACP task. Does not create a new Bridge job, broaden materials, or replay on uncertain acknowledgement.',
 'inputSchema':{'type':'object','properties':{'job_id':{'type':'string'},'thread_id':{'type':'string'},'steering_id':{'type':'string'},'text':{'type':'string','minLength':1,'maxLength':16000}},'required':['job_id','thread_id','steering_id','text'],'additionalProperties':False}})

TOOLS.append({'name':'bridge_recover','description':'Inspect an interrupted ACP task and optionally load its existing context without sending a prompt or repeating generation. Runtime must be confirmed gone before remote inspection; scope is never revived.',
 'inputSchema':{'type':'object','properties':{'job_id':{'type':'string'},'thread_id':{'type':'string'},'probe':{'type':'boolean','default':False}},'required':['job_id','thread_id'],'additionalProperties':False}})

def call_tool(name, args):
    if name == 'bridge_retry':
        from bridge import validate_job_id
        return service_request('/jobs/'+validate_job_id(args['job_id'])+'/retry',{'thread_id':args['thread_id']})
    if name == "bridge_doctor":return service_request("/doctor",args)
    if name == "bridge_plan":return service_request("/plan",args)
    if name == "bridge_summary":
        from bridge import validate_job_id
        return service_request("/jobs/"+validate_job_id(args["job_id"])+"/summary")
    if name == "bridge_task_mode":
        return service_request("/task-mode", args)
    if name == "bridge_recover":
        from bridge import validate_job_id
        return service_request('/jobs/'+validate_job_id(args['job_id'])+'/recover',{k:v for k,v in args.items() if k!='job_id'})
    if name == "bridge_steer":
        from bridge import validate_job_id
        return service_request('/jobs/'+validate_job_id(args['job_id'])+'/steer',{k:args[k] for k in ('thread_id','steering_id','text')})
    if name == "bridge_submit":
        return service_request("/jobs", args)
    if name == "bridge_jobs":
        return service_request("/jobs")
    if name in ("bridge_status", "bridge_cancel", "bridge_review"):
        from bridge import validate_job_id
        job_id = validate_job_id(args["job_id"])
        path = "/jobs/" + job_id
        if name == "bridge_cancel":
            return service_request(path + "/cancel", {})
        if name == "bridge_review":
            return service_request(path + "/review", {key:args[key] for key in ('verdict','note','accepted_items','issues','changes') if key in args})
        return service_request(path)
    if name == "bridge_models":
        return service_request("/models")
    if name == "bridge_report":
        job_id = args["job_id"]
        if not isinstance(job_id, str) or not job_id or any(c not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-" for c in job_id):
            raise ValueError("Invalid job ID")
        return compact_report(json.loads((ROOT / "runs" / job_id / "report.json").read_text()))
    if name == "bridge_run_text":
        observation_deadline = time.monotonic() + 30
        prompt = args["prompt"]
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > LIMITS["MAX_PROMPT_CHARS"]:
            raise ValueError("Prompt exceeds configured character budget or is empty")
        if ("model" in args) != ("effort" in args):
            raise ValueError("Specify model and effort together")
        profile = load_profile(ROOT / "profile.json")
        if "model" in args:
            if not isinstance(args["model"], str) or not args["model"].strip():
                raise ValueError("Invalid model")
            if args["effort"] not in ("low", "medium", "high", "xhigh", "max", "fixed"):
                raise ValueError("Invalid effort")
            profile.update(model=args["model"], effort=args["effort"])
        submitted = service_request("/jobs", {"prompt": prompt, "kind": "copy",
                                               "model": profile["model"], "effort": profile["effort"]},
                                    timeout_seconds=max(.01,observation_deadline-time.monotonic()))
        # Legacy callers get a bounded observation window, not the entire
        # generation wait. The same job continues and is never re-submitted.
        deadline = observation_deadline
        while time.monotonic() < deadline:
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            try:
                value = service_request("/jobs/" + submitted["job_id"],start=False,timeout_seconds=min(5,remaining))
            except (OSError,ValueError,RuntimeError):
                return {"job_id":submitted['job_id'],"status":"still_pending","next_action":"bridge_status",
                        "query_notice":"Status query unavailable; original generation is not cancelled."}
            if value["state"]["status"] not in ("queued", "running", "cancelling"):
                return value["report"] or value["state"]
            time.sleep(0.25)
        return {"job_id": submitted["job_id"], "status": "still_pending", "next_action": "bridge_status"}
    raise ValueError("Unknown tool")


def handle(request):
    protocol_trace(request,'received')
    method = request.get("method")
    if "id" not in request:
        return None
    if method == "initialize":
        params = request.get("params", {})
        supported = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
        requested = params.get("protocolVersion")
        version = requested if requested in supported else supported[-1]
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "gemini-bridge-poc", "version": VERSION},
                  "instructions": SERVER_INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params", {})
        try:
            value = call_tool(params["name"], params.get("arguments", {}))
            value = progress.enrich(value)
            value = viewer.attach_links(value)
            from cli_updates import attach_notice
            value = attach_notice(value, deliver_notice=params['name'] != 'bridge_status')
            if isinstance(value, dict):
                value = dict(value, mcp_adapter_version=VERSION)
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            if params["name"] == "bridge_status":
                # App refreshes read data without invoking a UI-rendering tool.
                # Keep full input and review evidence out of model-visible text.
                try:
                    from task_card import compact_card, panel_snapshot, review_record
                    job_id = params.get("arguments", {})["job_id"]
                    detail=panel_snapshot(job_id)
                    result["_meta"] = {
                        "panel_card": compact_card(dict(value,deliverables=detail.get('deliverables') or []), review_record(job_id)),
                        "panel_detail": detail,
                    }
                except (OSError, ValueError, KeyError, ImportError):
                    pass  # Display enrichment cannot invalidate an existing status.
        except Exception as error:
            result = {"isError": True, "content": [{"type": "text", "text": str(error)}]}
    else:
        return {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


if __name__ == "__main__":
    import importlib
    for line in sys.stdin:
        request={}
        adapter=None
        try:
            # The stdio host can outlive edits. Keep this launcher stable and load
            # current adapters for each request; the daemon version is checked too.
            for module in ("activity", "specialist_budget", "input_limits", "preview_spec", "workspace_guard", "workspace", "preview_runtime", "preview_evidence", "command_broker", "project_tools", "project_gateway", "project_scope", "project_evidence", "native_children", "cli_capabilities", "runner_adapter", "continuation", "retry_policy", "acp_client", "acp_execution", "acp_workspace", "run_control", "session_workspace", "recovery", "catalog_cache", "project_research", "project_documents", "project_crossref", "project_results", "native_child_profiles", "activity_view", "task_card", "bridge", "model_selection", "cli_updates", "client", "task_mode", "jobs"):
                importlib.reload(importlib.import_module(module))
            adapter = importlib.reload(importlib.import_module("mcp_server"))
            request=json.loads(line)
            response = adapter.handle(request)
        except Exception:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid request"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
            if adapter is not None:adapter.protocol_trace(request,'response_flushed')
