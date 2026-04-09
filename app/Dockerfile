FROM python:3.12-slim

# /project is the working dir; app code lives at /project/app/
# so "from app.core..." imports resolve correctly
WORKDIR /project

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /project/app/

EXPOSE 7070

# Single worker — APScheduler runs in-process; multiple workers create duplicate schedulers
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:7070", "--timeout", "120", "--access-logfile", "-", "app.run:app"]
