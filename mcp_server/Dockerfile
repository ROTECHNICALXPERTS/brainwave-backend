# Standalone image for the AutoResearch MCP server.
#
# Build context is THIS directory, not the repo root - mcp_server has no import on
# `shared` or `orchestrator` (it only calls the orchestrator over HTTP, see server.py),
# so it never needs the rest of the monorepo, x402/web3/eth-account included. That also
# means this one folder is independently deployable: copy just mcp_server/ to a host and
# it builds on its own.
#
#   docker build -t autoresearch-mcp mcp_server/
#
# Runs in streamable-http mode: this is what makes the server reachable by an MCP client
# that isn't on this machine. stdio mode (Claude Desktop's local default) is a spawned
# subprocess, not a network service - there's nothing to containerize for that case.
FROM python:3.12-slim

WORKDIR /app

# Separate layer from the source copy so `docker build` only reinstalls deps when
# requirements.txt actually changes, not on every code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./mcp_server/

# Which orchestrator this server talks to - always override at `docker run` time
# (-e AUTORESEARCH_API_URL=https://your-backend) to your real deployed backend. Left as
# localhost here only so the image has a defined default rather than an unset variable.
ENV AUTORESEARCH_API_URL=http://localhost:4000
# No AUTORESEARCH_API_KEY set here deliberately: in streamable-http mode each client
# sends its own key on the request (see _caller_key in server.py's Context.headers use),
# so the server itself doesn't need one. Setting one here would silently become every
# connecting client's shared fallback key.

EXPOSE 8080

# --host 0.0.0.0 is required in a container - the default 127.0.0.1 only accepts
# connections from inside the container's own network namespace, unreachable from
# outside even with the port published.
CMD ["python", "-m", "mcp_server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8080"]
