# MCP（Model Context Protocol）客户端层

MAX_MCP_INSTRUCTIONS_LENGTH = 2048


def truncate_mcp_instructions(value: object) -> str:
    """Apply Claude Code's server-instruction handshake contract."""
    text = str(value or "")
    if len(text) <= MAX_MCP_INSTRUCTIONS_LENGTH:
        return text
    return f"{text[:MAX_MCP_INSTRUCTIONS_LENGTH]}… [truncated]"
