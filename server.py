from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse
import uvicorn
import threading
from fastmcp import FastMCP
import httpx
import os
import json
import re
import xml.etree.ElementTree as ET
from typing import Optional
from datetime import datetime
import uuid

mcp = FastMCP("WorkFlowy API")

WORKFLOWY_URL = "https://workflowy.com"
LOGIN_URL = f"{WORKFLOWY_URL}/ajax_login"
INITIALIZATION_DATA_URL = f"{WORKFLOWY_URL}/get_initialization_data?client_version=21&client_version_v2=28&no_root_children=1"
TREE_DATA_URL = f"{WORKFLOWY_URL}/get_tree_data/"
SHARED_TREE_DATA_URL = f"{WORKFLOWY_URL}/get_tree_data/?share_id="
PUSH_AND_POLL_URL = f"{WORKFLOWY_URL}/push_and_poll"
CLIENT_VERSION = "21"


def create_client_id():
    now = datetime.utcnow()
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"


async def login(email: str, password: str, client: httpx.AsyncClient) -> dict:
    """Login to WorkFlowy and return session headers."""
    response = await client.post(
        LOGIN_URL,
        data={"username": email, "password": password},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0",
        },
        follow_redirects=True,
    )
    if response.status_code not in (200, 302):
        raise Exception(f"Login failed with status {response.status_code}: {response.text}")
    
    # Extract session cookie
    session_cookie = None
    for cookie in response.cookies.jar:
        if cookie.name == "sessionid":
            session_cookie = cookie.value
            break
    
    if not session_cookie:
        # Try from set-cookie headers
        for key, value in response.headers.items():
            if key.lower() == "set-cookie" and "sessionid=" in value:
                match = re.search(r"sessionid=([^;]+)", value)
                if match:
                    session_cookie = match.group(1)
                    break
    
    if not session_cookie:
        raise Exception("Login failed: no session cookie received. Check credentials.")
    
    return {"Cookie": f"sessionid={session_cookie}"}


