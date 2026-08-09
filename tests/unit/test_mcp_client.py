import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.ai.mcp_client.client import call_tool, close_session
from src.ai.mcp_server.models import ToolError
from mcp.types import CallToolResult, TextContent

@pytest.mark.asyncio
async def test_call_tool_success_parsing():
    """Test call_tool parses successful JSON response correctly."""
    mock_session = AsyncMock()
    mock_response = CallToolResult(
        isError=False,
        content=[
            TextContent(
                type="text",
                text='{"flights": 10000.0, "total": 20000.0}'
            )
        ]
    )
    mock_session.call_tool.return_value = mock_response

    with patch("src.ai.mcp_client.client.get_session", return_value=mock_session):
        res = await call_tool("dummy_tool", {"param1": "val1"})
        
        # Verify the call to the session
        mock_session.call_tool.assert_called_once_with(
            "dummy_tool",
            arguments={"input": {"param1": "val1"}}
        )
        assert isinstance(res, dict)
        assert res["flights"] == 10000.0
        assert res["total"] == 20000.0


@pytest.mark.asyncio
async def test_call_tool_returns_tool_error_model():
    """Test call_tool parses ToolError dictionary into a ToolError Pydantic model."""
    mock_session = AsyncMock()
    mock_response = CallToolResult(
        isError=False,
        content=[
            TextContent(
                type="text",
                text='{"error": "Budget too low", "code": "BUDGET_TOO_LOW"}'
            )
        ]
    )
    mock_session.call_tool.return_value = mock_response

    with patch("src.ai.mcp_client.client.get_session", return_value=mock_session):
        res = await call_tool("dummy_tool", {"budget": 100})
        
        assert isinstance(res, ToolError)
        assert res.error == "Budget too low"
        assert res.code == "BUDGET_TOO_LOW"


@pytest.mark.asyncio
async def test_call_tool_mcp_protocol_error():
    """Test call_tool handles MCP server/protocol errors cleanly by returning a ToolError."""
    mock_session = AsyncMock()
    mock_response = CallToolResult(
        isError=True,
        content=[
            TextContent(
                type="text",
                text="Validation failed"
            )
        ]
    )
    mock_session.call_tool.return_value = mock_response

    with patch("src.ai.mcp_client.client.get_session", return_value=mock_session):
        res = await call_tool("dummy_tool", {})
        
        assert isinstance(res, ToolError)
        assert "Validation failed" in res.error
        assert res.code == "MCP_SERVER_ERROR"


@pytest.mark.asyncio
async def test_call_tool_connection_exception():
    """Test call_tool handles connection exceptions gracefully by returning a ToolError."""
    mock_session = AsyncMock()
    mock_session.call_tool.side_effect = Exception("Connection closed")

    with patch("src.ai.mcp_client.client.get_session", return_value=mock_session):
        res = await call_tool("dummy_tool", {})
        
        assert isinstance(res, ToolError)
        assert "Connection closed" in res.error
        assert res.code == "MCP_CLIENT_ERROR"
