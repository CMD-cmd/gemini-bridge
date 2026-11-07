"""Bounded coordinator-driven retries from persisted execution evidence."""
import re

MAX_ATTEMPTS = 3


def eligibility_failed_before_conversation(report):
    """CLI launched, but a positively identified eligibility request failed.

    All negative evidence must be present in the trusted final report. Silence,
    a missing init event alone, or zero output alone never establishes this.
    """
    if report.get('transport') == 'acp' or report.get('runner') != 'antigravity':
        return False
    if (report.get('native_status') != 'ERROR' or report.get('provider_error_category') != 'catalog_network'
            or not re.search(r'^Eligibility check failed: failed to get load code assist response:.*:loadCodeAssist',
                             str(report.get('native_error') or ''))):
        return False
    if report.get('duration_seconds') != 0 or report.get('exit_code') != 1:
        return False
    if any(report.get(k) for k in ('observed_model','observed_agent','provider_session_id','continue_job_id',
                                  'response','deliverables','generated_images','tool_calls','denied_actions','parse_errors')):
        return False
    if report.get('workspace_changes') != [] or report.get('workspace_audit') != {'allowed':0,'blocked':0}:
        return False
    checks=report.get('command_validation') or {}
    if any(checks.get(k) != 0 for k in ('requested','started','succeeded','not_started','failed')):
        return False
    usage=report.get('usage') or {}
    if any(usage.get(k) != 0 for k in ('input_tokens','output_tokens','thinking_tokens','total_tokens')):
        return False
    stream=report.get('stream_delivery_evidence') or {}
    return (stream.get('result_count') == 1 and stream.get('nonempty_response_observed') is False
            and stream.get('conflicting_terminal_statuses') is False
            and set(report.get('failure_reasons') or []) <= {
                'provider_error:catalog_network','model_identity_unverified','agent_identity_unverified'})


def decision(state, report=None):
    report = report or {}
    chain = state.get('retry') or {}
    attempt = chain.get('attempt', 1)
    valid = type(attempt) is int and attempt >= 1
    result = {'allowed': False, 'attempt': attempt, 'max_attempts': MAX_ATTEMPTS,
              'remaining_attempts': max(0, MAX_ATTEMPTS-attempt) if valid else 0,
              'root_job_id': chain.get('root_job_id', state['job_id'])}

    def stop(reason):
        return dict(result, reason=reason)

    if not valid:
        return stop('Invalid persisted retry count')
    if attempt >= MAX_ATTEMPTS:
        return stop('Three attempts exhausted')
    if state.get('cancel_requested'):
        return stop('User cancellation stops retries')
    if state.get('status') not in ('bridge_error', 'failed_or_unverified', 'timeout'):
        return stop('No confirmed retryable terminal failure')
    if state.get('pid') or state.get('review') == 'accepted':
        return stop('Execution or accepted delivery must be inspected first')
    if state.get('kind') == 'image':
        return stop('Use per-image recovery; never regenerate a whole image batch')
    if (state.get('execution_mode') == 'workspace' and state.get('provider_started') is not False
            and not eligibility_failed_before_conversation(report)):
        return stop('Workspace execution may have run commands; inspect its effects first')
    if report.get('deliverables') or report.get('generated_images'):
        return stop('Existing files must be inspected before another generation')
    stream = report.get('stream_delivery_evidence') or {}
    if stream.get('nonempty_response_observed') or stream.get('conflicting_terminal_statuses') or report.get('parse_errors'):
        return stop('Stream contains an earlier response or ambiguous results; inspect it first')
    response = (report.get('response') or '').strip()
    if response and response != (report.get('native_error') or '').strip():
        return stop('Existing response must be inspected before another generation')
    if report.get('observed_model') and report.get('model_matches') is not True:
        return stop('Observed model identity mismatch')
    if report.get('observed_agent') and report.get('selected_agent') != report['observed_agent']:
        return stop('Observed agent identity mismatch')
    if report.get('denied_actions') or any(
            re.search('identity_mismatch|model_mismatch|unexpected_tool|permission', str(x), re.I)
            for x in report.get('failure_reasons', [])):
        return stop('Identity or permission failure is not retryable')
    error = str(state.get('error') or '')
    category = report.get('provider_error_category')
    if category in ('authentication', 'unsupported_location', 'catalog_access_denied'):
        return stop('Authentication, location or access failure requires correction')
    if re.search(r'invalid_grant|user location is not supported|permission_denied|(?<!\d)(?:401|403)(?!\d)',
                 error+' '+str(report.get('native_error') or ''), re.I):
        return stop('Explicit authentication, location or permission failure')
    if not state.get('continue_job_id') and eligibility_failed_before_conversation(report):
        return dict(result, allowed=True, reason='CLI eligibility network failure before any provider conversation or workspace effect')
    if (state.get('provider_started') is False
            and re.search(r'CLI model catalog unavailable \((catalog_network|catalog_timeout|catalog_server_error|quota)\)', error)):
        return dict(result, allowed=True, reason='Transient catalog failure before generation')
    if report.get('transport')=='acp' and state.get('provider_started') is not False:
        acp_error=report.get('acp_error') or {}
        if category=='transport_interrupted' or acp_error.get('kind') in ('eof','transport','closed','timeout','protocol'):
            return stop('ACP transport interrupted; inspect existing session before retry')
        if report.get('all_prompts_terminal') is not True and report.get('provider_terminal_error') is not True:
            return stop('ACP provider execution is not terminally verified; recover existing context first')
    if (report.get('native_status') == 'ERROR'
            and (report.get('provider_error_category') in ('catalog_network', 'catalog_server_error', 'quota')
                 or re.search(r'\b(?:500|502|503|504|UNAVAILABLE)\b', str(report.get('native_error') or '')))):
        if state.get('continue_job_id') and not (report.get('provider_session_id') and report.get('model_matches')):
            return stop('Continuation identity is not verified')
        return dict(result, allowed=True, reason='Provider reported a transient terminal error without a delivery')
    return stop('Failure needs inspection or correction before retry')
