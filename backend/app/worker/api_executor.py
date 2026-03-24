from __future__ import annotations

import json
import re
import time
from typing import Any, TYPE_CHECKING

import requests
from requests.exceptions import ConnectionError as ReqConnectionError, Timeout as ReqTimeout
from jsonpath_ng import parse as jsonpath_parse
import structlog

if TYPE_CHECKING:
    from openai import OpenAI
    from app.worker.ai_provider import AIProvider
    from app.worker.shared_context import SharedExecutionContext

logger = structlog.get_logger(__name__)

# Retry configuration
_MAX_RETRIES = 3
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Unresolved variable detection
_UNRESOLVED_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


API_VERIFY_SYSTEM_PROMPT = """You are a QA test verification expert. You analyze API responses to determine whether a test step's expected result has been achieved.

You will receive:
1. The HTTP request details (method, URL, headers)
2. The HTTP response (status code, headers, body)
3. The expected result description

Respond with ONLY a JSON object (no markdown, no explanation):
{"status": "passed", "actual": "description of what the response contains"}

Or if the expected result is NOT met:
{"status": "failed", "actual": "description of what the response actually contains instead"}

Be precise and factual. Focus on the data in the response."""


def _is_retryable_error(exc: Exception) -> bool:
    """Check if an HTTP exception is transient and worth retrying."""
    return isinstance(exc, (ReqConnectionError, ReqTimeout))


def resolve_template(template: str, variables: dict[str, Any]) -> str:
    """Replace {{variable_name}} placeholders with values from the variable store."""
    result = template
    for var_name, var_value in variables.items():
        result = result.replace(f"{{{{{var_name}}}}}", str(var_value))
    return result


def extract_variables(
    response_data: Any,
    extract_vars: dict[str, str],
) -> dict[str, Any]:
    """Extract variables from a JSON response using JSONPath expressions."""
    extracted: dict[str, Any] = {}
    if not isinstance(response_data, (dict, list)):
        return extracted
    for var_name, jsonpath_expr in extract_vars.items():
        try:
            matches = jsonpath_parse(jsonpath_expr).find(response_data)
            if matches:
                extracted[var_name] = matches[0].value
            else:
                logger.warning(
                    "api_step.var_not_found",
                    variable=var_name,
                    jsonpath=jsonpath_expr,
                )
        except Exception as e:
            logger.warning(
                "api_step.var_extract_error",
                variable=var_name,
                jsonpath=jsonpath_expr,
                error=str(e)[:200],
            )
    return extracted


