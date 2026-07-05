"""Shared ADK setup — run this at the top of any notebook with %run adk_setup.py"""
import os
import uuid
import base64
from IPython.display import display, HTML, Image as IPImage
from dotenv import load_dotenv, find_dotenv

from google.genai import types
from google.adk.agents import Agent, LlmAgent, SequentialAgent, ParallelAgent, LoopAgent
from google.adk.models.google_llm import Gemini
from google.adk.runners import InMemoryRunner, Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import google_search, AgentTool, FunctionTool
from google.adk.tools.tool_context import ToolContext
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from google.adk.code_executors import BuiltInCodeExecutor
from google.adk.apps.app import App, ResumabilityConfig

# ── API key ──────────────────────────────────────────────────
load_dotenv(find_dotenv())
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if GOOGLE_API_KEY:
    print("✅ Gemini API key setup complete.")
else:
    print("🔑 GOOGLE_API_KEY not found. Add it to your .env file.")

# ── Retry config ─────────────────────────────────────────────
# 429 excluded: free-tier daily quota errors won't resolve by retrying.
retry_config = types.HttpRetryOptions(
    attempts=3,
    exp_base=2,
    initial_delay=1,
    http_status_codes=[500, 503, 504],
)

# ── Helpers ───────────────────────────────────────────────────
def show_python_code_and_result(response):
    """Use when an agent has a BuiltInCodeExecutor attached.

    The code executor makes the agent generate and run Python instead of doing
    math or data manipulation itself (more reliable than letting the LLM guess).
    This function digs through the raw ADK response events and prints:
      - the Python code the agent generated, and
      - the stdout output produced when that code ran.
    Call it after await runner.run_debug(...) to inspect what code was executed.
    """
    for item in response:
        if (item.content and item.content.parts
                and item.content.parts[0]
                and item.content.parts[0].function_response
                and item.content.parts[0].function_response.response):
            r = item.content.parts[0].function_response.response
            if "result" in r and r["result"] != "```":
                label = "Generated Python Code >>" if "tool_code" in r["result"] else "Generated Python Response >>"
                print(label, r["result"].replace("tool_code", ""))


def check_for_approval(events):
    """Use for long-running operations (human-in-the-loop workflows).

    When a tool calls tool_context.request_confirmation(), ADK pauses the agent
    and emits a special 'adk_request_confirmation' event instead of finishing.
    This function scans the event list for that signal and returns the two IDs
    needed to resume: approval_id (identifies this specific confirmation request)
    and invocation_id (identifies the paused execution to resume).
    Returns None if the agent completed normally without pausing.
    """
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if (part.function_call
                        and part.function_call.name == "adk_request_confirmation"):
                    return {
                        "approval_id": part.function_call.id,
                        "invocation_id": event.invocation_id,
                    }
    return None


def print_agent_response(events):
    """Print just the agent's text replies from a list of ADK events.

    ADK events carry many types of content (tool calls, function results,
    internal state updates, etc.). This filters down to the text the agent
    actually wrote and prints it with an 'Agent >' prefix.
    Use this when check_for_approval() returned None (agent finished normally)
    and you want a clean view of what the agent said.
    """
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    print(f"Agent > {part.text}")


def create_approval_response(approval_info, approved):
    """Use for long-running operations — builds the message that resumes a paused agent.

    After check_for_approval() returns approval info and the human has made a
    decision, pass that decision here to get back a properly formatted Content
    object. Send it to runner.run_async() along with the saved invocation_id so
    ADK knows to resume the paused execution rather than start a new one.

    Args:
        approval_info: The dict returned by check_for_approval().
        approved:      True to confirm the action, False to reject it.
    """
    return types.Content(
        role="user",
        parts=[types.Part(function_response=types.FunctionResponse(
            id=approval_info["approval_id"],
            name="adk_request_confirmation",
            response={"confirmed": approved},
        ))],
    )


print("✅ ADK setup complete.")
