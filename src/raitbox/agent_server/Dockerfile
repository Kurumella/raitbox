# RaitBox agent-server image for AWS Lambda MicroVMs.
#
# Built by Lambda MicroVMs on top of a managed base image. The container base
# below is the AWS-provided minimal AL2023 image; the app exposes the RaitBox
# HTTP contract + lifecycle hooks on port 8080. `create_microvm_image` snapshots
# the running process after the /ready hook returns 200.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip && dnf clean all \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3 \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python

WORKDIR /opt/raitbox

COPY requirements.txt .
RUN python3.12 -m pip install --no-cache-dir -r requirements.txt

# Ship the agent server package.
COPY server.py ./raitbox_agent_server/server.py
RUN touch ./raitbox_agent_server/__init__.py

# Resolve the agent-server package by path, not CWD, so declarative Image()
# layers below are free to set their own WORKDIR without breaking startup.
ENV PYTHONPATH=/opt/raitbox

# >>> raitbox custom layers <<<
# Declarative Image() steps are injected here by tooling.build_image.

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# Serve on 0.0.0.0:8080 — the port the MicroVM proxy forwards to by default.
CMD ["python3.12", "-m", "uvicorn", "raitbox_agent_server.server:app", \
     "--host", "0.0.0.0", "--port", "8080"]