async def get_tree_data(session_headers: dict, client: httpx.AsyncClient) -> list:
    """Fetch all tree data from WorkFlowy."""
    response = await client.get(
        TREE_DATA_URL,
        headers={**session_headers, "User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
    )
    if response.status_code != 200:
        raise Exception(f"Failed to get tree data: {response.status_code}")
    data = response.json()
    return data.get("items", [])


def build_tree(items: list, parent_id: str = "None") -> list:
    """Build a hierarchical tree from flat items list."""
    result = []
    for item in items:
        if str(item.get("parentid", "")) == str(parent_id):
            node = {
                "id": item.get("id"),
                "name": item.get("nm", ""),
                "note": item.get("no", ""),
                "completed": item.get("cp") is not None,
                "children": build_tree(items, item.get("id", "")),
            }
            if item.get("metadata"):
                node["metadata"] = item["metadata"]
            result.append(node)
    return result


def flatten_tree(items: list) -> list:
    """Flatten a hierarchical tree to a flat list."""
    result = []
    for item in items:
        result.append(item)
        if item.get("children"):
            result.extend(flatten_tree(item["children"]))
    return result


def search_in_items(items: list, query: str, find_all: bool = True) -> list:
    """Search for items matching a query string or regex pattern."""
    results = []
    flat = flatten_tree(items)
    
    try:
        pattern = re.compile(query)
        use_regex = True
    except re.error:
        use_regex = False
    
    for item in flat:
        name = item.get("name", "")
        if use_regex:
            match = pattern.search(name)
        else:
            match = query.lower() in name.lower()
        
        if match:
            results.append(item)
            if not find_all:
                break
    
    return results


def find_item_by_name(items: list, name: str) -> Optional[dict]:
    """Find an item by name (exact or regex)."""
    results = search_in_items(items, name, find_all=False)
    return results[0] if results else None


def items_to_plaintext(items: list, indent: int = 0, include_completed: bool = True) -> str:
    """Convert items tree to plaintext."""
    lines = []
    for item in items:
        if not include_completed and item.get("completed"):
            continue
        prefix = "  " * indent + "- "
        name = item.get("name", "")
        lines.append(f"{prefix}{name}")
        if item.get("note"):
            note_prefix = "  " * (indent + 1) + "  "
            lines.append(f"{note_prefix}{item['note']}")
        if item.get("children"):
            lines.append(items_to_plaintext(item["children"], indent + 1, include_completed))
    return "\n".join(lines)


def items_to_opml(items: list, include_completed: bool = True) -> str:
    """Convert items tree to OPML format."""
    root = ET.Element("opml", version="2.0")
    head = ET.SubElement(root, "head")
    title = ET.SubElement(head, "title")
    title.text = "WorkFlowy Export"
    body = ET.SubElement(root, "body")
    
    def add_items(parent_el, item_list):
        for item in item_list:
            if not include_completed and item.get("completed"):
                continue
            attrs = {"text": item.get("name", "")}
            if item.get("note"):
                attrs["_note"] = item["note"]
            if item.get("completed"):
                attrs["complete"] = "true"
            outline = ET.SubElement(parent_el, "outline", **attrs)
            if item.get("children"):
                add_items(outline, item["children"])
    
    add_items(body, items)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


async def push_operations(session_headers: dict, operations: list, client: httpx.AsyncClient) -> dict:
    """Push operations to WorkFlowy."""
    client_id = create_client_id()
    payload = {
        "client_id": client_id,
        "client_version": CLIENT_VERSION,
        "push_poll_id": str(uuid.uuid4()).replace("-", "")[:8],
        "push_poll_data": json.dumps([{
            "most_recent_operation_transaction_id": "",
            "operations": operations,
        }]),
    }
    response = await client.post(
        PUSH_AND_POLL_URL,
        data=payload,
        headers={
            **session_headers,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0",
        },
        follow_redirects=True,
    )
    if response.status_code != 200:
        raise Exception(f"Push failed: {response.status_code}: {response.text}")
    return response.json()


@mcp.tool()
async def authenticate_workflowy(email: str, password: str) -> dict:
    """Authenticate with WorkFlowy using email and password to establish a session. Use this first before any other WorkFlowy operations."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            session_headers = await login(email, password, client)
            return {
                "success": True,
                "message": "Successfully authenticated with WorkFlowy",
                "session_established": True,
                "email": email,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }


@mcp.tool()
async def get_document(email: str, password: str) -> dict:
    """Fetch and load the full WorkFlowy document/outline into an interactive structure. Returns all lists, items, and their hierarchy."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            session_headers = await login(email, password, client)
            raw_items = await get_tree_data(session_headers, client)
            tree = build_tree(raw_items)
            flat = flatten_tree(tree)
            return {
                "success": True,
                "total_items": len(flat),
                "root_items": len(tree),
                "document": tree,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }


@mcp.tool()
async def search_lists(
    email: str,
    password: str,
    query: str,
    find_all: bool = True,
    parent_path: Optional[str] = None,
) -> dict:
    """Search for lists or items within a WorkFlowy document using a text query or regex pattern."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            session_headers = await login(email, password, client)
            raw_items = await get_tree_data(session_headers, client)
            tree = build_tree(raw_items)
            
            search_scope = tree
            if parent_path:
                parent = find_item_by_name(tree, parent_path)
                if parent and parent.get("children"):
                    search_scope = parent["children"]
                elif parent:
                    search_scope = []
            
            results = search_in_items(search_scope, query, find_all=find_all)
            
            # Remove children from results to keep response manageable
            simplified = []
            for r in results:
                simplified.append({
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "note": r.get("note"),
                    "completed": r.get("completed"),
                    "child_count": len(r.get("children", [])),
                })
            
            return {
                "success": True,
                "query": query,
                "count": len(simplified),
                "results": simplified,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }


@mcp.tool()
async def edit_list(
    email: str,
    password: str,
    list_name: str,
    new_name: Optional[str] = None,
    new_note: Optional[str] = None,
    completed: Optional[bool] = None,
) -> dict:
    """Edit a WorkFlowy list item by setting its name, note, or completion status."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            session_headers = await login(email, password, client)
            raw_items = await get_tree_data(session_headers, client)
            tree = build_tree(raw_items)
            
            item = find_item_by_name(tree, list_name)
            if not item:
                return {
                    "success": False,
                    "error": f"List item matching '{list_name}' not found",
                }
            
            item_id = item["id"]
            operations = []
            timestamp = int(datetime.utcnow().timestamp())
            
            if new_name is not None:
                operations.append({
                    "type": "edit",
                    "data": {
                        "projectid": item_id,
                        "name": new_name,
                    },
                    "client_timestamp": timestamp,
                    "undo_data": {
                        "previous_name": item.get("name", ""),
                        "previous_last_modified": timestamp,
                    },
                })
            
            if new_note is not None:
                operations.append({
                    "type": "edit",
                    "data": {
                        "projectid": item_id,
                        "description": new_note,
                    },
                    "client_timestamp": timestamp,
                    "undo_data": {
                        "previous_description": item.get("note", ""),
                        "previous_last_modified": timestamp,
                    },
                })
            
            if completed is not None:
                if completed:
                    operations.append({
                        "type": "complete",
                        "data": {"projectid": item_id},
                        "client_timestamp": timestamp,
                        "undo_data": {"previous_last_modified": timestamp},
                    })
                else:
                    operations.append({
                        "type": "uncomplete",
                        "data": {"projectid": item_id},
                        "client_timestamp": timestamp,
                        "undo_data": {"previous_last_modified": timestamp},
                    })
            
            if not operations:
                return {
                    "success": False,
                    "error": "No changes specified. Provide new_name, new_note, or completed.",
                }
            
            result = await push_operations(session_headers, operations, client)
            
            return {
                "success": True,
                "message": f"Successfully edited '{item.get('name')}'",
                "item_id": item_id,
                "changes": {
                    "new_name": new_name,
                    "new_note": new_note,
                    "completed": completed,
                },
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }


@mcp.tool()
async def create_list_item(
    email: str,
    password: str,
    name: str,
    parent_name: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Create a new list item or sublist under a specified parent list in WorkFlowy."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            session_headers = await login(email, password, client)
            raw_items = await get_tree_data(session_headers, client)
            tree = build_tree(raw_items)
            
            parent_id = "None"  # Root level
            parent_item_name = "root"
            
            if parent_name:
                parent_item = find_item_by_name(tree, parent_name)
                if not parent_item:
                    return {
                        "success": False,
                        "error": f"Parent list matching '{parent_name}' not found",
                    }
                parent_id = parent_item["id"]
                parent_item_name = parent_item["name"]
            
            new_id = str(uuid.uuid4())
            timestamp = int(datetime.utcnow().timestamp())
            
            operations = [{
                "type": "create",
                "data": {
                    "projectid": new_id,
                    "parentid": parent_id,
                    "priority": 0,
                },
                "client_timestamp": timestamp,
                "undo_data": {},
            }]
            
            operations.append({
                "type": "edit",
                "data": {
                    "projectid": new_id,
                    "name": name,
                },
                "client_timestamp": timestamp,
                "undo_data": {
                    "previous_name": "",
                    "previous_last_modified": timestamp,
                },
            })
            
            if note:
                operations.append({
                    "type": "edit",
                    "data": {
                        "projectid": new_id,
                        "description": note,
                    },
                    "client_timestamp": timestamp,
                    "undo_data": {
                        "previous_description": "",
                        "previous_last_modified": timestamp,
                    },
                })
            
            result = await push_operations(session_headers, operations, client)
            
            return {
                "success": True,
                "message": f"Successfully created '{name}' under '{parent_item_name}'",
                "new_item_id": new_id,
                "name": name,
                "note": note,
                "parent": parent_item_name,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }


@mcp.tool()
async def export_document(
    email: str,
    password: str,
    format: str,
    list_name: Optional[str] = None,
    include_completed: bool = True,
) -> dict:
    """Export WorkFlowy lists to a specified format: json, plaintext, string, or opml."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            session_headers = await login(email, password, client)
            raw_items = await get_tree_data(session_headers, client)
            tree = build_tree(raw_items)
            
            export_items = tree
            if list_name:
                found = find_item_by_name(tree, list_name)
                if not found:
                    return {
                        "success": False,
                        "error": f"List matching '{list_name}' not found",
                    }
                export_items = [found]
            
            if not include_completed:
                def filter_completed(items):
                    result = []
                    for item in items:
                        if item.get("completed"):
                            continue
                        filtered = dict(item)
                        if filtered.get("children"):
                            filtered["children"] = filter_completed(filtered["children"])
                        result.append(filtered)
                    return result
                export_items = filter_completed(export_items)
            
            fmt = format.lower().strip()
            
            if fmt == "json":
                exported = export_items
                return {
                    "success": True,
                    "format": "json",
                    "data": exported,
                    "item_count": len(flatten_tree(exported)),
                }
            elif fmt in ("plaintext", "string"):
                text = items_to_plaintext(export_items, include_completed=include_completed)
                return {
                    "success": True,
                    "format": fmt,
                    "data": text,
                }
            elif fmt == "opml":
                opml_str = items_to_opml(export_items, include_completed=include_completed)
                return {
                    "success": True,
                    "format": "opml",
                    "data": opml_str,
                }
            else:
                return {
                    "success": False,
                    "error": f"Unknown format '{format}'. Supported: json, plaintext, string, opml",
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }


@mcp.tool()
async def get_shared_document(
    share_url: str,
    export_format: str = "json",
) -> dict:
    """Access and read a publicly shared WorkFlowy document or list using its share URL."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # Extract share_id from URL
            # WorkFlowy share URLs look like: https://workflowy.com/s/SHARE_ID
            match = re.search(r"/s/([a-zA-Z0-9_-]+)", share_url)
            if not match:
                return {
                    "success": False,
                    "error": f"Could not extract share ID from URL: {share_url}",
                }
            
            share_id = match.group(1)
            url = f"{SHARED_TREE_DATA_URL}{share_id}"
            
            response = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                follow_redirects=True,
            )
            
            if response.status_code != 200:
                return {
                    "success": False,
                    "error": f"Failed to fetch shared document: HTTP {response.status_code}",
                }
            
            data = response.json()
            raw_items = data.get("items", [])
            tree = build_tree(raw_items)
            
            fmt = export_format.lower().strip()
            
            if fmt == "json":
                return {
                    "success": True,
                    "share_id": share_id,
                    "format": "json",
                    "data": tree,
                    "item_count": len(flatten_tree(tree)),
                }
            elif fmt in ("plaintext", "string"):
                text = items_to_plaintext(tree)
                return {
                    "success": True,
                    "share_id": share_id,
                    "format": fmt,
                    "data": text,
                }
            elif fmt == "opml":
                opml_str = items_to_opml(tree)
                return {
                    "success": True,
                    "share_id": share_id,
                    "format": "opml",
                    "data": opml_str,
                }
            else:
                return {
                    "success": False,
                    "error": f"Unknown format '{export_format}'. Supported: json, plaintext, string, opml",
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }


@mcp.tool()
async def download_attachment(
    email: str,
    password: str,
    list_name: str,
    output_path: Optional[str] = None,
) -> dict:
    """Download a file attachment (image or other file) that is attached to a WorkFlowy list item."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            session_headers = await login(email, password, client)
            raw_items = await get_tree_data(session_headers, client)
            tree = build_tree(raw_items)
            
            item = find_item_by_name(tree, list_name)
            if not item:
                return {
                    "success": False,
                    "error": f"List item matching '{list_name}' not found",
                }
            
            metadata = item.get("metadata", {})
            if not metadata:
                return {
                    "success": False,
                    "error": f"Item '{item.get('name')}' has no metadata (no attachment found)",
                }
            
            # WorkFlowy stores file info in metadata
            file_info = metadata.get("file", {})
            if not file_info:
                return {
                    "success": False,
                    "error": f"Item '{item.get('name')}' has no file attachment",
                }
            
            file_name = file_info.get("fileName", "attachment")
            file_type = file_info.get("fileType", "application/octet-stream")
            
            # Build attachment URL - WorkFlowy uses a specific endpoint for file attachments
            item_id = item["id"]
            file_url = f"{WORKFLOWY_URL}/file/{item_id}/{file_name}"
            
            # Try to download the file
            file_response = await client.get(
                file_url,
                headers={**session_headers, "User-Agent": "Mozilla/5.0"},
                follow_redirects=True,
            )
            
            if file_response.status_code != 200:
                return {
                    "success": False,
                    "error": f"Failed to download attachment: HTTP {file_response.status_code}",
                    "attempted_url": file_url,
                    "file_info": file_info,
                }
            
            content = file_response.content
            
            if output_path:
                with open(output_path, "wb") as f:
                    f.write(content)
                return {
                    "success": True,
                    "message": f"Attachment downloaded to '{output_path}'",
                    "file_name": file_name,
                    "file_type": file_type,
                    "size_bytes": len(content),
                    "output_path": output_path,
                    "item_name": item.get("name"),
                }
            else:
                return {
                    "success": True,
                    "message": "Attachment downloaded (not saved to disk)",
                    "file_name": file_name,
                    "file_type": file_type,
                    "size_bytes": len(content),
                    "item_name": item.get("name"),
                    "note": "Provide output_path to save the file to disk",
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }




_SERVER_SLUG = "karelklima-workflowy"

def _track(tool_name: str, ua: str = ""):
    try:
        import urllib.request, json as _json
        data = _json.dumps({"slug": _SERVER_SLUG, "event": "tool_call", "tool": tool_name, "user_agent": ua}).encode()
        req = urllib.request.Request("https://www.volspan.dev/api/analytics/event", data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=1)
    except Exception:
        pass

async def health(request):
    return JSONResponse({"status": "ok", "server": mcp.name})

async def tools(request):
    registered = await mcp.list_tools()
    tool_list = [{"name": t.name, "description": t.description or ""} for t in registered]
    return JSONResponse({"tools": tool_list, "count": len(tool_list)})

sse_app = mcp.http_app(transport="sse")

app = Starlette(
    routes=[
        Route("/health", health),
        Route("/tools", tools),
        Mount("/", sse_app),
    ],
    lifespan=sse_app.lifespan,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
