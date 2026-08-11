"""Entry point: `python -m mcp_server`.

Defaults to stdio, which is what Claude Desktop and Claude Code launch. Pass
--transport streamable-http to serve many clients from one deployment instead.
"""
import argparse

from .server import API_URL, server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AutoResearch MCP server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="stdio",
        help="stdio for a local client (default); streamable-http to serve over the network",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind host for http transports")
    parser.add_argument("--port", type=int, default=8080, help="bind port for http transports")
    args = parser.parse_args()

    if args.transport == "stdio":
        # Nothing may be printed to stdout here: on stdio, stdout *is* the protocol
        # channel, and a stray line would corrupt the first JSON-RPC message.
        server.run(transport="stdio")
    else:
        print(f"AutoResearch MCP server on {args.host}:{args.port} -> orchestrator at {API_URL}")
        server.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
