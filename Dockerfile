FROM python:3.12-slim

WORKDIR /app

# Streamlit добавляет в sys.path директорию самого скрипта (app/dashboard.py),
# а не рабочую директорию — без этого `from app import ...` не резолвится.
ENV PYTHONPATH=/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8501

CMD ["streamlit", "run", "app/dashboard.py", "--server.address=0.0.0.0", "--server.port=8501"]
