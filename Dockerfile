FROM python:3.12-slim

WORKDIR /app

# Streamlit adds the script's own directory (app/dashboard.py) to sys.path,
# not the working directory — without this, `from app import ...` doesn't resolve.
ENV PYTHONPATH=/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY .streamlit ./.streamlit
# The changelog is not documentation here, it is data: config.APP_VERSION reads
# its top heading, and that is the only thing telling a screenshot which
# revision it came from. Without this line the app runs and reports "unknown",
# which is precisely the state this was meant to end.
COPY CHANGELOG.md .
# The upgrade path ships with the product. scripts/import_sqlite.py is what
# UPGRADING.md tells people to run to bring a pre-Postgres database across, and
# a documented command that fails because the file was never copied in is worse
# than no command — found by running the instructions rather than reading them.
COPY scripts ./scripts

EXPOSE 8501

CMD ["streamlit", "run", "app/dashboard.py", "--server.address=0.0.0.0", "--server.port=8501"]
