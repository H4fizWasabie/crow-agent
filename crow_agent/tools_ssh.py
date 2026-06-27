"""SSH execution tool."""

from __future__ import annotations

import os
from typing import Any

from .tools_common import is_private_host

# ponytail: guardrails via env vars, no config file needed
_SSH_ALLOWED_HOSTS = frozenset(
    h.strip() for h in os.environ.get("SSH_ALLOWED_HOSTS", "").split(",") if h.strip()
)
_SSH_ALLOWED_COMMANDS = frozenset(
    c.strip() for c in os.environ.get("SSH_ALLOWED_COMMANDS", "").split(",") if c.strip()
)

# Dangerous SSH commands — blocked regardless of host
_SSH_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    ("rm -rf /", "command targets root filesystem"),
    ("mkfs", "command formats a filesystem"),
    (":(){ :|:& };:", "fork bomb detected"),
    ("dd if=/dev/", "command reads from raw disk device"),
    ("> /dev/sd", "command writes directly to disk device"),
    ("chmod 0 ", "command removes ALL file permissions"),
    ("chmod 000", "command removes ALL file permissions"),
]


def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register SSH tools."""

    @registry.register(
        description="Execute a command on a remote server via SSH. Uses password or key authentication. Returns stdout+stderr.",
        name="ssh_exec",
        check_fn=lambda: bool(_SSH_ALLOWED_HOSTS),
    )
    def ssh_exec(
        host: str,
        command: str,
        username: str = "root",
        password: str = "",
        key_path: str = "",
        port: int = 22,
        timeout: int = 30,
    ) -> str:
        # ── Guardrails ──
        # Host allowlist
        if _SSH_ALLOWED_HOSTS and host not in _SSH_ALLOWED_HOSTS:
            allowed = ", ".join(sorted(_SSH_ALLOWED_HOSTS))
            return f"[PERMANENT] SSH host '{host}' not in allowlist. Allowed: {allowed}"

        # Private host check (skip if in explicit allowlist)
        if is_private_host(host) and host not in _SSH_ALLOWED_HOSTS:
            return f"[PERMANENT] SSH blocked: '{host}' resolves to a private/internal address."

        # Command allowlist
        if _SSH_ALLOWED_COMMANDS:
            allowed_cmd = any(command.strip().startswith(c) for c in _SSH_ALLOWED_COMMANDS)
            if not allowed_cmd:
                allowed = ", ".join(sorted(_SSH_ALLOWED_COMMANDS))
                return f"[PERMANENT] SSH command not in allowlist. Allowed prefixes: {allowed}"

        # Dangerous command patterns
        for pattern, reason in _SSH_DANGEROUS_PATTERNS:
            if pattern in command:
                return f"[PERMANENT] SSH command blocked: {reason}."

        # ── Connect ──
        import paramiko
        from paramiko.ssh_exception import SSHException, AuthenticationException
        import socket

        client = paramiko.SSHClient()
        # ponytail: use system known_hosts instead of blind AutoAddPolicy
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.WarningPolicy())
        client.set_log_channel("paramiko.transport")
        import logging
        logging.getLogger("paramiko.transport").setLevel(logging.WARNING)

        try:
            connect_kwargs: dict[str, Any] = {
                "hostname": host,
                "port": port,
                "username": username,
                "timeout": timeout,
                "allow_agent": False,
                "look_for_keys": False,
            }
            if password:
                connect_kwargs["password"] = password
            elif key_path:
                connect_kwargs["key_filename"] = key_path
                connect_kwargs["look_for_keys"] = False
            else:
                connect_kwargs.pop("allow_agent")
                connect_kwargs.pop("look_for_keys")

            client.connect(**connect_kwargs)

            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)

            stdin, stdout, stderr = client.exec_command(
                command,
                timeout=timeout,
                get_pty=True,
            )
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()

            result_parts = []
            if out:
                result_parts.append(out)
            if err:
                result_parts.append(f"[STDERR]\n{err}")
            result = "\n".join(result_parts)
            result += f"\n\n--- exit code: {exit_code} ---"
            return result

        except AuthenticationException as e:
            return f"SSH authentication failed: {e}"
        except socket.timeout:
            return f"SSH connection timed out after {timeout}s"
        except SSHException as e:
            return f"SSH error: {e}"
        except Exception as e:
            return f"SSH connection failed: {e}"
        finally:
            client.close()
