FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Fail the *build* (not the pod) if any role cannot even be imported —
# catches dependency/version problems before they become a CrashLoopBackOff.
RUN for role in frontend orders inventory; do \
      SERVICE_ROLE=$role python -c "import app.main" || exit 1; \
    done && echo "import check passed for all roles"

# Unprivileged user (matches runAsUser in the manifests)
RUN useradd --uid 10001 --no-create-home shoplite
USER 10001

EXPOSE 8000
# One worker: keeps memory/CPU behaviour per pod easy to reason about
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
