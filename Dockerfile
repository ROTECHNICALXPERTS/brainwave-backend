# AutoResearch backend: the orchestrator plus all 8 x402-gated microservices, in one image.
#
# Nine processes in one container is deliberate, not an oversight. The services are separate
# *processes* because the orchestrator pays each one per HTTP call over x402 - that is the
# whole premise of the project, and it also makes the reverse auction and the provider
# failover real rather than simulated. But they are a single deployable *unit*: only the
# orchestrator is ever reachable from outside, and the services are reached at
# http://localhost:400X from within this container. Splitting them into 9 containers would
# add 9 deploys and 9 sets of config to buy exactly nothing.
#
#   docker build -t brainwave-backend .
#   docker run -p 4000:4000 --env-file .env brainwave-backend
FROM python:3.12-slim

WORKDIR /app

# Separate layer from the source copy so `docker build` only reinstalls dependencies when
# requirements.txt actually changes, not on every code edit. Matters on a small VPS where
# a cold web3/eth-account install is the slowest part of the build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# run_all.sh copies .env.example -> .env when no .env exists. In a container that would
# silently seed placeholder values - including REQUIRE_API_KEY=false, which would leave the
# API open to the internet. Creating an empty .env instead means that copy never fires and
# every setting comes from real environment variables (Coolify's env vars), which is what we
# want: shared/__init__.py calls load_dotenv() without override=True, so real environment
# variables always win over anything in this file.
RUN touch .env && chmod +x run_all.sh

# The payment ledger (data/ledger.db) is written here at runtime. Mount a persistent volume
# at this path on the host, or every redeploy starts the ledger from empty.
RUN mkdir -p data
VOLUME ["/app/data"]

# Only the orchestrator is published. Ports 4001-4008 stay internal to the container - the
# 8 services need outbound internet (LLM APIs, the x402 facilitator, the Base Sepolia RPC)
# but nothing external ever needs to connect in to them.
EXPOSE 4000

# uvicorn already binds 0.0.0.0 for the orchestrator inside run_all.sh, which is required in
# a container: the default 127.0.0.1 only accepts connections from inside the container's own
# network namespace and would be unreachable even with the port published.
CMD ["./run_all.sh"]
