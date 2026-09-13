"""Shared test fakes: in-memory stand-ins for the AWS clients RaitBox calls.

No AWS access is required for the default (non-integration) test run.
"""

from __future__ import annotations

from typing import Any


class FakeCIClient:
    """Fake ``bedrock-agentcore`` Code Interpreter client.

    Emulates a stateful workspace: writeFiles/readFiles/listFiles/removeFiles,
    executeCode (echoes a trailing-newline-stripped stdout), executeCommand
    (pty-merged output in the ``stderr`` field, per the verified quirk).
    """

    def __init__(self) -> None:
        self.files: dict[str, Any] = {}
        self.sessions: list[str] = []
        self.stopped: list[str] = []
        self.code_args: list[dict] = []  # every executeCode arguments dict
        self.cmd_args: list[str] = []  # every executeCommand command string

    def start_code_interpreter_session(self, **kw: Any) -> dict[str, Any]:
        sid = f"ci-{len(self.sessions)}"
        self.sessions.append(sid)
        return {"sessionId": sid, "codeInterpreterIdentifier": kw["codeInterpreterIdentifier"]}

    def stop_code_interpreter_session(self, **kw: Any) -> dict[str, Any]:
        self.stopped.append(kw["sessionId"])
        return {"sessionId": kw["sessionId"]}

    def invoke_code_interpreter(self, **kw: Any) -> dict[str, Any]:
        name = kw["name"]
        args = kw["arguments"]
        handler = getattr(self, f"_op_{name}")
        return {"stream": handler(args)}

    # -- ops ---------------------------------------------------------------

    def _op_executeCode(self, args: dict) -> list[dict]:
        self.code_args.append(args)
        code = args["code"]
        # Emulate print() -> stdout without a trailing newline (AgentCore strips it).
        if "1/0" in code or "raise" in code:
            return [
                {
                    "result": {
                        "content": [
                            {"type": "text", "text": "ZeroDivisionError: division by zero"}
                        ],
                        "structuredContent": {
                            "stdout": "",
                            "stderr": "Traceback...\nZeroDivisionError: division by zero",
                            "exitCode": 1,
                        },
                        "isError": True,
                    }
                }
            ]
        text = "hello from sandbox"
        return [
            {
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": {"stdout": text, "stderr": "", "exitCode": 0},
                    "isError": False,
                }
            }
        ]

    def list_code_interpreter_sessions(self, **kw: Any) -> dict[str, Any]:
        ident = kw["codeInterpreterIdentifier"]
        return {
            "items": [
                {"sessionId": s, "codeInterpreterIdentifier": ident, "status": "READY"}
                for s in self.sessions
                if s not in self.stopped
            ]
        }

    def _op_executeCommand(self, args: dict) -> list[dict]:
        cmd = args["command"]
        self.cmd_args.append(cmd)
        if "false" in cmd or "exit 3" in cmd:
            return [
                {
                    "result": {
                        "content": [{"type": "text", "text": "boom\r\n"}],
                        "structuredContent": {"stdout": "", "stderr": "boom\r\n", "exitCode": 3},
                        "isError": True,
                    }
                }
            ]
        return [
            {
                "result": {
                    "content": [{"type": "text", "text": "ok\r\n"}],
                    "structuredContent": {"stdout": "", "stderr": "ok\r\n", "exitCode": 0},
                    "isError": False,
                }
            }
        ]

    def _op_writeFiles(self, args: dict) -> list[dict]:
        for entry in args["content"]:
            self.files[entry["path"]] = entry.get("text", entry.get("blob"))
        return [
            {
                "result": {
                    "content": [
                        {"type": "text", "text": f"Successfully wrote {len(args['content'])} files"}
                    ],
                    "isError": False,
                }
            }
        ]

    def _op_readFiles(self, args: dict) -> list[dict]:
        path = args["paths"][0]
        if path not in self.files:
            return [{"result": {"content": [], "isError": True}}]
        val = self.files[path]
        resource = {"uri": f"file:///{path}", "mimeType": "text/plain"}
        if isinstance(val, bytes):
            resource["blob"] = val
        else:
            resource["text"] = val
        return [
            {"result": {"content": [{"type": "resource", "resource": resource}], "isError": False}}
        ]

    def _op_listFiles(self, args: dict) -> list[dict]:
        prefix = args.get("directoryPath", "")
        content = []
        for path in self.files:
            if prefix and not path.startswith(prefix):
                continue
            content.append(
                {
                    "type": "resource_link",
                    "uri": f"file:///{path}",
                    "name": path.rsplit("/", 1)[-1],
                    "description": "File",
                }
            )
        return [{"result": {"content": content, "isError": False}}]

    def _op_removeFiles(self, args: dict) -> list[dict]:
        for path in args["paths"]:
            self.files.pop(path, None)
        return [{"result": {"content": [], "isError": False}}]