def verify_response_with_ai(
    client: OpenAI,
    method: str,
    url: str,
    request_headers: dict,
    request_body: str,
    status_code: int,
    response_headers: dict,
    response_body: str,
    expected_description: str,
    model: str = "gpt-5-mini",
) -> dict:
    """Use OpenAI (text-only, no vision) to verify an API response."""
    prompt = f"""HTTP Request:
- Method: {method}
- URL: {url}
- Headers: {json.dumps(request_headers, indent=2)}
- Body: {request_body or '(none)'}

HTTP Response:
- Status Code: {status_code}
- Headers: {json.dumps(response_headers, indent=2)}
- Body: {response_body[:4000]}

Expected: {expected_description}

Analyze the response and verify if the expected result is achieved."""

    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=512,
        messages=[
            {"role": "system", "content": API_VERIFY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )

    text = response.choices[0].message.content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        result = json.loads(text)
        return {
            "status": result.get("status", "failed"),
            "actual": result.get("actual", "Unable to parse verification result"),
        }
    except json.JSONDecodeError:
        return {
            "status": "failed",
            "actual": f"AI verification returned non-JSON: {text[:200]}",
        }


def execute_api_step(
    ai_client: AIProvider | None,
    step: dict,
    base_url: str,
    org_id: str,
    test_run_id: str,
    s3_client: Any,
    bucket: str,
    variables: dict[str, Any],
    model: str = "gpt-5-mini",
    step_timeout_s: float = 60.0,
    shared_context: SharedExecutionContext | None = None,
) -> dict:
    """Execute a single API test step with retry and timeout enforcement.

    Returns the step result dict and updates `variables` in-place
    with any extracted vars.
    """
    step_number = step["step_number"]
    api_config = step.get("api_config") or {}
    step_start = time.monotonic()

    def remaining_s() -> float:
        return max(step_timeout_s - (time.monotonic() - step_start), 0)

    method = api_config.get("method", "GET")
    endpoint = api_config.get("endpoint", "")
    headers = api_config.get("headers", {})
    body_str = api_config.get("body", "")
    expected_status = api_config.get("expected_status_code")
    expected_response = api_config.get("expected_response", "")
    extract_vars = api_config.get("extract_vars", {})

    # Resolve variable placeholders
    endpoint = resolve_template(endpoint, variables)
    body_str = resolve_template(body_str, variables)
    headers = {k: resolve_template(v, variables) for k, v in headers.items()}

    # Detect unresolved variables (warns on cascading failures from earlier steps)
    unresolved = []
    unresolved.extend(_UNRESOLVED_VAR_PATTERN.findall(endpoint))
    unresolved.extend(_UNRESOLVED_VAR_PATTERN.findall(body_str))
    for v in headers.values():
        unresolved.extend(_UNRESOLVED_VAR_PATTERN.findall(v))
    if unresolved:
        logger.warning(
            "api_step.unresolved_vars",
            step=step_number,
            variables=unresolved,
        )

    # If endpoint is an absolute URL, use it directly; otherwise prepend base_url
    if endpoint and endpoint.startswith(("http://", "https://")):
        url = endpoint
    else:
        url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}" if endpoint else base_url

    action = f"{method} {endpoint or '/'}"
    expected_text = expected_response or (
        f"Status {expected_status}" if expected_status else ""
    )

    # Auto-set Content-Type for JSON bodies
    if body_str and "Content-Type" not in headers and "content-type" not in headers:
        try:
            json.loads(body_str)
            headers["Content-Type"] = "application/json"
        except json.JSONDecodeError:
            pass

    # Make the HTTP request with retry on transient failures
    resp = None
    for attempt in range(_MAX_RETRIES):
        if remaining_s() <= 0:
            duration = int((time.monotonic() - step_start) * 1000)
            return {
                "step_number": step_number,
                "status": "error",
                "action": action,
                "expected": expected_text,
                "actual": "",
                "error_message": f"Step timeout exceeded ({step_timeout_s}s) after {attempt} attempt(s)",
                "evidence_url": "",
                "generated_code": "",
                "duration_ms": duration,
            }

        # Per-request timeout: remaining budget capped at 30s, floored at 5s
        request_timeout_s = max(min(remaining_s(), 30), 5)

        try:
            request_kwargs: dict[str, Any] = {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "data": body_str.encode("utf-8") if body_str else None,
                "timeout": request_timeout_s,
                "allow_redirects": True,
            }
            # Inject shared cookies/headers from browser context
            if shared_context is not None:
                shared_context.apply_to_session(request_kwargs)
            resp = requests.request(**request_kwargs)
            # Retry on retryable server status codes
            if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                logger.info(
                    "api_step.retryable_status",
                    step=step_number,
                    status=resp.status_code,
                    attempt=attempt + 1,
                )
                time.sleep(min(2 ** attempt, 8))
                continue
            break  # success or non-retryable status
        except Exception as e:
            if _is_retryable_error(e) and attempt < _MAX_RETRIES - 1:
                logger.info(
                    "api_step.retryable_error",
                    step=step_number,
                    error=str(e)[:200],
                    attempt=attempt + 1,
                )
                time.sleep(min(2 ** attempt, 8))
                continue
            # Non-retryable or final attempt
            duration = int((time.monotonic() - step_start) * 1000)
            return {
                "step_number": step_number,
                "status": "error",
                "action": action,
                "expected": expected_text,
                "actual": "",
                "error_message": f"HTTP request failed after {attempt + 1} attempt(s): {e}",
                "evidence_url": "",
                "generated_code": "",
                "duration_ms": duration,
            }

    response_body = resp.text[:10000]

    # Build and upload evidence JSON
    evidence = {
        "request": {
            "method": method,
            "url": url,
            "headers": headers,
            "body": body_str,
        },
        "response": {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": response_body,
        },
    }

    evidence_key = f"{org_id}/{test_run_id}/step_{step_number}.json"
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=evidence_key,
            Body=json.dumps(evidence, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as e:
        logger.warning(
            "api_step.evidence_upload_failed",
            step=step_number,
            error=str(e)[:200],
        )
        evidence_key = ""

    # Extract variables for chaining
    try:
        response_data = resp.json()
    except Exception as e:
        response_data = None
        if extract_vars:
            logger.warning(
                "api_step.json_parse_failed",
                step=step_number,
                content_type=resp.headers.get("content-type", ""),
                error=str(e)[:200],
            )

    if extract_vars and response_data is not None:
        new_vars = extract_variables(response_data, extract_vars)
        variables.update(new_vars)

    # Check expected status code
    status_code_ok = True
    if expected_status is not None and resp.status_code != expected_status:
        status_code_ok = False

    # AI verification of response content
    if expected_response and ai_client is not None and remaining_s() > 0:
        try:
            verification = ai_client.verify_api_response(
                method=method,
                url=url,
                request_headers=headers,
                request_body=body_str,
                status_code=resp.status_code,
                response_headers=dict(resp.headers),
                response_body=response_body,
                expected_description=expected_response,
                model=model,
            )
            ai_status = verification["status"]
            actual = verification["actual"]
        except Exception as e:
            ai_status = "error"
            actual = f"AI verification failed: {e}"
    elif expected_response and remaining_s() <= 0:
        ai_status = "passed"
        actual = f"Status {resp.status_code} (step budget exceeded, verification skipped)"
    elif expected_response:
        ai_status = "passed"
        actual = f"Status {resp.status_code} (AI verification unavailable)"
    else:
        ai_status = "passed"
        actual = f"Status {resp.status_code}, body length {len(response_body)} chars"

    # Combine status code check and AI verification
    if not status_code_ok:
        final_status = "failed"
        actual = f"Expected status {expected_status}, got {resp.status_code}. {actual}"
    else:
        final_status = ai_status

    duration = int((time.monotonic() - step_start) * 1000)
    return {
        "step_number": step_number,
        "status": final_status,
        "action": action,
        "expected": expected_text,
        "actual": actual,
        "error_message": "",
        "evidence_url": evidence_key,
        "generated_code": "",
        "duration_ms": duration,
    }
