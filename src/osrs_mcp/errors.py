from fastmcp.exceptions import ToolError


class SourceError(ToolError):
    """A data-source problem with a message Claude can act on or relay to the user.

    ToolError messages are returned to the client as the tool result text (not masked), so
    keep them short, specific, and free of internals.
    """
