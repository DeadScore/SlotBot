# Vollständiges Python 3.11 Image (inkl. audioop)
FROM python:3.11

# Arbeitsverzeichnis im Container
WORKDIR /app

# Projektdateien kopieren
COPY . /app

# Pip upgraden und Abhängigkeiten installieren
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Bot starten
CMD ["python", "main.py"]
