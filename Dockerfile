FROM python:3.12-slim

WORKDIR /app

# Streamlit adds the script's own directory (app/dashboard.py) to sys.path,
# not the working directory — without this, `from app import ...` doesn't resolve.
ENV PYTHONPATH=/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY .streamlit ./.streamlit

EXPOSE 8501

CMD ["streamlit", "run", "app/dashboard.py", "--server.address=0.0.0.0", "--server.port=8501"]
