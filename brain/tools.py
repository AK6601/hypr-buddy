import subprocess
import sqlite3
import time
import urllib.request
import urllib.parse
from pathlib import Path
import logging
import re

logger = logging.getLogger("hypr-buddy.brain.tools")

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": "Launch or open a desktop application.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "The exact command or name of the application to launch (e.g., 'firefox', 'kitty', 'dolphin')"
                    }
                },
                "required": ["app_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a general shell command. Use with caution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run."
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "control_hyprland",
            "description": "Control the Hyprland compositor using dispatch commands (e.g., 'exec kitty', 'workspace 1', 'focuswindow firefox').",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The hyprctl dispatch command."
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "media_control",
            "description": "Control media playback (play, pause, next, previous).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play-pause", "next", "previous", "stop"],
                        "description": "The playback action to perform."
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_stats",
            "description": "Get detailed system statistics including CPU, memory, and disk usage.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": "Save an important fact or preference about the user to long-term memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "The concise fact to remember."
                    }
                },
                "required": ["fact"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the internet to answer user questions or find current information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "look_at_screen",
            "description": (
                "Look at the user's current screen to read on-screen text, "
                "see what app/content they're looking at, or answer questions "
                "about what's visible. Use this whenever you need to actually "
                "SEE the desktop to help."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "What you want to see or read on the screen."
                    }
                }
            }
        }
    }
]

class Tools:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        # Create the full-shape table. The async Database (persistence.py) shares
        # this same DB file and will reconcile/ALTER any missing columns if it
        # finds an older bare schema, so it's safe for either side to win the
        # race to first-create.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS memory_facts (
                    id INTEGER PRIMARY KEY,
                    fact TEXT,
                    created_at REAL DEFAULT 0,
                    last_used_at REAL DEFAULT 0,
                    use_count INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'explicit'
                )"""
            )

    def get_memories(self) -> list[str]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT fact FROM memory_facts")
                return [row[0] for row in cursor.fetchall()]
        except Exception:
            return []

    def open_application(self, app_name: str) -> str:
        logger.info(f"Tool executing: open_application({app_name})")
        try:
            # We use nohup and backgrounding to not block the brain
            subprocess.Popen(f"nohup {app_name} >/dev/null 2>&1 &", shell=True)
            return f"Successfully launched {app_name}."
        except Exception as e:
            return f"Failed to launch {app_name}: {e}"

    def run_command(self, command: str) -> str:
        logger.info(f"Tool executing: run_command({command})")
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10.0)
            output = result.stdout.strip() or result.stderr.strip()
            return f"Command output: {output[:500]}"
        except Exception as e:
            return f"Failed to run command: {e}"

    def control_hyprland(self, command: str) -> str:
        logger.info(f"Tool executing: control_hyprland({command})")
        try:
            subprocess.run(["hyprctl", "dispatch", *command.split()], check=True)
            return f"Successfully executed hyprctl dispatch {command}"
        except Exception as e:
            return f"Hyprland control failed: {e}"

    def media_control(self, action: str) -> str:
        logger.info(f"Tool executing: media_control({action})")
        try:
            subprocess.run(["playerctl", action], check=True)
            return f"Successfully performed media action: {action}"
        except Exception as e:
            return f"Media control failed: {e}. Make sure playerctl is installed."

    def get_system_stats(self) -> str:
        logger.info("Tool executing: get_system_stats()")
        try:
            cpu_cmd = "top -bn1 | grep 'Cpu(s)' | awk '{print $2 + $4}'"
            mem_cmd = "free -m | awk '/Mem:/ {print $3 \"/\" $2 \" MB used\"}'"
            disk_cmd = "df -h / | awk '/\\// {print $3 \"/\" $2 \" used\"}'"
            
            cpu = subprocess.check_output(cpu_cmd, shell=True).decode().strip()
            mem = subprocess.check_output(mem_cmd, shell=True).decode().strip()
            disk = subprocess.check_output(disk_cmd, shell=True).decode().strip()
            
            return f"System Stats - CPU Usage: {cpu}%, Memory: {mem}, Disk (/): {disk}"
        except Exception as e:
            return f"Failed to get system stats: {e}"

    def remember_fact(self, fact: str) -> str:
        logger.info(f"Tool executing: remember_fact({fact})")
        try:
            now = time.time()
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO memory_facts
                           (fact, created_at, last_used_at, use_count, source)
                       VALUES (?, ?, ?, 0, 'explicit')""",
                    (fact, now, now),
                )
            return f"Successfully committed '{fact}' to long-term memory."
        except Exception as e:
            return f"Failed to save fact: {e}"

    def search_web(self, query: str) -> str:
        logger.info(f"Tool executing: search_web({query})")
        try:
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            with urllib.request.urlopen(req, timeout=5.0) as response:
                html = response.read().decode('utf-8')
                snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL)
                if not snippets:
                    return "No results found on the web."
                
                text = re.sub(r'<[^>]+>', '', snippets[0])
                text = re.sub(r'\s+', ' ', text).strip()
                return f"Top web result: {text}"
        except Exception as e:
            return f"Web search failed: {e}"

    def execute(self, tool_name: str, args: dict) -> str:
        if tool_name == "open_application":
            return self.open_application(**args)
        elif tool_name == "run_command":
            return self.run_command(**args)
        elif tool_name == "control_hyprland":
            return self.control_hyprland(**args)
        elif tool_name == "media_control":
            return self.media_control(**args)
        elif tool_name == "get_system_stats":
            return self.get_system_stats()
        elif tool_name == "remember_fact":
            return self.remember_fact(**args)
        elif tool_name == "search_web":
            return self.search_web(**args)
        elif tool_name == "look_at_screen":
            # The coordinator (LLMBackend) intercepts look_at_screen and routes
            # it through the vision provider. If we ever reach here, vision was
            # not wired up — return a clear sentinel rather than crashing.
            return "Error: look_at_screen must be handled by the vision coordinator, not Tools.execute."
        else:
            return f"Error: Unknown tool {tool_name}"
